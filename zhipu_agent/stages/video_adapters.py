"""Provider-specific request construction for the video stage."""
from __future__ import annotations

import base64
import io
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _render(value: Any, context: dict) -> Any:
    if isinstance(value, dict):
        return {key: _render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, context) for item in value]
    if not isinstance(value, str):
        return value
    if value.startswith("{{") and value.endswith("}}") and value.count("{{") == 1:
        key = value[2:-2]
        if key in context:
            return context[key]
    for key, replacement in context.items():
        value = value.replace("{{" + key + "}}", str(replacement))
    return value


COGVIDEOX_PROMPT_LIMIT = 512
COGVIDEOX_SIZES = ("1280x720",)
COGVIDEOX_REFERENCE_RATIOS = ("16:9", "9:16", "1:1")
COGVIDEOX_MOVEMENTS = ("auto", "small", "medium", "large")


def fit_cogvideox_prompt(prompt: str, limit: int = COGVIDEOX_PROMPT_LIMIT) -> str:
    lines = [ln.strip() for ln in str(prompt).splitlines() if ln.strip()]
    if lines and re.match(r"^(How the reference pictures align|参考图与目标视频的对齐|目标视频在)", lines[0]):
        lines = lines[1:]
    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(mark) for mark in ("。", "！", "？", ".", "；", "，", ", "))
    return cut[:boundary + 1] if boundary >= limit // 2 else cut


IMAGE_LIMIT_BYTES = 5 * 1024 * 1024
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def encode_image(path: Path, encoding: str = "data_uri") -> str:
    path = Path(path)
    if path.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f"分镜图格式必须是 png/jpg/jpeg/webp：{path.name}")
    raw = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if len(raw) > IMAGE_LIMIT_BYTES:
        try:
            from PIL import Image
        except ImportError as exc:
            raise ValueError(f"分镜图 {path.name} 超过 5MB，且未安装 Pillow 无法自动压缩（pip install pillow）") from exc
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        for scale, quality in ((1.0, 90), (1.0, 80), (0.85, 80), (0.7, 75), (0.5, 70)):
            resized = image if scale == 1.0 else image.resize((int(image.width * scale), int(image.height * scale)))
            buffer = io.BytesIO()
            resized.save(buffer, "JPEG", quality=quality)
            if buffer.tell() <= IMAGE_LIMIT_BYTES:
                raw, mime = buffer.getvalue(), "image/jpeg"
                break
        else:
            raise ValueError(f"分镜图 {path.name} 压缩后仍超过 5MB")
    b64 = base64.b64encode(raw).decode("ascii")
    return b64 if encoding == "base64" else f"data:{mime};base64,{b64}"


def _reference_media(provider: dict, item: dict) -> list[dict]:
    media = item.get("reference_media")
    if media is None:
        media = provider.get("reference_media")
    if media is None:
        template = provider.get("request_template")
        media = template.get("reference_media") if isinstance(template, dict) else None
    media = media or []
    if not isinstance(media, list):
        raise ValueError("reference_media 必须是数组")
    result = []
    allowed_types = {"reference_image", "reference_video"}
    for index, entry in enumerate(media, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"reference_media[{index}] 必须是对象")
        media_type = str(entry.get("type") or "").strip()
        url = str(entry.get("url") or "").strip()
        if media_type not in allowed_types:
            raise ValueError(f"reference_media[{index}].type 必须是 reference_image 或 reference_video")
        if not (urlparse(url).scheme in ("http", "https") or url.startswith("data:image/")):
            raise ValueError(f"reference_media[{index}].url 必须是 HTTP(S) URL 或 data:image Base64")
        normalized = {"type": media_type, "url": url}
        if entry.get("reference_voice"):
            voice = str(entry["reference_voice"]).strip()
            if urlparse(voice).scheme not in ("http", "https") and not voice.startswith("data:audio/"):
                raise ValueError(f"reference_media[{index}].reference_voice 必须是 HTTP(S) URL")
            normalized["reference_voice"] = voice
        if entry.get("label"):
            normalized["label"] = str(entry["label"]).strip()
        result.append(normalized)
    if len(result) > 5:
        raise ValueError("Wan R2V 参考图/视频最多 5 个；当前 character/art 引用超过 5 个")
    return result


def _reference_asset_path(asset: dict) -> Path:
    value = Path(str(asset.get("path") or ""))
    candidates = [value]
    if not value.is_absolute() and not str(value).startswith("output/"):
        candidates.insert(0, Path("output") / value)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"CogVideoX-3 参考图不存在：{asset.get('path')}")


def _cog_reference_images(item: dict) -> list[str]:
    assets = item.get("reference_assets") or []
    if not assets:
        raise ValueError("CogVideoX-3 参考生视频没有从 character/art 找到参考图")
    if len(assets) > 3:
        assets = assets[:3]
    images = [encode_image(_reference_asset_path(asset), "data_uri") for asset in assets]
    if not 1 <= len(images) <= 3:
        raise ValueError("CogVideoX-3 image_url 必须包含 1-3 张参考图")
    return images


def _wan_r2v_prompt(item: dict) -> str:
    labels = []
    for index, entry in enumerate(item.get("reference_media") or [], 1):
        label = entry.get("label")
        if label:
            labels.append(f"图{index}是{label}")
    prompt = str(item["prompt"]).strip()
    return ("；".join(labels) + "。\n\n" + prompt) if labels else prompt


