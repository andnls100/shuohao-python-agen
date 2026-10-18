"""Optional cloud video generation stage. Preview is the default; --video-submit is required for API calls."""
from __future__ import annotations

import json, os, re, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from ..validator import Result
from ..config import models_file_path
from .base import Context, read_json, write_json
from ..visual_style import visual_style_instruction


def artifact(ctx: Context) -> Path:
    return ctx.out / "video" / "video-manifest.json"


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "segment"


def _duration(segment: dict) -> float:
    return round(sum(float(c.get("seconds", 0) or 0) for c in segment.get("cuts", [])), 2)


def _prompt(segment: dict, protocol: str) -> str:
    h3 = str(segment.get("h3Prompt") or "").strip()
    if protocol == "h3":
        if not h3:
            raise ValueError(f"分段 {segment.get('id', '?')} 缺少 h3Prompt")
        return h3
    if protocol != "seedance":
        raise ValueError(f"不支持的视频提示词协议：{protocol}")
    parts = []
    if segment.get("blocking"):
        parts.append("【人物关系与构图逻辑】\n" + str(segment["blocking"]))
    for i, cut in enumerate(segment.get("cuts", []), 1):
        rows = [f"【镜头{i}】"]
        for label, key in (("焦距", "lens"), ("机位", "cameraPosition"), ("构图", "composition"),
                           ("运镜", "camera"), ("景别", "size"), ("画面", "shot"),
                           ("音效", "sfx"), ("光影", "lighting")):
            if cut.get(key):
                rows.append(f"{label}：{cut[key]}")
        parts.append("\n".join(rows))
    if segment.get("soundscape"):
        parts.append("<" + str(segment["soundscape"]) + ">")
    if segment.get("music"):
        parts.append("（" + str(segment["music"]) + "）")
    if h3:
        parts.append("台词与声音参考（保留原文台词）：\n" + h3)
    parts.append("保持角色外观、服装、场景和空间关系连续；避免字幕、画面文字和多余人物。")
    return "\n\n".join(parts)


def _shot_prompt(segment: dict, cut: dict, shot_index: int, visual_style: str) -> str:
    parts = []
    if segment.get("blocking"):
        parts.append("【人物关系与构图逻辑】\n" + str(segment["blocking"]))
    rows = [f"【镜头{shot_index}】"]
    for label, key in (("时长", "seconds"), ("焦距", "lens"), ("机位", "cameraPosition"),
                       ("构图", "composition"), ("运镜", "camera"), ("景别", "size"),
                       ("画面", "shot"), ("音效", "sfx"), ("光影", "lighting"),
                       ("视线", "eyeline"), ("焦点", "focus")):
        if cut.get(key) not in (None, ""):
            rows.append(f"{label}：{cut[key]}秒" if key == "seconds" else f"{label}：{cut[key]}")
    parts.append("\n".join(rows))
    if segment.get("soundscape"):
        parts.append("环境声：" + str(segment["soundscape"]))
    if segment.get("music"):
        parts.append("音乐：" + str(segment["music"]))
    if segment.get("h3Prompt"):
        parts.append("剧情与台词上下文（仅供本镜头参考）：\n" + str(segment["h3Prompt"]))
    parts.append("只生成当前这一个镜头，不要拼接其他镜头；保持角色外观、服装、场景和空间关系连续；避免字幕、画面文字和多余人物。")
    parts.append(visual_style_instruction(visual_style))
    return "\n\n".join(parts)


def _motion_prompt(cut: dict, shot_id: str) -> str:
    parts = []
    for label, key in (("运镜", "camera"), ("画面", "shot"), ("音效", "sfx")):
        value = str(cut.get(key) or "").strip()
        if value:
            parts.append(f"{label}：{value}")
    if not any(p.startswith("画面") for p in parts):
        raise ValueError(f"镜头 {shot_id} 缺少 shot 字段，无法生成图生视频提示词")
    parts.append("保持首帧画面中人物外观、服装和场景不变；无字幕、无水印。")
    return "\n".join(parts)


def _frame_file(base: Path, sid: str, index: int) -> Path:
    for suffix in (".png", ".jpg", ".jpeg"):
        candidate = base / sid / f"f{index}{suffix}"
        if candidate.is_file():
            return candidate
    return base / sid / f"f{index}.png"


def _rel(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())


