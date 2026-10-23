"""Sequential cloud video generation with explicit shot-to-shot continuity."""
from __future__ import annotations

import json, os, re, shutil, subprocess, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from ..config import models_file_path
from ..validator import Result
from ..visual_style import visual_style_instruction
from .base import Context, read_json, write_json


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
        for label, key in (("焦距", "lens"), ("机位", "cameraPosition"), ("构图", "composition"), ("运镜", "camera"), ("景别", "size"), ("画面", "shot"), ("音效", "sfx"), ("光影", "lighting")):
            if cut.get(key): rows.append(f"{label}：{cut[key]}")
        parts.append("\n".join(rows))
    if segment.get("soundscape"): parts.append("环境声：" + str(segment["soundscape"]))
    if segment.get("music"): parts.append("音乐：" + str(segment["music"]))
    if h3: parts.append("台词与声音参考（保留原文台词）：\n" + h3)
    parts.append("保持角色外观、服装、场景和空间关系连续；避免字幕、画面文字和多余人物。")
    return "\n\n".join(parts)


def _shot_prompt(segment: dict, cut: dict, shot_index: int, visual_style: str, visual_style_prompt: str = "") -> str:
    parts = []
    if segment.get("blocking"):
        parts.append("【人物关系与构图逻辑】\n" + str(segment["blocking"]))
    rows = [f"【镜头{shot_index}】"]
    for label, key in (("时长", "seconds"), ("焦距", "lens"), ("机位", "cameraPosition"), ("构图", "composition"), ("运镜", "camera"), ("景别", "size"), ("画面", "shot"), ("音效", "sfx"), ("光影", "lighting"), ("视线", "eyeline"), ("焦点", "focus")):
        if cut.get(key) not in (None, ""):
            rows.append(f"{label}：{cut[key]}秒" if key == "seconds" else f"{label}：{cut[key]}")
    parts.append("\n".join(rows))
    if segment.get("soundscape"): parts.append("环境声：" + str(segment["soundscape"]))
    if segment.get("music"): parts.append("音乐：" + str(segment["music"]))
    if segment.get("h3Prompt"): parts.append("剧情与台词上下文（仅供本镜头参考）：\n" + str(segment["h3Prompt"]))
    parts.append("只生成当前这个镜头；保持角色外观、服装、场景和空间关系连续；避免字幕、画面文字和多余人物。")
    parts.append(visual_style_instruction(visual_style, visual_style_prompt))
    return "\n\n".join(parts)


def _reference_assets(ctx: Context, ep: int, segment: dict, cut: dict, limit: int = 5) -> list[dict]:
    from .frames import _asset_index, _scene_ids, _references
    assets, scene_ids = _asset_index(ctx), _scene_ids(ctx)
    refs, _missing = _references(ctx, ep, segment, cut, assets, scene_ids, limit=99)
    return [dict(ref, no=index) for index, ref in enumerate(refs[:limit], 1)]


def _asset(refs: list[dict], kind: str) -> Optional[dict]:
    return next((r for r in refs if r.get("kind") == kind), None)


def _is_continuous(previous: Optional[dict], current: dict) -> tuple[bool, str]:
    if not previous: return False, "first_shot"
    if int(previous.get("ep", -1)) != int(current.get("ep", -2)): return False, "episode_boundary"
    continuity = current.get("continuity") or {}
    if str(continuity.get("mode") or "").strip().lower() != "inherit": return False, "explicit_cut_or_unspecified"
    if str(current.get("continuity_mode") or "") == "hard_cut": return False, "segment_hard_cut"
    if int(previous.get("sceneIndex") or -1) != int(current.get("sceneIndex") or -2): return False, "scene_changed"
    if str(previous.get("segmentId") or "") != str(current.get("segmentId") or "") and str(current.get("continuity_mode") or "") != "continuous":
        return False, "cross_segment_not_marked_continuous"
    reference = str(continuity.get("referenceFrame") or "").strip()
    allowed = {str(previous.get("id") or ""), str(previous.get("frame_ref") or ""), str(previous.get("segmentId") or "") + "/f" + str(previous.get("shotIndex") or "") + ".png"}
    if not reference: return False, "inherit_missing_referenceFrame"
    if reference not in allowed: return False, "reference_not_previous_shot"
    return True, "explicit_inherit"


