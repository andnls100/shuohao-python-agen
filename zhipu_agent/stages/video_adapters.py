"""Provider-specific request construction for the video stage."""
from __future__ import annotations

import base64, io, mimetypes, re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _render(value: Any, context: dict) -> Any:
    if isinstance(value, dict): return {k: _render(v, context) for k, v in value.items()}
    if isinstance(value, list): return [_render(v, context) for v in value]
    if not isinstance(value, str): return value
    if value.startswith("{{") and value.endswith("}}") and value.count("{{") == 1:
        return context.get(value[2:-2], value)
    for key, replacement in context.items(): value = value.replace("{{" + key + "}}", str(replacement))
    return value


COGVIDEOX_PROMPT_LIMIT = 512
COGVIDEOX_SIZES = ("1280x720",)
COGVIDEOX_ALL_SIZES = ("1280x720", "720x1280", "1024x1024", "1920x1080", "1080x1920", "2048x1080", "3840x2160")
COGVIDEOX_REFERENCE_RATIOS = ("16:9", "9:16", "1:1")
COGVIDEOX_MOVEMENTS = ("auto", "small", "medium", "large")
IMAGE_LIMIT_BYTES = 5 * 1024 * 1024
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def fit_cogvideox_prompt(prompt: str, limit: int = COGVIDEOX_PROMPT_LIMIT) -> str:
    lines = [x.strip() for x in str(prompt).splitlines() if x.strip()]
    if lines and re.match(r"^(How the reference pictures align|参考图与目标视频的对齐|目标视频在)", lines[0]): lines = lines[1:]
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if len(text) <= limit: return text
    cut = text[:limit]; boundary = max(cut.rfind(x) for x in ("。", "！", "？", ".", "；", "，", ", "))
    return cut[:boundary + 1] if boundary >= limit // 2 else cut


def encode_image(path: Path, encoding: str = "data_uri") -> str:
    path = Path(path)
    if path.suffix.lower() not in IMAGE_EXTS: raise ValueError(f"分镜图格式必须是 png/jpg/jpeg/webp：{path.name}")
    raw = path.read_bytes(); mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if len(raw) > IMAGE_LIMIT_BYTES:
        try:
            from PIL import Image
        except ImportError as exc:
            raise ValueError(f"图片 {path.name} 超过 5MB，且未安装 Pillow") from exc
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        for scale, quality in ((1.0, 90), (1.0, 80), (0.85, 80), (0.7, 75), (0.5, 70)):
            resized = image if scale == 1.0 else image.resize((int(image.width * scale), int(image.height * scale)))
            buffer = io.BytesIO(); resized.save(buffer, "JPEG", quality=quality)
            if buffer.tell() <= IMAGE_LIMIT_BYTES: raw, mime = buffer.getvalue(), "image/jpeg"; break
        else: raise ValueError(f"图片 {path.name} 压缩后仍超过 5MB")
    b64 = base64.b64encode(raw).decode("ascii")
    return b64 if encoding == "base64" else f"data:{mime};base64,{b64}"


def _reference_media(provider: dict, item: dict) -> list[dict]:
    media = item.get("reference_media")
    if media is None: media = provider.get("reference_media")
    if media is None:
        template = provider.get("request_template"); media = template.get("reference_media") if isinstance(template, dict) else None
    media = media or []
    if not isinstance(media, list): raise ValueError("reference_media 必须是数组")
    result = []
    allowed_types = {"first_frame", "reference_image", "reference_video"}
    for index, entry in enumerate(media, 1):
        if not isinstance(entry, dict): raise ValueError(f"reference_media[{index}] 必须是对象")
        media_type = str(entry.get("type") or "").strip(); url = str(entry.get("url") or "").strip()
        if media_type not in allowed_types: raise ValueError(f"reference_media[{index}].type 不支持：{media_type}")
        if not (urlparse(url).scheme in ("http", "https") or url.startswith("data:image/")):
            raise ValueError(f"reference_media[{index}].url 必须是 HTTP(S) URL 或 data:image Base64")
        normalized = {"type": media_type, "url": url}
        if entry.get("reference_voice"): normalized["reference_voice"] = str(entry["reference_voice"]).strip()
        if entry.get("label"): normalized["label"] = str(entry["label"]).strip()
        result.append(normalized)
    if sum(1 for x in result if x["type"] == "first_frame") > 1: raise ValueError("Wan R2V 最多 1 张 first_frame")
    if len([x for x in result if x["type"] != "first_frame"]) > 5: raise ValueError("Wan R2V 最多 5 个参考图/视频")
    return result