def _reference_assets(ctx: Context, ep: int, segment: dict, cuts: list, limit: int = 5) -> list[dict]:
    """Reuse the frames stage's character/scene/prop reference resolution."""
    from .frames import _asset_index, _scene_ids, _references

    assets, scene_ids = _asset_index(ctx), _scene_ids(ctx)
    merged, seen = [], set()
    for cut in cuts:
        refs, _missing = _references(ctx, ep, segment, cut, assets, scene_ids, limit=99)
        for ref in refs:
            if ref["path"] in seen:
                continue
            seen.add(ref["path"])
            merged.append(dict(ref))
    for no, ref in enumerate(merged[:limit], 1):
        ref["no"] = no
    return merged[:limit]


def _collect(ctx: Context, eps: Optional[list], protocol: str, visual_style: str, shots: Optional[list] = None,
             frames_dir: Optional[Path] = None, flf: bool = False) -> dict:
    source = ctx.artifact("storyboard")
    if not source.is_file():
        raise FileNotFoundError(f"找不到分镜文件：{source}；请先运行 storyboard 阶段")
    board = read_json(source)
    wanted, items = set(eps or []), []
    style_hint = visual_style_instruction(visual_style)
    for episode in board.get("episodes", []):
        ep = int(episode.get("ep", 0))
        if wanted and ep not in wanted:
            continue
        for idx, segment in enumerate(episode.get("segments", []), 1):
            sid = str(segment.get("id") or f"E{ep:02d}-{idx:02d}")
            cuts = segment.get("cuts") or []
            segment_refs = _reference_assets(ctx, ep, segment, cuts, limit=5) if cuts else []
            if protocol == "i2v":
                if not cuts:
                    raise ValueError(f"分段 {sid} 没有 cuts，无法按分镜图生视频")
                base = Path(frames_dir) if frames_dir else ctx.out / "frames"
                for cut_idx, cut in enumerate(cuts, 1):
                    shot_id = f"{sid}-C{cut_idx:02d}"
                    try:
                        duration = round(float(cut.get("seconds", 0) or 0), 2)
                    except (TypeError, ValueError):
                        duration = 0
                    if duration <= 0:
                        raise ValueError(f"镜头 {shot_id} 的 seconds 必须大于 0")
                    first = _frame_file(base, sid, cut_idx)
                    last = _frame_file(base, sid, cut_idx + 1) if flf and cut_idx < len(cuts) else None
                    refs = _reference_assets(ctx, ep, segment, [cut], limit=5)
                    items.append({
                        "id": shot_id, "ep": ep, "sceneIndex": segment.get("sceneIndex"),
                        "segmentId": sid, "shotIndex": cut_idx, "duration_seconds": duration,
                        "mode": "flf" if last else "i2v",
                        "first_frame": _rel(first, ctx.out), "last_frame": _rel(last, ctx.out) if last else None,
                        "reference_assets": refs,
                        "prompt": _motion_prompt(cut, shot_id),
                        "state": "planned", "task_id": None, "video_url": None,
                        "video_path": None, "error": None,
                    })
            elif protocol == "seedance" and cuts:
                for cut_idx, cut in enumerate(cuts, 1):
                    shot_id = f"{sid}-C{cut_idx:02d}"
                    try:
                        duration = round(float(cut.get("seconds", 0) or 0), 2)
                    except (TypeError, ValueError):
                        duration = 0
                    if duration <= 0:
                        raise ValueError(f"镜头 {shot_id} 的 seconds 必须大于 0")
                    refs = _reference_assets(ctx, ep, segment, [cut], limit=5)
                    items.append({
                        "id": shot_id, "ep": ep, "sceneIndex": segment.get("sceneIndex"),
                        "segmentId": sid, "shotIndex": cut_idx, "duration_seconds": duration,
                        "reference_assets": refs,
                        "prompt": _shot_prompt(segment, cut, cut_idx, visual_style),
                        "state": "planned", "task_id": None, "video_url": None,
                        "video_path": None, "error": None
                    })
            else:
                base_prompt = _prompt(segment, protocol)
                items.append({
                    "id": sid, "ep": ep, "sceneIndex": segment.get("sceneIndex"),
                    "duration_seconds": _duration(segment), "reference_assets": segment_refs,
                    "prompt": base_prompt + "\n\n" + style_hint,
                    "state": "planned", "task_id": None, "video_url": None,
                    "video_path": None, "error": None
                })
    if shots:
        requested = {str(value).strip() for value in shots if str(value).strip()}
        items = [item for item in items if item["id"] in requested]
        missing = requested - {item["id"] for item in items}
        if missing:
            raise ValueError("找不到指定镜头 ID：" + "、".join(sorted(missing)) + "；请查看 output/video/video-manifest.json 中的 items[].id")
    if not items:
        raise ValueError("所选集没有可生成的视频镜头/分段；检查分镜产物或 --eps 参数")
    return {"version": 2, "source": ctx.title, "protocol": protocol, "visual_style": visual_style,
            "aspect_ratio": board.get("aspectRatio", "16:9"), "items": items}