def _merge_clips(items: list, protocol: str, max_seconds: float) -> list:
    """Merge consecutive shots into one video call: same segment, each later shot continuous with the one before it
    (explicit inherit), total duration <= max_seconds. max_seconds <= 0 disables merging."""
    if not max_seconds or max_seconds <= 0 or not items: return items
    groups = []
    for item in items:
        group = groups[-1] if groups else None
        if (group and item["input_mode"] == "previous_last_frame" and item.get("previous_shot_id") == group[-1]["id"]
                and group[-1]["segmentId"] == item["segmentId"]
                and sum(x["duration_seconds"] for x in group) + item["duration_seconds"] <= max_seconds + 1e-9):
            group.append(item)
        else:
            groups.append([item])
    clip_of = {}; merged = []
    for group in groups:
        first = group[0]
        if len(group) == 1:
            clip = dict(first); clip["shot_ids"] = [first["id"]]
        else:
            refs, seen = [], set()
            for x in group:
                for ref in x.get("reference_assets") or []:
                    if ref.get("path") not in seen: seen.add(ref.get("path")); refs.append(dict(ref))
            refs = [dict(r, no=i) for i, r in enumerate(refs[:5], 1)]
            ids = [x["id"] for x in group]
            clip = dict(first); clip.update(id=f"{first['id']}-{ids[-1].rsplit('-', 1)[-1]}", shot_ids=ids,
                shot_indexes=[x["shotIndex"] for x in group], duration_seconds=round(sum(x["duration_seconds"] for x in group), 2),
                reference_assets=refs, scene_fixed=any(x.get("scene_fixed") for x in group))
            all_refs, seen_all = [], set()
            for x in group:
                for ref in x.get("reference_assets_all") or x.get("reference_assets") or []:
                    if ref.get("path") not in seen_all: seen_all.add(ref.get("path")); all_refs.append(dict(ref))
            clip["reference_assets_all"] = [dict(r, no=i) for i, r in enumerate(all_refs[:15], 1)]
            if protocol == "seedance":
                clip["prompt"] = f"本次视频由连续 {len(group)} 个镜头组成，按顺序在同一条视频内完成，镜头之间自然衔接，不要重复首帧画面：\n\n" + "\n\n".join(x["prompt"] for x in group)
        for sid in clip["shot_ids"]: clip_of[sid] = clip["id"]
        merged.append(clip)
    for clip in merged:
        if clip.get("previous_shot_id"): clip["previous_shot_id"] = clip_of.get(clip["previous_shot_id"], clip["previous_shot_id"])
    return merged