def _reference_asset_path(asset: dict) -> Path:
    value = Path(str(asset.get("path") or "")); candidates = [value]
    if not value.is_absolute() and not str(value).startswith("output/"): candidates.insert(0, Path("output") / value)
    for candidate in candidates:
        if candidate.is_file(): return candidate
    raise FileNotFoundError(f"参考图不存在：{asset.get('path')}")


def _cog_reference_images(item: dict) -> list[str]:
    assets = item.get("reference_assets") or []
    if not assets: raise ValueError("参考生视频没有参考图")
    images = [encode_image(_reference_asset_path(asset), "data_uri") for asset in assets[:3]]
    if not images: raise ValueError("参考生视频至少需要 1 张参考图")
    return images


def _wan_r2v_prompt(item: dict) -> str:
    labels = []
    for index, entry in enumerate(item.get("reference_media") or [], 1):
        if entry.get("type") == "first_frame": continue
        if entry.get("label"): labels.append(f"图{index}是{entry['label']}")
    prompt = str(item["prompt"]).strip()
    return ("；".join(labels) + "。\n\n" + prompt) if labels else prompt


def _prepare_prompt(provider: dict, item: dict) -> None:
    from ..video_middleware import optimize_prompt_with_skill
    adapter = str(provider.get("adapter") or "generic").strip().lower(); original = str(item.get("prompt_original") or item.get("prompt") or "").strip()
    if item.get("prompt_optimized_adapter") == adapter and item.get("prompt_optimized"):
        item["prompt"] = item["prompt_optimized"]; return
    optimized = optimize_prompt_with_skill(adapter, original)
    item["prompt_original"], item["prompt_optimized"], item["prompt_optimized_adapter"], item["prompt"] = original, optimized, adapter, optimized