def _config(model_key: Optional[str] = None) -> list:
    path = models_file_path()
    if not path.is_file():
        raise ValueError(f"找不到模型配置文件：{path}；请先复制 models.example.json 为 models.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 {path}：{exc}") from exc
    entries = data.get("video", [])
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path} 中必须配置非空的 video 列表；请参照 models.example.json")
    if model_key:
        matched = [e for e in entries if isinstance(e, dict) and model_key in
                   (str(e.get("key") or ""), str(e.get("name") or ""), str(e.get("model") or ""))]
        if not matched:
            raise ValueError(f"models.json 的 video 列表中找不到模型 {model_key!r}；请用 key/name/model 指定")
        if all(e.get("enabled", True) is False for e in matched):
            raise ValueError(f"模型 {model_key!r} 当前 enabled=false；请先核实接口文档并配置后再启用")
        entries = [e for e in matched if e.get("enabled", True) is not False]
    else:
        enabled = [e for e in entries if isinstance(e, dict) and e.get("enabled", True) is not False]
        active = [e for e in enabled if e.get("active") is True]
        entries = active or enabled
    providers = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"video[{index}] 必须是对象")
        name = str(entry.get("name") or f"video-{index}").strip()
        key = str(entry.get("key") or name).strip()
        adapter = str(entry.get("adapter") or "generic").strip().lower()
        submit_url = str(entry.get("submit_url") or "").strip()
        status_template = str(entry.get("status_url_template") or "").strip()
        workspace_env = str(entry.get("workspace_id_env") or "").strip()
        workspace_id = str(entry.get("workspace_id") or (os.environ.get(workspace_env, "") if workspace_env else "")).strip()
        if "{workspace_id}" in submit_url.lower():
            submit_url = re.sub(r"\{workspace_id\}", workspace_id, submit_url, flags=re.I)
        if "{workspace_id}" in status_template.lower():
            status_template = re.sub(r"\{workspace_id\}", workspace_id, status_template, flags=re.I)
        model = str(entry.get("model") or "").strip()
        api_key = str(entry.get("api_key") or "").strip()
        api_key_env = str(entry.get("api_key_env") or "").strip()
        if not api_key and api_key_env:
            api_key = os.environ.get(api_key_env, "").strip()
        missing = [field for field, value in (("submit_url", submit_url), ("model", model), ("api_key 或 api_key_env", api_key)) if not value]
        if adapter == "aliyun_wan" and not workspace_id:
            missing.append("workspace_id 或 workspace_id_env")
        if missing:
            raise ValueError(f"models.json 的视频模型 {key}（{name}）缺少：{'、'.join(missing)}")
        if urlparse(submit_url).scheme not in ("http", "https"):
            raise ValueError(f"models.json 的视频模型 {key}（{name}）submit_url 必须是 HTTP(S) URL")
        providers.append({
            "key": key, "name": name, "adapter": adapter,
            "VIDEO_API_SUBMIT_URL": submit_url, "VIDEO_API_KEY": api_key, "VIDEO_API_MODEL": model,
            "status_template": status_template, "timeout": max(10, int(entry.get("timeout", 120))),
            "poll": max(1, float(entry.get("poll_interval", 15 if adapter == "aliyun_wan" else 5))),
            "max_wait": max(10, int(entry.get("max_wait", 900))),
            "resolution": entry.get("resolution", "720P"), "ratio": entry.get("ratio", "16:9"),
            "prompt_extend": bool(entry.get("prompt_extend", True)), "request_template": entry.get("request_template"),
            "size": entry.get("size", "1280x720"), "quality": entry.get("quality", "standard"),
            "with_audio": bool(entry.get("with_audio", True)), "fps": int(entry.get("fps", 24)),
            "headers": entry.get("headers") or {}, "image_encoding": entry.get("image_encoding", "data_uri"),
            "video_url_keys": entry.get("video_url_keys") or ["video_url", "videoUrl", "output_url", "download_url"],
            "task_id_keys": entry.get("task_id_keys") or ["task_id", "taskId", "job_id", "jobId", "id"],
            "status_keys": entry.get("status_keys") or ["status", "state"],
            "failed_states": entry.get("failed_states") or ["failed", "FAIL", "error", "ERROR"],
        })
    return providers