def _collect(ctx: Context, eps: Optional[list], protocol: str, visual_style: str, shots: Optional[list] = None, visual_style_prompt: str = "", merge_seconds: float = 0) -> dict:
    source = ctx.artifact("storyboard")
    if not source.is_file(): raise FileNotFoundError(f"找不到分镜文件：{source}；请先运行 storyboard 阶段")
    board = read_json(source); wanted = set(eps or []); items = []; previous = None
    for episode in board.get("episodes", []):
        ep = int(episode.get("ep", 0))
        if wanted and ep not in wanted: continue
        for seg_index, segment in enumerate(episode.get("segments", []), 1):
            sid = str(segment.get("id") or f"E{ep:02d}-{seg_index:02d}"); cuts = segment.get("cuts") or []
            for cut_index, cut in enumerate(cuts, 1):
                shot_id = f"{sid}-C{cut_index:02d}"
                try: duration = round(float(cut.get("seconds", 0) or 0), 2)
                except (TypeError, ValueError): duration = 0
                if duration <= 0: raise ValueError(f"镜头 {shot_id} 的 seconds 必须大于 0")
                refs = _reference_assets(ctx, ep, segment, cut)
                refs_all = _reference_assets(ctx, ep, segment, cut, limit=15)
                current = {"id": shot_id, "ep": ep, "sceneIndex": segment.get("sceneIndex"), "segmentId": sid,
                           "shotIndex": cut_index, "frame_ref": f"{sid}/f{cut_index}.png", "duration_seconds": duration,
                           "reference_assets": refs, "reference_assets_all": refs_all, "continuity": cut.get("continuity") or {},
                           "continuity_mode": str(segment.get("continuityMode") or ""),
                           "prompt": _shot_prompt(segment, cut, cut_index, visual_style, visual_style_prompt) if protocol == "seedance" else _prompt(segment, protocol),
                           "state": "planned", "task_id": None, "video_url": None, "video_path": None, "last_frame_path": None, "error": None}
                continuous, reason = _is_continuous(previous, current)
                current["input_mode"] = "previous_last_frame" if continuous else "scene_or_assets"
                current["continuity_decision"] = reason
                current["previous_shot_id"] = previous.get("id") if continuous and previous else None
                scene = _asset(refs, "scene")
                current["scene_fixed"] = bool(scene)
                current["initial_reference"] = "previous_last_frame" if continuous else ("scene_reference" if scene else "none")
                items.append(current); previous = current
    if shots:
        requested = {str(v).strip() for v in shots if str(v).strip()}; unknown = requested - {i["id"] for i in items}
        if unknown: raise ValueError("找不到指定镜头 ID：" + "、".join(sorted(unknown)))
    if not items: raise ValueError("所选集没有可生成的视频镜头")
    items = _merge_clips(items, protocol, merge_seconds)
    return {"version": 3, "source": ctx.title, "protocol": protocol, "visual_style": visual_style,
            "visual_style_prompt": str(visual_style_prompt or "").strip(),
            "visual_style_instruction": visual_style_instruction(visual_style, visual_style_prompt),
            "aspect_ratio": board.get("aspectRatio", "16:9"),
            "continuity_rule": "explicit inherit + same episode + same scene + immediately previous shot", "items": items}


