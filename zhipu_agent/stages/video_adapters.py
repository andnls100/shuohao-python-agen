"""Provider-specific request construction for the video stage.

The stage owns manifests, retries, polling, and downloads. This module translates
the normalized shot fields into provider-specific request bodies and headers.
"""
from __future__ import annotations

import base64
import io
import mimetypes
import re
from pathlib import Path
from typing import Any


def _render(value: Any, context: dict) -> Any:
    """Recursively interpolate {{field}} placeholders; exact placeholders preserve type."""
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
COGVIDEOX_SIZES = ("1280x720", "720x1280", "1024x1024", "1920x1080", "1080x1920", "2048x1080", "3840x2160")


def fit_cogvideox_prompt(prompt: str, limit: int = COGVIDEOX_PROMPT_LIMIT) -> str:
    """CogVideoX-3 的 prompt 最多 512 字符：去掉图片对齐指令行，压缩空白，再按句子边界截断。"""
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
IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def encode_image(path: Path, encoding: str = "data_uri") -> str:
    """把本地分镜图编码成 CogVideoX-3 能接收的 Base64（png/jpeg/jpg，单张 ≤5MB；超限时转 JPEG 压缩）。"""
    path = Path(path)
    if path.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f"分镜图格式必须是 png/jpg/jpeg：{path.name}")
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


def build_request(provider: dict, item: dict, manifest: dict) -> dict:
    """Build the request body for the selected provider adapter."""
    adapter = str(provider.get("adapter") or "generic").lower()
    context = {
        "model": provider["VIDEO_API_MODEL"],
        "prompt": item["prompt"],
        "duration_seconds": item["duration_seconds"],
        "aspect_ratio": manifest.get("aspect_ratio", "16:9"),
        "resolution": provider.get("resolution", "480P"),
    }

    images = list(item.get("images") or [])
    if images and adapter != "cogvideox_zhipu":
        raise ValueError(f"适配器 {adapter!r} 暂未支持图生视频；目前只有 cogvideox_zhipu 支持")

    if adapter == "cogvideox_zhipu":
        # Zhipu videos API accepts 5 or 10 seconds for CogVideoX-3.
        duration = 5 if float(item["duration_seconds"]) <= 5 else 10
        ratio = context["aspect_ratio"]
        size = provider.get("size") or ("1920x1080" if ratio == "16:9" else "1080x1920" if ratio == "9:16" else "1024x1024")
        if size not in COGVIDEOX_SIZES:
            raise ValueError(f"CogVideoX-3 size 必须是 {', '.join(COGVIDEOX_SIZES)} 之一，当前为 {size!r}")
        quality = provider.get("quality", "quality")
        if isinstance(quality, bool):
            quality = "quality" if quality else "speed"
        if quality not in ("quality", "speed"):
            raise ValueError("CogVideoX-3 quality 必须为 'quality' 或 'speed'")

        body = {
            "model": context["model"],
            "prompt": fit_cogvideox_prompt(context["prompt"]),
            "duration": duration,
            "size": size,
            "quality": quality,
            "with_audio": bool(provider.get("with_audio", True)),
            "fps": int(provider.get("fps", 30)),
        }
        if images:
            if len(images) > 2:
                raise ValueError("CogVideoX-3 最多传 2 张图（首帧、尾帧）")
            # 单张是首帧；两张时第一张首帧、第二张尾帧（官方）。首尾帧模式只支持 speed。
            body["image_url"] = images[0] if len(images) == 1 else images
            if len(images) == 2:
                body["quality"] = "speed"
            if not provider.get("size"):
                body.pop("size")  # 不传 size：按图片比例决定画幅，和分镜图一致
        return body

    if adapter == "aliyun_wan":
        # Wan 3.0 official API: input.prompt + parameters.duration/resolution/ratio.
        # Wan takes an integer duration; short storyboard shots are rounded to seconds.
        duration = max(2, min(30, int(round(float(item["duration_seconds"])))))
        ratio = context["aspect_ratio"]
        if ratio not in ("16:9", "9:16", "4:3", "1:1", "3:4", "adaptive"):
            ratio = "adaptive"
        return {
            "model": context["model"],
            "input": {"prompt": context["prompt"]},
            "parameters": {
                "resolution": context["resolution"],
                "ratio": provider.get("ratio") or ratio,
                "duration": duration,
                "prompt_extend": bool(provider.get("prompt_extend", True)),
            },
        }

    template = provider.get("request_template")
    if isinstance(template, dict) and template:
        return _render(template, context)

    # Backwards-compatible generic shape for existing providers. New providers
    # with a different API contract should supply request_template in models.json.
    return {
        "model": context["model"],
        "prompt": context["prompt"],
        "duration": context["duration_seconds"],
        "aspect_ratio": context["aspect_ratio"],
    }


def request_headers(provider: dict, has_body: bool = True) -> dict:
    headers = {
        "Authorization": "Bearer " + provider["VIDEO_API_KEY"],
        "Accept": "application/json",
    }
    if has_body:
        headers["Content-Type"] = "application/json"
    if str(provider.get("adapter") or "").lower() == "aliyun_wan":
        headers["X-DashScope-Async"] = "enable"
    custom = provider.get("headers")
    if isinstance(custom, dict):
        for key, value in custom.items():
            headers[str(key)] = str(value)
    return headers