def build_request(provider: dict, item: dict, manifest: dict) -> dict:
    adapter = str(provider.get("adapter") or "generic").lower(); _prepare_prompt(provider, item)
    context = {"model": provider["VIDEO_API_MODEL"], "prompt": item["prompt"], "duration_seconds": item["duration_seconds"],
               "aspect_ratio": manifest.get("aspect_ratio", "16:9"), "resolution": provider.get("resolution", "480P"),
               "images": list(item.get("images") or []), "first_frame": (list(item.get("images") or [])[:1])}
    images = context["images"]
    is_wan_r2v = adapter == "aliyun_wan" and (str(provider.get("mode") or "").lower() == "r2v" or "r2v" in str(context["model"]).lower())
    if images and adapter not in ("cogvideox_zhipu",) and not is_wan_r2v:
        template = provider.get("request_template")
        if not isinstance(template, dict) or not template: raise ValueError(f"适配器 {adapter!r} 没有配置支持图片输入的 request_template")
    if adapter == "cogvideox_zhipu" and str(context["model"]).strip() != "vidu2-reference":
        duration = 5 if float(item["duration_seconds"]) <= 5 else 10; ratio = context["aspect_ratio"]
        size = provider.get("size") or ("1920x1080" if ratio == "16:9" else "1080x1920" if ratio == "9:16" else "1024x1024")
        if size not in COGVIDEOX_ALL_SIZES: raise ValueError(f"CogVideoX-3 size 不支持：{size}")
        quality = provider.get("quality", "quality"); quality = ("quality" if quality else "speed") if isinstance(quality, bool) else quality
        if quality not in ("quality", "speed"): raise ValueError("CogVideoX-3 quality 必须为 quality 或 speed")
        body = {"model": context["model"], "prompt": fit_cogvideox_prompt(context["prompt"]), "duration": duration, "size": size, "quality": quality,
                "with_audio": bool(provider.get("with_audio", True)), "fps": int(provider.get("fps", 30))}
        if images:
            if len(images) > 2: raise ValueError("CogVideoX-3 最多传 2 张图")
            body["image_url"] = images[0] if len(images) == 1 else images
            if len(images) == 2: body["quality"] = "speed"
            if not provider.get("size"): body.pop("size")
        return body
    if adapter == "cogvideox_zhipu":
        return {"model": "vidu2-reference", "prompt": fit_cogvideox_prompt(context["prompt"]), "image_url": _cog_reference_images(item),
                "duration": 4, "aspect_ratio": str(provider.get("aspect_ratio") or context["aspect_ratio"] if context["aspect_ratio"] in COGVIDEOX_REFERENCE_RATIOS else "16:9"),
                "size": str(provider.get("size") or "1280x720"), "movement_amplitude": str(provider.get("movement_amplitude") or "auto"),
                "with_audio": bool(provider.get("with_audio", True))}
    if adapter == "aliyun_wan":
        model_name = str(context["model"]).lower()
        mode = str(provider.get("mode") or "").lower() or ("r2v" if "r2v" in model_name else "")
        if "wan3.0-video" in model_name:
            media = item.get("reference_media") or []
            prompt = str(context["prompt"]).strip()
            labels = []
            for index, entry in enumerate(media, 1):
                if entry.get("type") in ("reference_image", "reference_video", "reference_audio") and entry.get("label"):
                    labels.append(f"图{index}是{entry['label']}" if entry["type"] == "reference_image" else f"素材{index}是{entry['label']}")
            if labels:
                prompt = "；".join(labels) + "。\n\n" + prompt
            ratio = context["aspect_ratio"] if context["aspect_ratio"] in ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive") else "adaptive"
            parameters = {
                "resolution": context["resolution"] if context["resolution"] in ("480P", "720P", "1080P") else "480P",
                "ratio": provider.get("ratio") or ratio,
                "duration": max(2, min(30, int(round(float(item["duration_seconds"]))))),
                "prompt_extend": bool(provider.get("prompt_extend", True)),
            }
            body = {"model": context["model"], "input": {"prompt": prompt}, "parameters": parameters}
            if media:
                body["input"]["media"] = [{"type": entry["type"], "url": entry["url"]} for entry in media]
            return body
        if mode == "r2v":
            media = _reference_media(provider, item)
            if not media: raise ValueError("Wan R2V 没有参考媒体")
            ratio = context["aspect_ratio"] if context["aspect_ratio"] in ("16:9", "9:16", "4:3", "1:1", "3:4", "adaptive") else "16:9"
            return {"model": context["model"], "input": {"prompt": _wan_r2v_prompt(item), "media": media},
                    "parameters": {"resolution": context["resolution"] if context["resolution"] in ("720P", "1080P") else "720P",
                                   "ratio": provider.get("ratio") or ratio, "duration": max(2, min(15, int(round(float(item["duration_seconds"]))))),
                                   "prompt_extend": bool(provider.get("prompt_extend", True))}}
        return {"model": context["model"], "input": {"prompt": context["prompt"]}, "parameters": {"resolution": context["resolution"],
                "ratio": provider.get("ratio") or context["aspect_ratio"], "duration": max(2, min(30, int(round(float(item["duration_seconds"]))))),
                "prompt_extend": bool(provider.get("prompt_extend", True))}}
    template = provider.get("request_template")
    if isinstance(template, dict) and template: return _render(template, context)
    body = {"model": context["model"], "prompt": context["prompt"], "duration": context["duration_seconds"], "aspect_ratio": context["aspect_ratio"]}
    if images: body["image_url"] = images[0] if len(images) == 1 else images
    return body


def request_headers(provider: dict, has_body: bool = True) -> dict:
    headers = {"Authorization": "Bearer " + provider["VIDEO_API_KEY"], "Accept": "application/json"}
    if has_body: headers["Content-Type"] = "application/json"
    if str(provider.get("adapter") or "").lower() == "aliyun_wan": headers["X-DashScope-Async"] = "enable"
    custom = provider.get("headers")
    if isinstance(custom, dict):
        for key, value in custom.items(): headers[str(key)] = str(value)
    return headers