def _config(model_key: Optional[str] = None) -> list:
    path = models_file_path()
    if not path.is_file(): raise ValueError(f"找不到模型配置文件：{path}；请先复制 models.example.json 为 models.json")
    data = json.loads(path.read_text(encoding="utf-8-sig")); entries = data.get("video", [])
    if not isinstance(entries, list) or not entries: raise ValueError(f"{path} 中必须配置非空的 video 列表")
    if model_key:
        matched = [e for e in entries if isinstance(e, dict) and model_key in (str(e.get("key") or ""), str(e.get("name") or ""), str(e.get("model") or ""))]
        if not matched: raise ValueError(f"models.json 的 video 列表中找不到模型 {model_key!r}")
        entries = [e for e in matched if e.get("enabled", True) is not False]
        if not entries: raise ValueError(f"视频模型 {model_key!r} 在 models.json 里是 enabled=false；请改成 true 后再用 --video-model 指定")
    else:
        enabled = [e for e in entries if isinstance(e, dict) and e.get("enabled", True) is not False]; active = [e for e in enabled if e.get("active") is True]; entries = active or enabled
    providers = []
    for index, entry in enumerate(entries, 1):
        name = str(entry.get("name") or f"video-{index}").strip(); key = str(entry.get("key") or name).strip(); adapter = str(entry.get("adapter") or "generic").strip().lower()
        submit_url = str(entry.get("submit_url") or "").strip(); status_template = str(entry.get("status_url_template") or "").strip()
        workspace_env = str(entry.get("workspace_id_env") or "").strip(); workspace_id = str(entry.get("workspace_id") or (os.environ.get(workspace_env, "") if workspace_env else "")).strip()
        if "{workspace_id}" in submit_url.lower(): submit_url = re.sub(r"\{workspace_id\}", workspace_id, submit_url, flags=re.I)
        if "{workspace_id}" in status_template.lower(): status_template = re.sub(r"\{workspace_id\}", workspace_id, status_template, flags=re.I)
        model = str(entry.get("model") or "").strip(); api_key = str(entry.get("api_key") or "").strip()
        if not api_key and entry.get("api_key_env"): api_key = os.environ.get(str(entry["api_key_env"]), "").strip()
        comfy = None
        if adapter == "comfyui":
            from ..config import REPO_ROOT
            base_url = str(entry.get("base_url") or "").strip().rstrip("/")
            if not base_url: raise ValueError(f"视频模型 {key}（{name}）缺少：base_url（ComfyUI 服务器地址，如 https://xxx.trycloudflare.com）")
            if urlparse(base_url).scheme not in ("http", "https"): raise ValueError(f"视频模型 {key}（{name}）base_url 必须是 HTTP(S) 地址")
            def _wf(value):
                value = str(value or "").strip()
                if not value: return None
                path = Path(value)
                resolved = path if path.is_absolute() else REPO_ROOT / path
                return str(resolved)
            comfy = {"base_url": base_url, "headers": entry.get("headers") or {}, "workflow_ref": _wf(entry.get("workflow_ref")), "workflow_i2v": _wf(entry.get("workflow_i2v")),
                     "max_refs": int(entry.get("max_refs", 6)), "size_preset": entry.get("size_preset"), "max_seconds": entry.get("max_seconds"),
                     "nodes": entry.get("nodes") or {}, "timeout": max(10, int(entry.get("timeout", 120))), "poll": max(1, float(entry.get("poll_interval", 10))),
                     "max_wait": max(60, int(entry.get("max_wait", 3600)))}
            submit_url, model, api_key = base_url + "/prompt", model or "comfyui", api_key or "-"
        missing = [n for n, v in (("submit_url", submit_url), ("model", model), ("api_key", api_key)) if not v]
        if adapter == "aliyun_wan" and not workspace_id: missing.append("workspace_id 或 workspace_id_env")
        if missing: raise ValueError(f"视频模型 {key}（{name}）缺少：{'、'.join(missing)}")
        providers.append({"key": key, "name": name, "adapter": adapter, "VIDEO_API_SUBMIT_URL": submit_url, "VIDEO_API_KEY": api_key,
                          "VIDEO_API_MODEL": model, "status_template": status_template, "timeout": max(10, int(entry.get("timeout", 120))),
                          "poll": max(1, float(entry.get("poll_interval", 5))), "max_wait": max(10, int(entry.get("max_wait", 900))),
                          "resolution": entry.get("resolution", "720P"), "ratio": entry.get("ratio", "16:9"), "prompt_extend": bool(entry.get("prompt_extend", True)),
                          "request_template": entry.get("request_template"), "size": entry.get("size", "1280x720"), "quality": entry.get("quality", "standard"),
                          "with_audio": bool(entry.get("with_audio", True)), "fps": int(entry.get("fps", 24)), "headers": entry.get("headers") or {},
                          "image_encoding": entry.get("image_encoding", "data_uri"), "video_url_keys": entry.get("video_url_keys") or ["video_url", "videoUrl", "output_url", "download_url"],
                          "task_id_keys": entry.get("task_id_keys") or ["task_id", "taskId", "job_id", "jobId", "id"], "status_keys": entry.get("status_keys") or ["status", "state"],
                          "failed_states": entry.get("failed_states") or ["failed", "FAIL", "error", "ERROR"], "mode": entry.get("mode"), "comfyui": comfy})
    return providers


def _headers(provider: dict) -> dict:
    headers = {"Content-Type": "application/json"}; headers.update(provider.get("headers") or {})
    if provider.get("VIDEO_API_KEY"): headers.setdefault("Authorization", "Bearer " + provider["VIDEO_API_KEY"])
    if provider.get("adapter") == "aliyun_wan": headers["X-DashScope-Async"] = "enable"
    return headers


