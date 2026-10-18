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
    """为 Seedance 单独构造一个镜头的提示词，而不是把整段 cuts 一次提交。"""
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


def _collect(ctx: Context, eps: Optional[list], protocol: str, visual_style: str, shots: Optional[list] = None) -> dict:
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
            if protocol == "seedance" and segment.get("cuts"):
                # Seedance 模式下每个 cut 独立生成一个视频镜头，方便单镜头重试和跳过已完成项。
                for cut_idx, cut in enumerate(segment.get("cuts", []), 1):
                    shot_id = f"{sid}-C{cut_idx:02d}"
                    try:
                        duration = round(float(cut.get("seconds", 0) or 0), 2)
                    except (TypeError, ValueError):
                        duration = 0
                    if duration <= 0:
                        raise ValueError(f"镜头 {shot_id} 的 seconds 必须大于 0")
                    items.append({
                        "id": shot_id, "ep": ep, "sceneIndex": segment.get("sceneIndex"),
                        "segmentId": sid, "shotIndex": cut_idx,
                        "duration_seconds": duration,
                        "prompt": _shot_prompt(segment, cut, cut_idx, visual_style),
                        "state": "planned", "task_id": None, "video_url": None,
                        "video_path": None, "error": None
                    })
            else:
                base_prompt = _prompt(segment, protocol)
                items.append({
                    "id": sid, "ep": ep, "sceneIndex": segment.get("sceneIndex"),
                    "duration_seconds": _duration(segment),
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
    """Read video provider configs; explicit model selection disables cross-model failover."""
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
            "resolution": str(entry.get("resolution") or "480P"), "ratio": str(entry.get("ratio") or ""),
            "prompt_extend": entry.get("prompt_extend", True), "request_template": entry.get("request_template"),
            "size": str(entry.get("size") or ""), "quality": entry.get("quality", "quality"),
            "with_audio": entry.get("with_audio", True), "fps": int(entry.get("fps", 30)),
            "headers": entry.get("headers", {}),
            "video_url_keys": entry.get("video_url_keys", ["video_url", "videoUrl", "output_url", "download_url"]),
            "task_id_keys": entry.get("task_id_keys", ["task_id", "taskId", "job_id", "jobId", "id"]),
            "status_keys": entry.get("status_keys", ["status", "state", "task_status"]),
            "failed_states": entry.get("failed_states", ["failed", "fail", "error", "cancelled", "canceled", "rejected"]),
        })
    if not providers:
        raise ValueError("models.json 的 video 配置没有启用的模型；请设置 enabled=true")
    return providers
def _request(url: str, cfg: dict, body: Optional[dict] = None) -> dict:
    from .video_adapters import request_headers
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    headers = request_headers(cfg, has_body=data is not None)
    req = urllib.request.Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as res:
            raw = res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"视频 API HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:800]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"视频 API 网络错误：{exc}") from exc
    try:
        result = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"视频 API 返回的不是 JSON：{raw[:300]}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("视频 API JSON 顶层必须是对象")
    return result
def _find(obj, keys: tuple):
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        for value in obj.values():
            found = _find(value, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find(value, keys)
            if found:
                return found
    return None


def _poll(task_id: str, cfg: dict) -> dict:
    if not cfg["status_template"]:
        raise ValueError("API 返回任务 ID，但未配置 VIDEO_API_STATUS_URL_TEMPLATE")
    url = cfg["status_template"].replace("{task_id}", urllib.parse.quote(task_id, safe=""))
    deadline = time.monotonic() + cfg["max_wait"]
    while True:
        result = _request(url, cfg)
        if _find(result, tuple(cfg.get("video_url_keys") or ["video_url", "videoUrl", "output_url", "download_url"])):
            return result
        state = (_find(result, tuple(cfg.get("status_keys") or ["status", "state", "task_status"])) or "").lower()
        if state in {str(x).lower() for x in (cfg.get("failed_states") or ["failed", "fail", "error", "cancelled", "canceled", "rejected"])}:
            raise RuntimeError("视频任务失败：" + json.dumps(result, ensure_ascii=False)[:800])
        if time.monotonic() >= deadline:
            raise TimeoutError(f"任务 {task_id} 等待超时；任务 ID 已保存，可重试续查")
        time.sleep(cfg["poll"])


def _download(url: str, target: Path, cfg: dict) -> None:
    if urlparse(url).scheme not in ("http", "https"):
        raise RuntimeError("视频 URL 必须为 HTTP(S)")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".mp4.part")
    try:
        with urllib.request.urlopen(url, timeout=cfg["timeout"]) as res, tmp.open("wb") as out:
            while True:
                chunk = res.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        if tmp.stat().st_size == 0:
            raise RuntimeError("下载到的视频为空")
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink()


def _submit(item: dict, manifest: dict, providers: list, root: Path, path: Path) -> None:
    if item.get("state") == "succeeded" and item.get("video_url"):
        return
    try:
        # 已有 task_id 时必须沿用原平台查询，避免重复创建付费任务。
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
            for candidate in providers:
                item["state"], item["error"] = "submitting", None
                item["provider"] = candidate["name"]
                write_json(path, manifest)
                from .video_adapters import build_request
                body = build_request(candidate, item, manifest)
                try:
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
                        # 已创建任务后不切换平台，避免重复生成和重复计费。
                        item["state"] = "polling"
                        write_json(path, manifest)
                        result, cfg = _poll(task_id, candidate), candidate
                        break
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
        item.update(state="succeeded", video_url=url, error=None, provider=cfg["name"])
        try:
            target = root / "videos" / (_safe(item["id"]) + ".mp4")
            _download(url, target, cfg)
            item["video_path"] = str(target.relative_to(root))
        except Exception as exc:
            item["video_path"], item["download_error"] = None, str(exc)
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
        visual_style: str = "manju", shots: Optional[list] = None, video_model: Optional[str] = None) -> Path:
    root = ctx.out / "video"
    root.mkdir(parents=True, exist_ok=True)
    path = artifact(ctx)
    manifest = _collect(ctx, eps, protocol, visual_style, shots)
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
            # 只处理指定镜头时，把清单中未选中的已有镜头保留下来。
            extras = [dict(item) for item in previous.values() if str(item.get("id")) not in selected_ids]
        else:
            extras = [dict(item) for item in previous.values()
                      if int(item.get("ep", 0)) not in selected_eps and str(item.get("id")) not in selected_ids]
        manifest["items"] = extras + manifest["items"]
        manifest["items"].sort(key=lambda item: (int(item.get("ep", 0)), str(item.get("id", ""))))
    for item in manifest["items"]:
        old = previous.get(item["id"])
        if old and old.get("prompt") == item["prompt"]:
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
    if not submit:
        ctx.log("预览模式：未调用视频 API。检查提示词后再加 --video-submit 发起生成。")
        return path
    cfg = _config(video_model)
    selected_shots = {str(value).strip() for value in (shots or []) if str(value).strip()}
    for item in manifest["items"]:
        if selected_shots and item["id"] not in selected_shots:
            continue
        if item.get("state") == "succeeded" and item.get("video_url"):
            ctx.log(f"跳过已生成镜头：{item['id']}")
            continue
        ctx.log(f"视频任务：{item['id']}（{item['duration_seconds']} 秒）")
        _submit(item, manifest, cfg, root, path)
        ctx.log(f"  状态：{item['state']}" + (f"；错误：{item['error']}" if item.get("error") else ""))
    return path
