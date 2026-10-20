"""分镜图批量出图阶段（frames）。

读 storyboard.json 里每个分镜的 `frame`（分镜图提示词），连同参考图一起交给出图模型：
  - 角色参考图：来自 images 阶段的角色图（output/images/characters/…）
  - 场景参考图：分镜所属场次对应的场景设定图（output/art/images/…-sheet.*）
  - 道具参考图：分镜里出现的叙事道具图（output/images/props/…）
产物：output/frames/<段号>/f<切序>.png（与视频阶段 i2v 的首帧约定一致）和 output/frames/frames-manifest.json。

分镜编号 = 视频阶段的镜头 ID：E01-01-C01（第 1 集第 1 段第 1 个分镜）。
不加 --frames-gen 时只整理任务清单和参考图引用，不调用出图 API。
"""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..validator import Result
from .base import Context, read_json, safe_name, write_json
from .images import _download_image, _extension, _find_image_payload
from .video_adapters import _render

PRINT_FRAME_REQUEST = True          # 打印出图请求（参考图只显示数量和文件名，不打印 Base64）
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
KIND_LABEL = {"character": "角色", "scene": "场景", "prop": "道具", "previous": "上一镜参考帧"}
KIND_ROLE = {"character": "的外观（脸型、发型、服装）", "scene": "的空间布局与光线", "prop": "的外形",
             "previous": "，只用来继承人物身份、服装、姿态、道具持握和空间关系；本镜构图、机位以下面的描述为准，不要照搬上一镜的机位"}


def artifact(ctx: Context) -> Path:
    return ctx.out / "frames" / "frames-manifest.json"


# ---------------------------------------------------------------- 配置

def _providers() -> List[dict]:
    """分镜图和角色图、场景图共用 models.json 的 image 号池（同一个接口），不另设配置。

    image 条目里可选追加这些字段（都不必填）：
      sizes             按画幅选尺寸，如 {"16:9": "1536x1024", "9:16": "1024x1536"}
      reference_field   参考图放在请求体的哪个字段，默认 "image"
      reference_style   list（字符串数组，默认）| single（只传第 1 张）| objects（[{"image_url": ...}]）
      image_encoding    data_uri（默认）| base64
      max_references    最多传几张参考图，默认 10
      request_template  请求体结构特殊时整体指定，可用 {{model}} {{prompt}} {{size}} {{quality}} {{images}} {{image}}
      headers           额外请求头
    """
    from .images import _image_providers
    providers = []
    for base in _image_providers():
        entry = base.get("raw") or {}
        style = str(entry.get("reference_style") or "list")
        if style not in ("list", "single", "objects"):
            raise ValueError(f"models.json 的 image（{base['name']}）reference_style 只能是 list / single / objects")
        encoding = str(entry.get("image_encoding") or "data_uri")
        if encoding not in ("data_uri", "base64"):
            raise ValueError(f"models.json 的 image（{base['name']}）image_encoding 只能是 data_uri / base64")
        providers.append({
            **{k: base[k] for k in ("name", "url", "api_key", "model", "size", "quality", "timeout")},
            "sizes": entry.get("sizes") if isinstance(entry.get("sizes"), dict) else {},
            "reference_field": str(entry.get("reference_field") or "image"),
            "reference_style": style, "image_encoding": encoding,
            "max_references": max(0, int(entry.get("max_references", 10))),
            "request_template": entry.get("request_template"),
            "headers": entry.get("headers") if isinstance(entry.get("headers"), dict) else {},
            "adapter": str(entry.get("adapter") or "openai_image").strip().lower(),
            "raw": entry,
        })
    return providers


def _size_for(provider: dict, ratio: str) -> str:
    return str(provider["sizes"].get(ratio) or provider["size"])


def _encode(path: Path, encoding: str) -> str:
    raw = path.read_bytes()
    ext = path.suffix.lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext or 'png'}"
    b64 = base64.b64encode(raw).decode("ascii")
    return b64 if encoding == "base64" else f"data:{mime};base64,{b64}"