def _request(url: str, provider: dict, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=_headers(provider), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=provider["timeout"]) as response: data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc: raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:1000]}") from exc
    except urllib.error.URLError as exc: raise RuntimeError(f"网络请求失败：{exc}") from exc
    if not isinstance(data, dict): raise RuntimeError("接口返回 JSON 不是对象")
    return data


def _request_get(url: str, provider: dict) -> dict:
    headers = {"Accept": "application/json"};
    if provider.get("VIDEO_API_KEY"): headers["Authorization"] = "Bearer " + provider["VIDEO_API_KEY"]
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=provider["timeout"]) as response: data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc: raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:1000]}") from exc
    except urllib.error.URLError as exc: raise RuntimeError(f"网络请求失败：{exc}") from exc
    if not isinstance(data, dict): raise RuntimeError("接口返回 JSON 不是对象")
    return data


def _find(data: object, keys: tuple) -> Optional[str]:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value not in (None, "") and isinstance(value, (str, int, float)): return str(value)
        for value in data.values():
            found = _find(value, keys)
            if found: return found
    elif isinstance(data, list):
        for value in data:
            found = _find(value, keys)
            if found: return found
    return None


def _poll(task_id: str, provider: dict) -> dict:
    if not provider.get("status_template"): raise RuntimeError(f"平台 {provider['name']} 未配置 status_url_template")
    url = provider["status_template"].format(task_id=urllib.parse.quote(task_id, safe="")); deadline = time.time() + provider["max_wait"]
    while time.time() < deadline:
        data = _request_get(url, provider); status = str(_find(data, tuple(provider["status_keys"])) or "").strip()
        if status in set(map(str, provider["failed_states"])): raise RuntimeError(f"视频任务 {task_id} 失败：{data}")
        if _find(data, tuple(provider["video_url_keys"])): return data
        time.sleep(provider["poll"])
    raise TimeoutError(f"视频任务 {task_id} 超时：{provider['max_wait']} 秒")


def _resolve(base: Path, value: str) -> Path:
    p = Path(value); return p if p.is_absolute() else base / p


def _encode(path: Path, encoding: str = "data_uri") -> str:
    from .video_adapters import encode_image
    return encode_image(path, encoding)


def _extract_last_frame(video_path: Path, target: Path) -> None:
    if not shutil.which("ffmpeg"): raise RuntimeError("生成下一镜首关键帧需要 ffmpeg；请安装 ffmpeg 并确保它在 PATH 中")
    target.parent.mkdir(parents=True, exist_ok=True); temp = target.with_suffix(".tmp.png")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-sseof", "-1", "-i", str(video_path), "-frames:v", "1", "-update", "1", str(temp)]
    print(f"FFmpeg 提取视频最后一帧命令：{subprocess.list2cmdline(cmd)}", flush=True)
    try: result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc: raise RuntimeError(f"提取视频最后一帧失败：{exc}") from exc
    if result.returncode != 0 or not temp.is_file() or temp.stat().st_size == 0: raise RuntimeError(f"提取视频最后一帧失败：{result.stderr[-1000:]}")
    temp.replace(target)


def _download(url: str, target: Path, provider: dict) -> None:
    if urlparse(url).scheme not in ("http", "https"): raise RuntimeError("视频 URL 必须为 HTTP(S)")
    target.parent.mkdir(parents=True, exist_ok=True); headers = {"Accept": "video/mp4,video/*,application/octet-stream"}
    if provider.get("adapter") != "aliyun_wan" and provider.get("VIDEO_API_KEY"): headers["Authorization"] = "Bearer " + provider["VIDEO_API_KEY"]
    req = urllib.request.Request(url, headers=headers, method="GET"); temp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=provider["timeout"]) as response, temp.open("wb") as stream:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk: break
                stream.write(chunk)
        if temp.stat().st_size <= 0: raise RuntimeError("视频下载后文件为空")
        temp.replace(target)
    except urllib.error.HTTPError as exc: raise RuntimeError(f"视频下载 HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}") from exc
    finally: temp.unlink(missing_ok=True)