def _headers(provider: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    headers.update(provider.get("headers") or {})
    key = provider.get("VIDEO_API_KEY")
    if key:
        headers.setdefault("Authorization", "Bearer " + key)
    # Aliyun Wan video-synthesis (including Wan R2V) only supports async HTTP calls.
    # Missing this header causes AccessDenied: current user api does not support synchronous calls.
    if str(provider.get("adapter") or "").strip().lower() == "aliyun_wan":
        headers["X-DashScope-Async"] = "enable"
    return headers


def _request(url: str, provider: dict, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=_headers(provider), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=provider.get("timeout", 120)) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络请求失败：{exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"接口返回非 JSON：{raw[:500]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("接口返回 JSON 不是对象")
    return data


def _find(data: dict, keys: tuple) -> Optional[str]:
    def walk(obj):
        if isinstance(obj, dict):
            for key in keys:
                if key in obj and obj[key] not in (None, ""):
                    value = obj[key]
                    if isinstance(value, (str, int, float)):
                        return str(value)
            for value in obj.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = walk(value)
                if found:
                    return found
        return None
    return walk(data)


def _poll(task_id: str, provider: dict) -> dict:
    template = provider.get("status_template")
    if not template:
        raise RuntimeError(f"平台 {provider['name']} 已返回 task_id={task_id}，但 models.json 未配置 status_url_template")
    url = template.format(task_id=urllib.parse.quote(task_id, safe=""))
    deadline = time.time() + provider.get("max_wait", 900)
    while time.time() < deadline:
        data = _request_get(url, provider)
        status = str(_find(data, tuple(provider.get("status_keys") or ["status", "state"])) or "").strip()
        if status in set(str(x) for x in provider.get("failed_states") or []):
            raise RuntimeError(f"视频任务 {task_id} 失败：{data}")
        url_value = _find(data, tuple(provider.get("video_url_keys") or ["video_url", "videoUrl", "output_url", "download_url"]))
        if url_value:
            return data
        time.sleep(provider.get("poll", 5))
    raise TimeoutError(f"视频任务 {task_id} 超时：{provider.get('max_wait', 900)} 秒")


def _request_get(url: str, provider: dict) -> dict:
    req = urllib.request.Request(url, headers=_headers(provider), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=provider.get("timeout", 120)) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络请求失败：{exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"接口返回非 JSON：{raw[:500]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("接口返回 JSON 不是对象")
    return data


def _download(url: str, target: Path, provider: dict) -> None:
    if urlparse(url).scheme not in ("http", "https"):
        raise RuntimeError("视频 URL 必须为 HTTP(S)")
    target.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Accept": "video/mp4,video/*,application/octet-stream"}
    key = provider.get("VIDEO_API_KEY")
    if key:
        headers["Authorization"] = "Bearer " + key
    custom = provider.get("headers") or {}
    if isinstance(custom, dict):
        for name, value in custom.items():
            headers[str(name)] = str(value)
    req = urllib.request.Request(url, headers=headers, method="GET")
    temp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=provider.get("timeout", 120)) as response:
            with temp.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
        if not temp.is_file() or temp.stat().st_size <= 0:
            raise RuntimeError("视频下载完成但文件为空")
        temp.replace(target)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"视频下载 HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"视频下载失败：{exc}") from exc
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def _resolve(base: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else base / p


def _submit(item: dict, manifest: dict, providers: list, root: Path, path: Path) -> None:
    if item.get("state") == "succeeded" and item.get("video_url"):
        return
    try:
        if item.get("task_id"):
            cfg = next((p for p in providers if p["name"] == item.get("provider")), None)
            if cfg is None:
                raise RuntimeError(
                    f"任务 {item['task_id']} 记录的平台 {item.get('provider')!r} 不在当前 models.json video 号池中；"
                    "请恢复该平台配置后续查"
                )
            item["state"] = "polling"
            write_json(path, manifest)
            result = _poll(str(item["task_id"]), cfg)
        else:
            failures = []
            result = None
            cfg = None
            frames = [item.get(k) for k in ("first_frame", "last_frame") if item.get(k)]
            for candidate in providers:
                item["state"], item["error"] = "submitting", None
                item["provider"] = candidate["name"]
                write_json(path, manifest)
                from .video_adapters import build_request, encode_image
                try:
                    is_wan_r2v = candidate.get("adapter") == "aliyun_wan" and "r2v" in candidate.get("VIDEO_API_MODEL", "").lower()
                    if is_wan_r2v:
                        images = []
                    else:
                        missing = [f for f in frames if not _resolve(root.parent, f).is_file()]
                        if missing:
                            raise FileNotFoundError("缺少分镜图：" + "、".join(missing) + "；请先生成分镜图（默认目录 output/frames/<段号>/f<切序>.png，或用 --frames-dir 指定）")
                        images = [encode_image(_resolve(root.parent, f), candidate.get("image_encoding", "data_uri")) for f in frames]
                    reference_media = []
                    if is_wan_r2v:
                        assets = item.get("reference_assets") or []
                        if not assets:
                            raise ValueError(f"Wan R2V 镜头 {item['id']} 没有从 character/art 找到参考图")
                        for asset in assets:
                            ref_path = _resolve(root.parent, asset["path"])
                            if not ref_path.is_file():
                                raise FileNotFoundError(f"Wan R2V 参考图不存在：{asset['path']}")
                            kind = asset.get("kind")
                            label = ("角色「" if kind == "character" else "场景「" if kind == "scene" else
                                     "道具「" if kind == "prop" else "参考图「") + str(asset.get("name") or "") + "」"
                            reference_media.append({
                                "type": "reference_image", "url": encode_image(ref_path, "data_uri"), "label": label,
                            })
                    request_item = {**item, "images": images, "reference_media": reference_media}
                    body = build_request(candidate, request_item, manifest)
                    # Print the final, skill-optimized prompt exactly as sent to the video API.
                    submitted_prompt = body.get("input", {}).get("prompt") if isinstance(body.get("input"), dict) else body.get("prompt")
                    if submitted_prompt:
                        print(
                            f"\n===== 视频提示词（{item['id']} | {candidate['name']} | 技能优化后，实际提交内容）=====\n"
                            f"{submitted_prompt}\n"
                            f"===== 提示词结束：{item['id']} =====\n",
                            flush=True,
                        )
                    response = _request(candidate["VIDEO_API_SUBMIT_URL"], candidate, body)
                    url = _find(response, tuple(candidate.get("video_url_keys") or ["video_url", "videoUrl", "output_url", "download_url"]))
                    task_id = _find(response, tuple(candidate.get("task_id_keys") or ["task_id", "taskId", "job_id", "jobId", "id"]))
                    if task_id:
                        item["task_id"] = task_id
                        item["provider"] = candidate["name"]
                    if url:
                        result, cfg = response, candidate
                        break
                    if task_id:
                        # Submit asynchronously and persist the task ID; polling happens on a later run.
                        # This lets the pipeline submit every segment without blocking on each render.
                        item["state"] = "submitted"
                        write_json(path, manifest)
                        print(
                            f"视频任务已异步提交：{item['id']} | task_id={task_id}",
                            flush=True,
                        )
                        return
                    raise RuntimeError("响应中找不到 video_url、output_url 或 task_id；需适配该平台响应格式")
                except Exception as exc:
                    if item.get("task_id"):
                        raise
                    failures.append(f"{candidate['name']}: {exc}")
            if result is None or cfg is None:
                raise RuntimeError("所有视频 API 备用平台均失败：" + "；".join(failures))
        url = _find(result, tuple(cfg.get("video_url_keys") or ["video_url", "videoUrl", "output_url", "download_url"]))
        if not url:
            raise RuntimeError("任务结束但响应中没有视频 URL")
        target = root / "videos" / (_safe(item["id"]) + ".mp4")
        _download(url, target, cfg)
        item.update(state="succeeded", video_url=url, error=None, provider=cfg["name"], video_path=str(target.relative_to(root)), download_error=None)
        print(
            f"视频下载成功：{item['id']}\n下载位置：{target.resolve()}",
            flush=True,
        )
    except Exception as exc:
        item.update(state="failed", error=str(exc))
    write_json(path, manifest)


def check(ctx: Context) -> Result:
    path = artifact(ctx)
    if not path.is_file():
        return Result(1, "视频任务清单尚未生成")
    try:
        items = read_json(path).get("items", [])
    except (OSError, ValueError) as exc:
        return Result(1, f"视频任务清单不可读：{exc}")
    pending = [x for x in items if x.get("state") != "succeeded"]
    if not items or pending:
        return Result(1, f"{len(pending)}/{len(items)} 个视频分段尚未生成")
    return Result(0, f"{len(items)} 个视频分段已生成")


def run(ctx: Context, eps: Optional[list] = None, protocol: str = "h3", submit: bool = False,
        visual_style: str = "manju", shots: Optional[list] = None, video_model: Optional[str] = None,
        frames_dir: Optional[Path] = None, flf: bool = False) -> Path:
    root = ctx.out / "video"
    root.mkdir(parents=True, exist_ok=True)
    path = artifact(ctx)
    manifest = _collect(ctx, eps, protocol, visual_style, shots, frames_dir, flf)
    # Print the exact resolved reference-image paths before any video API submission.
    ctx.log("视频参考图路径检查（提交视频 API 前）：")
    for item in manifest["items"]:
        refs = item.get("reference_assets") or []
        if not refs:
            ctx.log(f"  {item['id']}: 未匹配到参考图")
            continue
        for asset in refs:
            ref_path = _resolve(ctx.out, str(asset.get("path") or ""))
            status = "存在" if ref_path.is_file() else "不存在"
            ctx.log(
                f"  {item['id']}: [{asset.get('kind', 'unknown')}] "
                f"{asset.get('name', '')} -> {ref_path.resolve()} [{status}]"
            )
    previous = {}
    if path.is_file() and not ctx.force:
        try:
            old = read_json(path)
            if old.get("protocol") == protocol and old.get("visual_style", "manju") == visual_style:
                previous = {str(x.get("id")): x for x in old.get("items", [])}
        except (OSError, ValueError):
            pass
    if previous and (eps or shots):
        selected_eps = set(eps or [])
        selected_ids = {item["id"] for item in manifest["items"]}
        if shots:
            extras = [dict(item) for item in previous.values() if str(item.get("id")) not in selected_ids]
        else:
            extras = [dict(item) for item in previous.values()
                      if int(item.get("ep", 0)) not in selected_eps and str(item.get("id")) not in selected_ids]
        manifest["items"] = extras + manifest["items"]
        manifest["items"].sort(key=lambda item: (int(item.get("ep", 0)), str(item.get("id", ""))))
    for item in manifest["items"]:
        old = previous.get(item["id"])
        if old and old.get("prompt") == item["prompt"] and all(
                old.get(k) == item.get(k) for k in ("mode", "first_frame", "last_frame", "reference_assets")):
            for key in ("state", "task_id", "provider", "video_url", "video_path", "error", "download_error"):
                if key in old:
                    item[key] = old[key]
    manifest["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_json(path, manifest)
    prompts = root / "prompts"
    prompts.mkdir(exist_ok=True)
    for item in manifest["items"]:
        (prompts / (_safe(item["id"]) + ".txt")).write_text(item["prompt"] + "\n", encoding="utf-8")
    qc = ["# 视频人工质检清单", "", "- [ ] 角色外观、服装与参考图一致",
          "- [ ] 人物位置、视线和动作方向连续", "- [ ] 台词完整，发音和口型可接受",
          "- [ ] 没有意外字幕、乱码、水印或多余人物", "- [ ] 时长和画幅符合交付要求",
          "- [ ] 与前后分段的剪辑点自然", "", "## 分段状态", ""]
    qc += [f"- [ ] {x['id']}：{x['state']}（第 {x['ep']} 集，约 {x['duration_seconds']} 秒）"
           for x in manifest["items"]]
    (root / "qc-checklist.md").write_text("\n".join(qc) + "\n", encoding="utf-8")
    ctx.log(f"视频阶段：已生成 {len(manifest['items'])} 个提示词和任务清单：{path}")
    absent = [f for it in manifest["items"] for f in (it.get("first_frame"), it.get("last_frame"))
              if f and not _resolve(ctx.out, f).is_file()]
    if absent and submit:
        ctx.log(f"警告：{len(absent)} 个分镜图不存在；只有支持引用资产的视频适配器可继续提交")
    if submit:
        providers = _config(video_model)
        for item in manifest["items"]:
            _submit(item, manifest, providers, ctx.out / "video", path)
    return path