def _prepare_prompt(provider: dict, item: dict) -> None:
    """在真正提交当前镜头前，只优化这一镜头一次；保留原始/优化结果供调试。"""
    from ..video_middleware import optimize_prompt_with_skill

    adapter = str(provider.get("adapter") or "generic").strip().lower()
    original = str(item.get("prompt_original") or item.get("prompt") or "").strip()
    if item.get("prompt_optimized_adapter") == adapter and item.get("prompt_optimized"):
        item["prompt"] = item["prompt_optimized"]
        return
    optimized = optimize_prompt_with_skill(adapter, original)
    item["prompt_original"] = original
    item["prompt_optimized"] = optimized
    item["prompt_optimized_adapter"] = adapter
    item["prompt"] = optimized


def build_request(provider: dict, item: dict, manifest: dict) -> dict:
    adapter = str(provider.get("adapter") or "generic").lower()
    _prepare_prompt(provider, item)
    context = {
        "model": provider["VIDEO_API_MODEL"],
        "prompt": item["prompt"],
        "duration_seconds": item["duration_seconds"],
        "aspect_ratio": manifest.get("aspect_ratio", "16:9"),
        "resolution": provider.get("resolution", "480P"),
    }

    images = list(item.get("images") or [])
    is_wan_r2v = adapter == "aliyun_wan" and "r2v" in str(context["model"]).lower()
    if images and adapter != "cogvideox_zhipu" and not is_wan_r2v:
        raise ValueError(f"适配器 {adapter!r} 暂未支持图生视频；目前只有 cogvideox_zhipu 支持")

    if adapter == "cogvideox_zhipu":
        if str(context["model"]).strip() != "vidu2-reference":
            raise ValueError("CogVideoX-3 参考生视频必须使用 model=vidu2-reference")
        prompt = fit_cogvideox_prompt(context["prompt"])
        if not prompt and not item.get("reference_assets"):
            raise ValueError("CogVideoX-3 参考生视频必须提供 prompt 或 image_url")
        ratio = str(provider.get("aspect_ratio") or context["aspect_ratio"] or "16:9")
        if ratio not in COGVIDEOX_REFERENCE_RATIOS:
            ratio = "16:9"
        size = str(provider.get("size") or "1280x720")
        if size not in COGVIDEOX_SIZES:
            raise ValueError(f"CogVideoX-3 size 必须是 {', '.join(COGVIDEOX_SIZES)} 之一，当前为 {size!r}")
        movement = str(provider.get("movement_amplitude") or "auto")
        if movement not in COGVIDEOX_MOVEMENTS:
            raise ValueError(f"CogVideoX-3 movement_amplitude 必须是 {', '.join(COGVIDEOX_MOVEMENTS)} 之一")
        return {
            "model": "vidu2-reference",
            "prompt": prompt,
            "image_url": _cog_reference_images(item),
            "duration": 4,
            "aspect_ratio": ratio,
            "size": size,
            "movement_amplitude": movement,
            "with_audio": bool(provider.get("with_audio", True)),
        }

    if adapter == "aliyun_wan":
        model = context["model"].lower()
        mode = str(provider.get("mode") or "").lower()
        if not mode and "r2v" in model:
            mode = "r2v"
        if mode == "r2v":
            media = _reference_media(provider, item)
            if not media:
                raise ValueError("Wan R2V 已启用，但没有从 character/art 引用中找到参考图")
            duration = max(2, min(15, int(round(float(item["duration_seconds"])))) )
            ratio = context["aspect_ratio"]
            if ratio not in ("16:9", "9:16", "4:3", "1:1", "3:4", "adaptive"):
                ratio = "16:9"
            return {
                "model": context["model"],
                "input": {"prompt": _wan_r2v_prompt(item), "media": media},
                "parameters": {
                    "resolution": context["resolution"] if context["resolution"] in ("720P", "1080P") else "720P",
                    "ratio": provider.get("ratio") or ratio,
                    "duration": duration,
                    "prompt_extend": bool(provider.get("prompt_extend", True)),
                },
            }

        duration = max(2, min(30, int(round(float(item["duration_seconds"])))) )
        ratio = context["aspect_ratio"]
        if ratio not in ("16:9", "9:16", "4:3", "1:1", "3:4", "adaptive"):
            ratio = "adaptive"
        return {
            "model": context["model"], "input": {"prompt": context["prompt"]},
            "parameters": {
                "resolution": context["resolution"], "ratio": provider.get("ratio") or ratio,
                "duration": duration, "prompt_extend": bool(provider.get("prompt_extend", True)),
            },
        }

    template = provider.get("request_template")
    if isinstance(template, dict) and template:
        return _render(template, context)
    return {
        "model": context["model"], "prompt": context["prompt"],
        "duration": context["duration_seconds"], "aspect_ratio": context["aspect_ratio"],
    }


def request_headers(provider: dict, has_body: bool = True) -> dict:
    headers = {"Authorization": "Bearer " + provider["VIDEO_API_KEY"], "Accept": "application/json"}
    if has_body:
        headers["Content-Type"] = "application/json"
    if str(provider.get("adapter") or "").lower() == "aliyun_wan":
        headers["X-DashScope-Async"] = "enable"
    custom = provider.get("headers")
    if isinstance(custom, dict):
        for key, value in custom.items():
            headers[str(key)] = str(value)
    return headers