def _reference_media(item: dict, root: Path, previous_last: Optional[Path]) -> list[dict]:
    media = []
    # Wan 2.7 R2V explicitly supports one first_frame plus up to five reference images/videos.
    if previous_last:
        media.append({"type": "first_frame", "url": _encode(previous_last), "label": "上一镜视频最后一帧（本镜首关键帧）"})
    else:
        scene = _asset(item.get("reference_assets") or [], "scene")
        if scene:
            scene_path = _resolve(root, str(scene.get("path") or ""))
            if scene_path.is_file():
                media.append({"type": "first_frame", "url": _encode(scene_path), "label": "固定场景首关键帧"})
    for asset in item.get("reference_assets") or []:
        if asset.get("kind") in ("previous", "scene"): continue
        path = _resolve(root, str(asset.get("path") or ""))
        if not path.is_file(): continue
        kind = asset.get("kind"); label = ("角色「" if kind == "character" else "道具「" if kind == "prop" else "参考图「") + str(asset.get("name") or "") + "」"
        media.append({"type": "reference_image", "url": _encode(path), "label": label})
    if len(media) > 6: raise ValueError(f"镜头 {item['id']} 需要 {len(media)} 张媒体，超过接口上限")
    return media


def _wan3_media(item: dict, root: Path, previous_last: Optional[Path]) -> list[dict]:
    # Wan 3.0 does not allow first_frame/last_frame mixed with reference_image.
    # Preserve strict continuity by sending only the previous shot's last frame on continuous shots.
    if previous_last:
        return [{"type": "first_frame", "url": _encode(previous_last), "label": "上一镜视频最后一帧（本镜首关键帧）"}]
    media = []
    for asset in item.get("reference_assets") or []:
        path = _resolve(root, str(asset.get("path") or ""))
        if not path.is_file():
            continue
        kind = asset.get("kind")
        label = ("场景「" if kind == "scene" else "角色「" if kind == "character" else "道具「" if kind == "prop" else "参考图「") + str(asset.get("name") or "") + "」"
        media.append({"type": "reference_image", "url": _encode(path), "label": label})
    if len(media) > 10:
        raise ValueError(f"镜头 {item['id']} 需要 {len(media)} 张参考图，超过 Wan 3.0 的 10 张上限")
    return media


def _build_body(provider: dict, item: dict, manifest: dict, root: Path, previous_last: Optional[Path]) -> dict:
    from .video_adapters import build_request
    request_item = dict(item); adapter = provider.get("adapter")
    model = str(provider.get("VIDEO_API_MODEL") or "").lower()
    is_wan_r2v = adapter == "aliyun_wan" and (str(provider.get("mode") or "").lower() == "r2v" or "r2v" in model)
    is_wan3 = adapter == "aliyun_wan" and "wan3.0-video" in model
    if is_wan_r2v:
        media = _reference_media(item, root, previous_last)
        if not media: raise ValueError(f"镜头 {item['id']} 没有可用参考图；固定场景需要场景图，连续镜头需要上一镜最后一帧")
        request_item["reference_media"] = media
    elif is_wan3:
        media = _wan3_media(item, root, previous_last)
        if media:
            request_item["reference_media"] = media
        elif item.get("input_mode") == "previous_last_frame":
            raise FileNotFoundError(f"镜头 {item['id']} 缺少上一镜最后一帧：{item.get('previous_shot_id')}")
    else:
        images = []
        if previous_last: images.append(_encode(previous_last, provider.get("image_encoding", "data_uri")))
        else:
            scene = _asset(item.get("reference_assets") or [], "scene")
            if scene:
                scene_path = _resolve(root, str(scene.get("path") or ""))
                if scene_path.is_file(): images.append(_encode(scene_path, provider.get("image_encoding", "data_uri")))
        request_item["images"] = images
        if not images and item.get("input_mode") == "previous_last_frame": raise FileNotFoundError(f"镜头 {item['id']} 缺少上一镜最后一帧：{item.get('previous_shot_id')}")
    return build_request(provider, request_item, manifest)