def build_request(provider: dict, prompt: str, size: str, images: List[str]) -> dict:
    """组装出图请求体。支持 request_template（{{model}} {{prompt}} {{size}} {{quality}} {{images}} {{image}}）。"""
    template = provider.get("request_template")
    if isinstance(template, dict) and template:
        return _render(template, {"model": provider["model"], "prompt": prompt, "size": size,
                                  "quality": provider.get("quality") or "", "images": images,
                                  "image": images[0] if images else ""})
    body: Dict[str, Any] = {"model": provider["model"], "prompt": prompt, "size": size, "n": 1}
    if provider.get("quality"):
        body["quality"] = provider["quality"]
    if images:
        style = provider["reference_style"]
        value: Any = images[0] if style == "single" else \
            [{"image_url": i} for i in images] if style == "objects" else list(images)
        body[provider["reference_field"]] = value
    return body


def _post(provider: dict, body: dict) -> dict:
    headers = {"Authorization": "Bearer " + provider["api_key"], "Content-Type": "application/json",
               "Accept": "application/json"}
    headers.update({str(k): str(v) for k, v in provider["headers"].items()})
    req = urllib.request.Request(provider["url"], data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=provider["timeout"]) as response:
            raw = response.read()
            if PRINT_FRAME_REQUEST:
                try:
                    response_text = raw.decode("utf-8", "replace")
                except Exception:
                    response_text = repr(raw[:1000])
                print(f"[分镜图 API 返回] HTTP {response.status} | 平台={provider.get('name')} | 模型={provider.get('model')}\\n{response_text}", flush=True)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", "replace")
        if PRINT_FRAME_REQUEST:
            print(f"[分镜图 API 错误返回] HTTP {exc.code} | 平台={provider.get('name')} | 模型={provider.get('model')}\\n{error_body[:5000]}", flush=True)
        raise RuntimeError(f"HTTP {exc.code}: {error_body[:700]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"网络请求失败：{exc}") from exc
    try:
        result = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise RuntimeError(f"出图 API 返回的不是 JSON：{raw[:200]!r}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("出图 API 返回的 JSON 顶层必须是对象")
    return result


def _modelscope_generate(provider: dict, prompt: str, refs: List[dict], files: List[Path], size: str) -> bytes:
    """直接读取 output 下已生成的角色、场景、道具参考图，不要求在 models.json 中配置图片 URL。"""
    cfg = provider.get("raw") or {}
    encoded_refs = [_encode(path, "data_uri") for path in files[:provider["max_references"]]]
    payload = {"prompt": prompt, "negative_prompt": str(cfg.get("negative_prompt") or ""),
               "image_size": size, "num_images": 1, "reference_images": encoded_refs,
               "ref_weight": float(cfg.get("ref_weight", 0.68))}
    result = _post(provider, payload)
    if result.get("status_code") not in (None, 200, "200"):
        raise RuntimeError(f"ModelScope 提交失败：{result}")
    task = result.get("result") if isinstance(result.get("result"), dict) else {}
    task_id = task.get("task_id")
    if not task_id:
        raise RuntimeError(f"ModelScope 响应中找不到 result.task_id：{result}")
    status_url = str(cfg.get("status_url_template") or "https://api-inference.modelscope.cn/api/v1/tasks/{task_id}").format(
        task_id=urllib.parse.quote(str(task_id), safe=""))
    deadline = time.time() + int(cfg.get("max_wait", 900))
    headers = {"Authorization": "Bearer " + provider["api_key"], "Accept": "application/json"}
    while time.time() < deadline:
        req = urllib.request.Request(status_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=provider["timeout"]) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"ModelScope 查询 HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:700]}") from exc
        state = str(data.get("status") or "").upper()
        if state == "FAILED":
            raise RuntimeError(f"ModelScope 生图失败：{data}")
        if state == "SUCCEEDED":
            task_result = data.get("result") if isinstance(data.get("result"), dict) else {}
            images = task_result.get("images") or []
            image_url = images[0].get("url") if images and isinstance(images[0], dict) else None
            if not image_url:
                raise RuntimeError(f"ModelScope 任务成功但没有图片 URL：{data}")
            return _download_image(str(image_url), provider["timeout"])
        time.sleep(max(1, int(cfg.get("poll_interval", 3))))
    raise TimeoutError(f"ModelScope 任务 {task_id} 超过最大等待时间")

def _redacted(body: dict) -> dict:
    def walk(value):
        if isinstance(value, str) and (value.startswith("data:image") or len(value) > 2000):
            return f"<图片数据 {len(value)} 字符>"
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value
    return walk(body)


# ---------------------------------------------------------------- 参考图引用

def _asset_index(ctx: Context) -> Dict[str, Dict[str, dict]]:
    """从 images 阶段的清单里建索引：kind -> {名称或编号: 条目}，只收文件真实存在的。"""
    index: Dict[str, Dict[str, dict]] = {"character": {}, "scene": {}, "prop": {}}
    manifest = ctx.out / "images" / "image-manifest.json"
    if manifest.is_file():
        try:
            items = read_json(manifest).get("items", [])
        except (OSError, ValueError):
            items = []
        for item in items:
            path = item.get("output_path")
            kind = item.get("kind")
            if kind in index and path and (ctx.out / path).is_file():
                for key in (item.get("name"), item.get("asset_id")):
                    if key:
                        index[kind].setdefault(str(key), {"path": str(path), "name": item.get("name") or key})
    return index


def _scene_ids(ctx: Context) -> Dict[tuple, str]:
    """(集号, 场序) -> art.json 场景编号（来自剧本 scenes[].sceneId）。"""
    path = ctx.artifact("script")
    ids: Dict[tuple, str] = {}
    if path.is_file():
        try:
            script = read_json(path)
        except (OSError, ValueError):
            return ids
        for ep in script.get("episodes", []):
            for i, scene in enumerate(ep.get("scenes", []), 1):
                if scene.get("sceneId"):
                    ids[(int(ep.get("ep", 0)), i)] = str(scene["sceneId"])
    return ids


def _references(ctx: Context, ep: int, segment: dict, cut: dict, assets: dict, scene_ids: dict,
                limit: int = 99, prev: Optional[dict] = None) -> tuple[List[dict], List[str]]:
    """返回 (参考图列表, 缺失说明)。顺序：角色 → 场景 → 道具 → 上一镜参考帧（连续镜头时），
    编号从 1 起，对应提示词里的"图 N"。名额不够时先舍弃道具、场景，上一镜参考帧保留，角色优先。"""
    refs: List[dict] = []
    missing: List[str] = []

    def add(kind: str, key: str, label: str) -> None:
        found = assets[kind].get(key)
        if not found:
            missing.append(f"{KIND_LABEL[kind]}「{label}」没有参考图（先运行 images 阶段出{KIND_LABEL[kind]}图）")
            return
        if any(r["path"] == found["path"] for r in refs):
            return
        refs.append({"kind": kind, "name": str(found["name"]), "path": found["path"]})

    for name in cut.get("characters") or []:
        add("character", str(name), str(name))
    scene_id = scene_ids.get((ep, int(segment.get("sceneIndex") or 0)))
    if scene_id:
        add("scene", scene_id, scene_id)
    else:
        missing.append("找不到分镜所属场景（缺剧本 sceneId），未引用场景图")
    for name in cut.get("props") or []:
        add("prop", str(name), str(name))
    room = limit - 1 if prev else limit
    if len(refs) > room:
        missing.append(f"参考图 {len(refs) + (1 if prev else 0)} 张超过接口上限 {limit}，只传前 {room} 张设定图"
                       + ("和上一镜参考帧" if prev else "") + "（角色优先）")
        refs = refs[:max(room, 0)]
    if prev:  # 路径出图时才定：上一镜可能就是本次先生成的，所以这里只记它的 output_base
        found = _existing(ctx, prev["output_base"])
        refs.append({"kind": "previous", "name": prev["id"], "base": prev["output_base"],
                     "path": str(found.relative_to(ctx.out)).replace("\\", "/") if found else prev["output_base"] + ".png"})
    for n, ref in enumerate(refs, 1):
        ref["no"] = n
    return refs, missing


def _reference_header(refs: List[dict]) -> str:
    if not refs:
        return ""
    parts = [f"图{r['no']}是{KIND_LABEL[r['kind']]}「{r['name']}」{KIND_ROLE[r['kind']]}" for r in refs]
    return ("参考图说明：" + "；".join(parts) + "。出图时严格保持这些参考图里的人物外观和场景布局一致，不要新增人物。\n\n")


# ---------------------------------------------------------------- 任务

def _collect(ctx: Context) -> tuple[List[dict], str]:
    source = ctx.artifact("storyboard")
    if not source.is_file():
        raise FileNotFoundError(f"找不到分镜文件：{source}；请先运行 storyboard 阶段")
    board = read_json(source)
    ratio = str(board.get("aspectRatio", "16:9"))
    assets, scene_ids = _asset_index(ctx), _scene_ids(ctx)
    tasks: List[dict] = []
    for episode in board.get("episodes", []):
        ep = int(episode.get("ep", 0))
        for seg_idx, segment in enumerate(episode.get("segments", []), 1):
            sid = str(segment.get("id") or f"E{ep:02d}-{seg_idx:02d}")
            for cut_idx, cut in enumerate(segment.get("cuts", []), 1):
                frame = str(cut.get("frame") or "").strip()
                shot_id = f"{sid}-C{cut_idx:02d}"
                if not frame:
                    raise ValueError(f"分镜 {shot_id} 缺少 frame（分镜图提示词）")
                tasks.append({
                    "id": shot_id, "ep": ep, "segmentId": sid, "shotIndex": cut_idx,
                    "frame_no": f"f{cut_idx}", "frame": frame, "_segment": segment, "_cut": cut,
                    "output_base": f"frames/{safe_name(sid)}/f{cut_idx}",
                    "_first_in_segment": cut_idx == 1,
                })
    for index, task in enumerate(tasks):
        task["_prev"] = _previous_task(tasks, index)
    if not tasks:
        raise ValueError("分镜文件里没有可出图的分镜")
    return tasks, ratio


def _previous_task(tasks: List[dict], index: int) -> Optional[dict]:
    """连续镜头的"上一镜"：cut.continuity.mode == inherit 才有。
    同段取上一切；段首取前一段的末切（段 continuityMode 为 hard_cut 或前面没有镜头时不取）。
    continuity.referenceFrame 若写的是某个已知分镜编号，以它为准。旧分镜没有 continuity 字段时一律不引用。"""
    task = tasks[index]
    continuity = (task["_cut"].get("continuity") or {})
    if continuity.get("mode") != "inherit":
        return None
    named = str(continuity.get("referenceFrame") or "").strip()
    for other in tasks[:index]:
        if named and named in (other["id"], other["output_base"], f"{other['output_base']}.png"):
            return other
    if index == 0:
        return None
    if task["_first_in_segment"] and task["_segment"].get("continuityMode") == "hard_cut":
        return None
    return tasks[index - 1]


def _matches(item: dict, wanted: List[str]) -> bool:
    """--frames-only：分镜编号（E01-01-C01）、段号（E01-01，整段所有分镜）或集号写法 E01。"""
    sid = item["segmentId"]
    return any(w in (item["id"], sid) or w.upper() == f"E{item['ep']:02d}" for w in wanted)


def _resolve_previous(ctx: Context, item: dict, entry: dict) -> tuple[List[dict], str]:
    """出图那一刻再确认上一镜参考帧：文件已经有（可能就是本次刚出的）才用；没有就去掉并重排图号。"""
    refs = [dict(r) for r in item["references"]]
    changed = False
    for ref in list(refs):
        if ref["kind"] != "previous":
            continue
        found = _existing(ctx, ref["base"])
        if found:
            ref["path"] = str(found.relative_to(ctx.out)).replace("\\", "/")
        else:
            refs.remove(ref)
            changed = True
            note = f"上一镜参考帧「{ref['name']}」还没有生成，本张没有使用（按顺序先出上一镜再出本镜）"
            entry["missing_references"] = [*entry.get("missing_references", []), note]
            ctx.log(f"⚠ {item['id']}: {note}")
    if not changed:
        return refs, item["prompt"]
    for n, ref in enumerate(refs, 1):
        ref["no"] = n
    return refs, _reference_header(refs) + item["frame"]


def _existing(ctx: Context, base: str) -> Optional[Path]:
    for suffix in IMAGE_SUFFIXES:
        p = ctx.out / (base + suffix)
        if p.is_file():
            return p
    return None


def check(ctx: Context) -> Result:
    path = artifact(ctx)
    if not path.is_file():
        return Result(1, "分镜图清单尚未生成")
    try:
        items = read_json(path).get("items", [])
    except (OSError, ValueError) as exc:
        return Result(1, f"分镜图清单不可读：{exc}")
    pending = [x for x in items if not (x.get("state") == "succeeded" and (ctx.out / str(x.get("output_path", ""))).is_file())]
    if not items or pending:
        return Result(1, f"{len(pending)}/{len(items)} 张分镜图尚未生成")
    return Result(0, f"{len(items)} 张分镜图已生成")


def run(ctx: Context, eps: Optional[list] = None, generate: bool = False,
        only: Optional[List[str]] = None) -> Path:
    path = artifact(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    tasks, ratio = _collect(ctx)
    assets, scene_ids = _asset_index(ctx), _scene_ids(ctx)

    if only:
        unknown = [w for w in only if not any(_matches(t, [w]) for t in tasks)]
        if unknown:
            sample = "、".join(t["id"] for t in tasks[:6])
            raise ValueError(f"--frames-only 里的 {'、'.join(unknown)} 找不到对应的分镜。编号格式如 {sample}……")

    previous: Dict[str, dict] = {}
    if path.is_file():
        try:
            previous = {i["id"]: i for i in read_json(path).get("items", [])}
        except (OSError, ValueError, KeyError):
            previous = {}

    providers: List[dict] = _providers() if generate else []
    limit = min([p["max_references"] for p in providers], default=99)
    wanted_eps = set(eps or [])

    items: List[dict] = []
    for task in tasks:
        segment, cut = task.pop("_segment"), task.pop("_cut")
        prev = task.pop("_prev", None)
        task.pop("_first_in_segment", None)
        refs, missing = _references(ctx, task["ep"], segment, cut, assets, scene_ids, limit, prev)
        prompt = _reference_header(refs) + task["frame"]
        digest = hashlib.sha256((prompt + json.dumps([r["path"] for r in refs])).encode("utf-8")).hexdigest()
        old = previous.get(task["id"], {})
        existing = _existing(ctx, task["output_base"])
        picked = (not wanted_eps or task["ep"] in wanted_eps) and (not only or _matches(task, only))
        reuse = old.get("prompt_sha256") == digest and old.get("state") == "succeeded" and existing is not None
        task.update({"references": refs, "missing_references": missing, "prompt": prompt, "prompt_sha256": digest})
        if reuse and not (ctx.force and generate and picked):
            task.update({k: old[k] for k in ("state", "provider", "model", "output_path", "error", "attempts") if k in old})
        else:
            task.update({"state": "pending", "provider": None, "error": None, "attempts": 0,
                         "output_path": (task["output_base"] + ".png")})
        task["picked"] = picked
        items.append(task)

    manifest = {"version": 1, "source": ctx.title, "aspect_ratio": ratio,
                "note": "refs[].no 对应提示词里的“图N”；分镜编号与视频阶段镜头 ID 相同。",
                "items": [{k: v for k, v in i.items() if k != "picked"} for i in items]}
    write_json(path, manifest)

    selected = [i for i in items if i["picked"]]
    warn = [f"{i['id']}: {m}" for i in selected for m in i["missing_references"]]
    for line in warn[:8]:
        ctx.log(f"⚠ {line}")
    if len(warn) > 8:
        ctx.log(f"⚠ 另有 {len(warn) - 8} 条参考图缺失提示（见 frames-manifest.json 的 missing_references）")

    if not generate:
        ctx.log(f"已整理 {len(items)} 个分镜图任务（本次选中 {len(selected)} 个）；没有调用出图 API。")
        ctx.log(f"确认 models.json 的 image 号池（和角色图、场景图共用）后，加 --frames-gen 开始出图：{path}")
        return path

    by_id = {m["id"]: m for m in manifest["items"]}
    for item in selected:
        entry = by_id[item["id"]]
        existing = _existing(ctx, item["output_base"])
        if entry.get("state") == "succeeded" and existing and not ctx.force:
            ctx.log(f"跳过已完成：{item['id']} → {existing.relative_to(ctx.out)}")
            continue
        entry["state"], entry["error"] = "generating", None
        write_json(path, manifest)
        failures: List[str] = []
        refs_now, prompt_now = _resolve_previous(ctx, item, entry)
        for provider in providers:
            try:
                files = [ctx.out / r["path"] for r in refs_now[:provider["max_references"]]]
                size = _size_for(provider, ratio)
                if provider["adapter"] == "modelscope":
                    # ModelScope 的 payload 在 _modelscope_generate 内部组装；此处先打印可读参数。
                    body = None
                    if PRINT_FRAME_REQUEST:
                        cfg = provider.get("raw") or {}
                        ctx.log(f"分镜图 {item['id']} | 平台={provider['name']} | 模型={provider['model']} | "
                                f"适配器=modelscope | 尺寸={size}")
                        ctx.log("完整生图提示词（含参考图说明）：" + prompt_now)
                        ctx.log("请求参数：" + json.dumps({
                            "prompt": prompt_now,
                            "negative_prompt": str(cfg.get("negative_prompt") or ""),
                            "image_size": size,
                            "num_images": 1,
                            "reference_images": [
                                {"kind": r.get("kind"), "name": r.get("name"), "path": str(f),
                                 "encoding": "data_uri", "content": f"<图片数据，{f.stat().st_size} 字节>"}
                                for r, f in zip(refs_now[:provider["max_references"]], files)
                            ],
                            "ref_weight": float(cfg.get("ref_weight", 0.68)),
                        }, ensure_ascii=False, indent=2))
                    data = _modelscope_generate(provider, prompt_now, refs_now, files, size)
                else:
                    images = [_encode(f, provider["image_encoding"]) for f in files]
                    body = build_request(provider, prompt_now, size, images)
                    if PRINT_FRAME_REQUEST:
                        ctx.log(f"分镜图 {item['id']} | 平台={provider['name']} | 模型={provider['model']} | "
                                f"适配器={provider['adapter']} | 尺寸={size}")
                        ctx.log("完整生图提示词（含参考图说明）：" + prompt_now)
                        ctx.log("参考图：" + json.dumps([
                            {"编号": r.get("no"), "类型": KIND_LABEL.get(r.get("kind"), r.get("kind")),
                             "名称": r.get("name"), "路径": r.get("path")}
                            for r in refs_now[:provider["max_references"]]
                        ], ensure_ascii=False))
                        ctx.log("请求参数：" + json.dumps(_redacted(body), ensure_ascii=False, indent=2))
                if provider["adapter"] != "modelscope":
                    result = _post(provider, body)
                    b64, url = _find_image_payload(result)
                    if b64:
                        data = base64.b64decode(b64, validate=True)
                    elif url:
                        data = _download_image(url, provider["timeout"])
                    else:
                        raise RuntimeError("响应中找不到 data[0].b64_json 或图片 URL")
                target = (ctx.out / item["output_base"]).with_suffix(_extension(data))
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix(target.suffix + ".part")
                temp.write_bytes(data)
                temp.replace(target)
                for other in IMAGE_SUFFIXES:  # 换了扩展名时清掉旧的同名分镜图，避免视频阶段读到旧图
                    stale = (ctx.out / item["output_base"]).with_suffix(other)
                    if stale != target and stale.is_file():
                        stale.unlink()
                entry.update(state="succeeded", provider=provider["name"], model=provider["model"],
                             output_path=str(target.relative_to(ctx.out)).replace("\\", "/"), error=None,
                             attempts=int(entry.get("attempts", 0)) + 1)
                break
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{provider['name']}: {exc}")
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
        else:
            entry.update(state="failed", error="；".join(failures))
        write_json(path, manifest)
        ctx.log(f"{'完成' if entry['state'] == 'succeeded' else '失败'}：{item['id']}"
                + (f" → {entry['output_path']}" if entry["state"] == "succeeded" else f" · {entry['error'][:250]}"))

    ctx.log(f"分镜图结果：{check(ctx).stdout}")
    return path
