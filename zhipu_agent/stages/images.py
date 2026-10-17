"""Optional batch image-asset stage.

Consumes the prompts already stored in cast.json and art.json. It never reads the
novel or calls a text model. Image API calls only happen with --images-gen.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import models_file_path
from ..validator import Result
from .base import Context, read_json, safe_name, write_json


def artifact(ctx: Context) -> Path:
    return ctx.out / "images" / "image-manifest.json"


def _image_providers() -> List[dict]:
    path = models_file_path()
    if not path.is_file():
        raise ValueError(f"找不到模型配置文件：{path}；请复制 models.example.json 为 models.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 {path}：{exc}") from exc

    entries = data.get("image", [])
    if not isinstance(entries, list) or not entries:
        raise ValueError(
            f"{path} 中没有配置 image 号池。请添加非空的 image 列表，格式见 models.example.json。"
        )

    providers = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"models.json 的 image[{index}] 必须是对象")
        name = str(entry.get("name") or f"image-{index}").strip()
        base_url = str(entry.get("base_url") or "").strip().rstrip("/")
        endpoint = str(entry.get("endpoint") or "/images/generations").strip()
        model = str(entry.get("model") or "").strip()
        api_key = str(entry.get("api_key") or "").strip()
        key_env = str(entry.get("api_key_env") or "").strip()
        if not api_key and key_env:
            api_key = os.environ.get(key_env, "").strip()
        missing = [field for field, value in (
            ("base_url", base_url), ("model", model), ("api_key 或 api_key_env", api_key)
        ) if not value]
        if missing:
            raise ValueError(f"models.json 的 image[{index}]（{name}）缺少：{'、'.join(missing)}")
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            url = endpoint
        else:
            url = base_url + "/" + endpoint.lstrip("/")
        if urllib.parse.urlparse(url).scheme not in ("http", "https"):
            raise ValueError(f"models.json 的 image[{index}]（{name}）endpoint 必须是 HTTP(S) URL")
        providers.append({
            "name": name,
            "url": url,
            "api_key": api_key,
            "model": model,
            "size": str(entry.get("size") or "1536x1024"),
            "quality": str(entry.get("quality") or "high"),
            "timeout": max(10, int(entry.get("timeout", 180))),
        })
    return providers


def _request_json(provider: dict, prompt: str) -> dict:
    body = {
        "model": provider["model"],
        "prompt": prompt,
        "size": provider["size"],
        "n": 1,
    }
    if provider.get("quality"):
        body["quality"] = provider["quality"]
    req = urllib.request.Request(
        provider["url"],
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + provider["api_key"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=provider["timeout"]) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:700]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"网络请求失败：{exc}") from exc
    try:
        result = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise RuntimeError(f"图片 API 返回的不是 JSON：{raw[:200]!r}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("图片 API 返回的 JSON 顶层必须是对象")
    return result


def _find_image_payload(result: dict) -> tuple[Optional[str], Optional[str]]:
    rows = result.get("data")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        row = rows[0]
        b64 = row.get("b64_json") or row.get("image_base64") or row.get("base64")
        url = row.get("url") or row.get("image_url")
        if isinstance(b64, str) and b64:
            return b64, None
        if isinstance(url, str) and url:
            return None, url
    # Some compatible providers use a top-level image_url or output_url.
    for key in ("image_url", "output_url", "url"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return None, value
    return None, None


def _download_image(url: str, timeout: int) -> bytes:
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise RuntimeError("图片 API 返回的图片 URL 必须是 HTTP(S)")
    req = urllib.request.Request(url, headers={"Accept": "image/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"下载图片失败：{exc}") from exc
    if not data:
        raise RuntimeError("图片 API 返回了空图片")
    return data


def _extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"RIFF",)) and data[8:12] == b"WEBP":
        return ".webp"
    raise RuntimeError("API 返回的数据不是可识别的 PNG/JPEG/WebP 图片")


def _prompt_for(record: dict, kind: str) -> tuple[str, str]:
    image = record.get("image") or {}
    prompt = str(image.get("sheet") or image.get("prompt") or "").strip()
    negative = str(image.get("negativePrompt") or "").strip()
    if not prompt:
        raise ValueError(f"{kind}「{record.get('name', record.get('id', '?'))}」没有 image.sheet 或 image.prompt")
    # The OpenAI Images API has no portable negative_prompt field; append it to the prompt.
    if negative:
        prompt += "\n\nAvoid the following visual elements: " + negative
    return prompt, negative


def _slug(ctx: Context, name: str) -> str:
    skill = ctx.skill("novel-staging")
    result = ctx.run(skill, ["slug", name])
    return result.stdout.strip() if result.ok and result.stdout.strip() else safe_name(name)


def _collect_tasks(ctx: Context) -> List[dict]:
    cast_path = ctx.artifact("characters")
    art_path = ctx.artifact("art")
    if not cast_path.is_file():
        raise FileNotFoundError(f"需要先有角色设定：{cast_path}（先运行 characters 阶段）")
    if not art_path.is_file():
        raise FileNotFoundError(f"需要先有美术设定：{art_path}（先运行 art 阶段）")

    cast = read_json(cast_path)
    art = read_json(art_path)
    tasks: List[dict] = []

    for index, character in enumerate(cast.get("characters", []), 1):
        prompt, negative = _prompt_for(character, "角色")
        ident = str(character.get("id") or f"C{index:03d}")
        filename = f"{safe_name(character['name'])}-{safe_name(ident)}-sheet"
        tasks.append({
            "id": f"character:{ident}", "kind": "character", "asset_id": ident,
            "name": character.get("name", ident), "prompt": prompt,
            "negative_prompt": negative, "output_base": Path("images") / "characters" / filename,
        })

    scene_dir = Path("art") / "images"
    for index, scene in enumerate(art.get("scenes", []), 1):
        prompt, negative = _prompt_for(scene, "场景")
        ident = str(scene.get("id") or f"S{index:03d}")
        filename = _slug(ctx, str(scene.get("name") or ident)) + "-sheet"
        tasks.append({
            "id": f"scene:{ident}", "kind": "scene", "asset_id": ident,
            "name": scene.get("name", ident), "prompt": prompt,
            "negative_prompt": negative, "output_base": scene_dir / filename,
        })

    for index, prop in enumerate(art.get("props", []), 1):
        prompt, negative = _prompt_for(prop, "道具")
        ident = str(prop.get("id") or f"P{index:03d}")
        filename = f"{safe_name(prop['name'])}-{safe_name(ident)}-sheet"
        tasks.append({
            "id": f"prop:{ident}", "kind": "prop", "asset_id": ident,
            "name": prop.get("name", ident), "prompt": prompt,
            "negative_prompt": negative, "output_base": Path("images") / "props" / filename,
        })

    if not tasks:
        raise ValueError("cast.json 和 art.json 中没有可生图的角色、场景或道具")
    return tasks


def check(ctx: Context) -> Result:
    path = artifact(ctx)
    if not path.is_file():
        return Result(1, "批量图片清单尚未生成")
    try:
        manifest = read_json(path)
    except (OSError, ValueError) as exc:
        return Result(1, f"批量图片清单不可读：{exc}")
    items = manifest.get("items", [])
    if not items:
        return Result(1, "批量图片清单为空")
    pending = [item for item in items if item.get("state") != "succeeded"
               or not (ctx.out / str(item.get("output_path", ""))).is_file()]
    if pending:
        return Result(1, f"{len(pending)}/{len(items)} 张图片尚未成功生成")
    return Result(0, f"{len(items)} 张图片已生成")


def run(ctx: Context, generate: bool = False) -> Path:
    path = artifact(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    tasks = _collect_tasks(ctx)
    previous = {}
    if path.is_file():
        try:
            previous = {item["id"]: item for item in read_json(path).get("items", [])}
        except (OSError, ValueError, KeyError):
            previous = {}

    items = []
    for task in tasks:
        digest = hashlib.sha256(task["prompt"].encode("utf-8")).hexdigest()
        old = previous.get(task["id"], {})
        output_path = old.get("output_path")
        old_ok = (
            old.get("prompt_sha256") == digest
            and old.get("state") == "succeeded"
            and output_path
            and (ctx.out / output_path).is_file()
        )
        output_base = task.pop("output_base")
        if old_ok and not (ctx.force and generate):
            task.update(old)
        else:
            task.update({
                "prompt_sha256": digest,
                "output_path": str(output_base.with_suffix(".png")).replace("\\", "/"),
                "state": "pending", "provider": None, "error": None,
                "attempts": 0,
            })
        task["output_base"] = str(output_base).replace("\\", "/")
        items.append(task)

    manifest = {
        "version": 1,
        "source": ctx.title,
        "generated_from": ["cast.json", "art.json"],
        "note": "Prompts are reused from saved JSON; this stage does not read the novel or call a text model.",
        "items": items,
    }
    write_json(path, manifest)

    if not generate:
        ctx.log(f"已从 cast.json / art.json 整理 {len(items)} 个生图任务；没有调用图片 API。")
        ctx.log(f"确认 models.json 的 image 号池后，使用 --stages images --images-gen 开始批量出图：{path}")
        return path

    providers = _image_providers()
    for item in items:
        target = ctx.out / str(item.get("output_path", ""))
        if (item.get("state") == "succeeded" and target.is_file() and not ctx.force):
            ctx.log(f"跳过已完成：{item['name']} → {item['output_path']}")
            continue

        failures = []
        item["state"], item["error"] = "generating", None
        write_json(path, manifest)
        for provider in providers:
            try:
                # Log the exact prompt passed to the image API. Do not log credentials.
                ctx.log(
                    f"生图提示词 | 任务={item['id']} | 名称={item['name']} | "
                    f"平台={provider['name']} | 模型={provider['model']} | 尺寸={provider['size']}"
                )
                ctx.log("----- 生图提示词开始 -----")
                ctx.log(item["prompt"])
                ctx.log("----- 生图提示词结束 -----")
                result = _request_json(provider, item["prompt"])
                b64, url = _find_image_payload(result)
                if b64:
                    data = base64.b64decode(b64, validate=True)
                elif url:
                    data = _download_image(url, provider["timeout"])
                else:
                    raise RuntimeError("响应中找不到 data[0].b64_json 或图片 URL")
                ext = _extension(data)
                base = ctx.out / item["output_base"]
                target = base.with_suffix(ext)
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix(target.suffix + ".part")
                temp.write_bytes(data)
                temp.replace(target)
                item.update(
                    state="succeeded",
                    provider=provider["name"],
                    model=provider["model"],
                    output_path=str(target.relative_to(ctx.out)).replace("\\", "/"),
                    error=None,
                    attempts=int(item.get("attempts", 0)) + 1,
                )
                break
            except Exception as exc:
                failures.append(f"{provider['name']}: {exc}")
                item["attempts"] = int(item.get("attempts", 0)) + 1
        else:
            item.update(state="failed", error="；".join(failures))
        write_json(path, manifest)
        ctx.log(
            f"{'完成' if item['state'] == 'succeeded' else '失败'}：{item['name']}"
            + (f" → {item['output_path']}" if item["state"] == "succeeded" else f" · {item['error'][:250]}")
        )

    result = check(ctx)
    ctx.log(f"批量生图结果：{result.stdout}")
    return path