def _submit_one(item: dict, manifest: dict, providers: list, video_root: Path, manifest_path: Path, previous_last: Optional[Path]) -> Optional[Path]:
    if item.get("state") == "succeeded" and item.get("video_path"):
        existing = _resolve(video_root, str(item["video_path"]))
        if existing.is_file() and item.get("last_frame_path"):
            last = _resolve(video_root, str(item["last_frame_path"]))
            if last.is_file(): return last
    failures = []
    if not providers:
        item["state"], item["error"] = "failed", "没有可用的视频平台：models.json 的 video 列表里没有 enabled 的模型"; write_json(manifest_path, manifest); return None
    for provider in providers:
        item["state"], item["provider"], item["error"] = "submitting", provider["name"], None; write_json(manifest_path, manifest)
        try:
            if provider.get("adapter") == "comfyui":
                from . import video_comfyui
                target_base = video_root / "videos" / _safe(item["id"])
                target, note = video_comfyui.generate(item, manifest, provider["comfyui"], video_root.parent, video_root / "work", previous_last, target_base)
                last = video_root / "last-frames" / (_safe(item["id"]) + ".png"); _extract_last_frame(target, last)
                item.update(state="succeeded", video_url=None, video_path=str(target.relative_to(video_root)), last_frame_path=str(last.relative_to(video_root)), error=None, comfy_mode=note); write_json(manifest_path, manifest)
                print(f"视频完成：{item['id']}（ComfyUI {note}）\n视频：{target.resolve()}\n最后一帧：{last.resolve()}", flush=True); return last
            body = _build_body(provider, item, manifest, video_root.parent, previous_last)
            submitted_prompt = body.get("input", {}).get("prompt") if isinstance(body.get("input"), dict) else body.get("prompt")
            print(f"\n===== 视频镜头 {item['id']} | 输入模式={item['input_mode']} | 判断={item['continuity_decision']} | 模型={provider['name']} =====\n{submitted_prompt or item['prompt']}\n===== 提示词结束 =====\n", flush=True)
            response = _request(provider["VIDEO_API_SUBMIT_URL"], provider, body)
            url = _find(response, tuple(provider["video_url_keys"])); task_id = _find(response, tuple(provider["task_id_keys"]))
            if task_id and not url:
                item["task_id"], item["state"] = task_id, "polling"; write_json(manifest_path, manifest); response = _poll(task_id, provider); url = _find(response, tuple(provider["video_url_keys"]))
            if not url: raise RuntimeError("响应中找不到 video_url/output_url 或 task_id")
            target = video_root / "videos" / (_safe(item["id"]) + ".mp4"); _download(url, target, provider)
            last = video_root / "last-frames" / (_safe(item["id"]) + ".png"); _extract_last_frame(target, last)
            item.update(state="succeeded", video_url=url, video_path=str(target.relative_to(video_root)), last_frame_path=str(last.relative_to(video_root)), error=None); write_json(manifest_path, manifest)
            print(f"视频完成：{item['id']}\n视频：{target.resolve()}\n最后一帧：{last.resolve()}", flush=True); return last
        except Exception as exc:
            failures.append(f"{provider['name']}: {exc}"); item["error"] = str(exc)
    item["state"], item["error"] = "failed", "；".join(failures); write_json(manifest_path, manifest); return None


def check(ctx: Context) -> Result:
    path = artifact(ctx)
    if not path.is_file(): return Result(1, "视频任务清单尚未生成")
    items = read_json(path).get("items", []); pending = [x for x in items if x.get("state") != "succeeded"]
    return Result(0 if items and not pending else 1, f"{len(items)} 个视频镜头已生成" if not pending and items else f"{len(pending)}/{len(items)} 个视频镜头尚未生成")


