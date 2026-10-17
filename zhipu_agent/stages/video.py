"""Optional cloud video generation stage. Preview is the default; --video-submit is required for API calls."""
from __future__ import annotations
import json, os, re, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from ..validator import Result
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


def _collect(ctx: Context, eps: Optional[list], protocol: str) -> dict:
    source = ctx.artifact("storyboard")
    if not source.is_file():
        raise FileNotFoundError(f"找不到分镜文件：{source}；请先运行 storyboard 阶段")
    board = read_json(source)
    wanted, items = set(eps or []), []
    for episode in board.get("episodes", []):
        ep = int(episode.get("ep", 0))
        if wanted and ep not in wanted:
            continue
        for idx, segment in enumerate(episode.get("segments", []), 1):
            sid = str(segment.get("id") or f"E{ep:02d}-{idx:02d}")
            items.append({"id": sid, "ep": ep, "sceneIndex": segment.get("sceneIndex"),
                          "duration_seconds": _duration(segment), "prompt": _prompt(segment, protocol),
                          "state": "planned", "task_id": None, "video_url": None,
                          "video_path": None, "error": None})
    if not items:
        raise ValueError("所选集没有可生成的视频分段；检查分镜产物或 --eps 参数")
    return {"version": 1, "source": ctx.title, "protocol": protocol,
            "aspect_ratio": board.get("aspectRatio", "16:9"), "items": items}


def _config() -> dict:
    cfg = {k: os.environ.get(k, "").strip() for k in
           ("VIDEO_API_SUBMIT_URL", "VIDEO_API_KEY", "VIDEO_API_MODEL")}
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise ValueError("提交前必须配置：" + "、".join(missing))
    if urlparse(cfg["VIDEO_API_SUBMIT_URL"]).scheme not in ("http", "https"):
        raise ValueError("VIDEO_API_SUBMIT_URL 必须是 HTTP(S) URL")
    cfg.update(status_template=os.environ.get("VIDEO_API_STATUS_URL_TEMPLATE", "").strip(),
               timeout=max(10, int(os.environ.get("VIDEO_API_TIMEOUT", "120"))),
               poll=max(1, float(os.environ.get("VIDEO_API_POLL_INTERVAL", "5"))),
               max_wait=max(10, int(os.environ.get("VIDEO_API_MAX_WAIT", "900"))))
    return cfg


def _request(url: str, cfg: dict, body: Optional[dict] = None) -> dict:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    headers = {"Authorization": "Bearer " + cfg["VIDEO_API_KEY"], "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
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
        if _find(result, ("video_url", "videoUrl", "output_url", "download_url")):
            return result
        state = (_find(result, ("status", "state", "task_status")) or "").lower()
        if state in ("failed", "error", "cancelled", "canceled", "rejected"):
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


def _submit(item: dict, manifest: dict, cfg: dict, root: Path, path: Path) -> None:
    if item.get("state") == "succeeded" and item.get("video_url"):
        return
    try:
        if item.get("task_id") and cfg["status_template"]:
            item["state"] = "polling"
            write_json(path, manifest)
            result = _poll(str(item["task_id"]), cfg)
        else:
            item["state"], item["error"] = "submitting", None
            write_json(path, manifest)
            body = {"model": cfg["VIDEO_API_MODEL"], "prompt": item["prompt"],
                    "duration": item["duration_seconds"], "aspect_ratio": manifest["aspect_ratio"]}
            result = _request(cfg["VIDEO_API_SUBMIT_URL"], cfg, body)
            url = _find(result, ("video_url", "videoUrl", "output_url", "download_url"))
            task_id = _find(result, ("task_id", "taskId", "job_id", "jobId", "id"))
            if task_id:
                item["task_id"] = task_id
            if not url and task_id:
                item["state"] = "polling"
                write_json(path, manifest)
                result = _poll(task_id, cfg)
            elif not url:
                raise RuntimeError("响应中找不到 video_url、output_url 或 task_id；需适配该平台响应格式")
        url = _find(result, ("video_url", "videoUrl", "output_url", "download_url"))
        if not url:
            raise RuntimeError("任务结束但响应中没有视频 URL")
        item.update(state="succeeded", video_url=url, error=None)
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


def run(ctx: Context, eps: Optional[list] = None, protocol: str = "h3", submit: bool = False) -> Path:
    root = ctx.out / "video"
    root.mkdir(parents=True, exist_ok=True)
    path = artifact(ctx)
    manifest = _collect(ctx, eps, protocol)
    previous = {}
    if path.is_file() and not ctx.force:
        try:
            old = read_json(path)
            if old.get("protocol") == protocol:
                previous = {str(x.get("id")): x for x in old.get("items", [])}
        except (OSError, ValueError):
            pass
    for item in manifest["items"]:
        old = previous.get(item["id"])
        if old and old.get("prompt") == item["prompt"]:
            for key in ("state", "task_id", "video_url", "video_path", "error", "download_error"):
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
    cfg = _config()
    for item in manifest["items"]:
        if item.get("state") == "succeeded" and item.get("video_url"):
            continue
        ctx.log(f"视频任务：{item['id']}（{item['duration_seconds']} 秒）")
        _submit(item, manifest, cfg, root, path)
        ctx.log(f"  状态：{item['state']}" + (f"；错误：{item['error']}" if item.get("error") else ""))
    return path