def run(ctx: Context, eps: Optional[list] = None, protocol: str = "h3", submit: bool = False, visual_style: str = "manju",
        visual_style_prompt: str = "", shots: Optional[list] = None, video_model: Optional[str] = None, frames_dir: Optional[Path] = None, flf: bool = False,
        segments: Optional[list] = None, merge_seconds: float = 10) -> Path:
    if protocol == "i2v": raise ValueError("旧的 --video-protocol i2v 已取消：视频阶段现在直接按连续镜头规则生成")
    root = ctx.out / "video"; root.mkdir(parents=True, exist_ok=True); path = artifact(ctx); manifest = _collect(ctx, eps, protocol, visual_style, shots, visual_style_prompt, merge_seconds)
    previous = {}
    if path.is_file() and not ctx.force:
        try: previous = {str(x.get("id")): x for x in read_json(path).get("items", [])}
        except (OSError, ValueError, TypeError): previous = {}
    for item in manifest["items"]:
        old = previous.get(item["id"])
        if old and old.get("prompt") == item.get("prompt") and old.get("input_mode") == item.get("input_mode") and old.get("continuity_decision") == item.get("continuity_decision"):
            for key in ("state", "task_id", "provider", "video_url", "video_path", "last_frame_path", "error"):
                if key in old: item[key] = old[key]
    write_json(path, manifest); prompts = root / "prompts"; prompts.mkdir(exist_ok=True)
    for item in manifest["items"]: (prompts / (_safe(item["id"]) + ".txt")).write_text(item["prompt"] + "\n", encoding="utf-8")
    qc = ["# 视频人工质检清单", "", "- [ ] 角色外观、服装与参考图一致", "- [ ] 连续镜头首帧使用上一镜真实最后一帧", "- [ ] 新场景不错误继承上一镜最后一帧", "- [ ] 没有字幕、水印或多余人物", "- [ ] 时长和画幅符合交付要求", "", "## 镜头状态", ""]
    qc += [f"- [ ] {x['id']}：{x['state']} | {x['input_mode']} | {x['continuity_decision']}" for x in manifest["items"]]; (root / "qc-checklist.md").write_text("\n".join(qc) + "\n", encoding="utf-8")
    ctx.log(f"视频阶段：已生成 {len(manifest['items'])} 个镜头清单：{path}")
    if not submit: return path
    providers = _config(video_model); requested = {str(v).strip() for v in shots or [] if str(v).strip()}
    if requested: requested |= {x["id"] for x in manifest["items"] if requested & set(x.get("shot_ids") or [])}
    seg_ids = {str(v).strip() for v in segments or [] if str(v).strip()}
    if seg_ids:
        known = {str(x.get("segmentId")) for x in manifest["items"]}
        if seg_ids - known: raise ValueError("找不到指定分段 ID：" + "、".join(sorted(seg_ids - known)) + "；分段 ID 形如 E01-01")
        requested |= {x["id"] for x in manifest["items"] if str(x.get("segmentId")) in seg_ids}
    for item in manifest["items"]:
        if requested and item["id"] not in requested: continue
        last_frame = None
        if item["input_mode"] == "previous_last_frame":
            previous_item = next((x for x in manifest["items"] if x["id"] == item.get("previous_shot_id")), None)
            if previous_item and previous_item.get("last_frame_path"): last_frame = _resolve(root, previous_item["last_frame_path"])
            if not last_frame or not last_frame.is_file(): raise RuntimeError(f"镜头 {item['id']} 必须先生成上一个连续镜头 {item.get('previous_shot_id')}，才能取得真实最后一帧")
        generated = _submit_one(item, manifest, providers, root, path, last_frame if item["input_mode"] == "previous_last_frame" else None)
        if item.get("state") != "succeeded": raise RuntimeError(f"镜头 {item['id']} 生成失败：{item.get('error')}")
    return path
