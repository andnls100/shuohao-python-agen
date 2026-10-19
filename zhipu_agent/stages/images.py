"""Optional batch image-asset stage.

Consumes the prompts already stored in cast.json and art.json. It never reads the
novel or calls a text model. Image API calls only happen with --images-gen.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import models_file_path
from ..validator import Result
from .base import Context, read_json, safe_name, write_json


# 只控制“图片请求参数”和“生图提示词”的调试日志，不影响其他 ctx.log / print。
# True：打印；False：不打印。
PRINT_IMAGE_PARAMS_AND_PROMPT = True


def artifact(ctx: Context) -> Path:
    return ctx.out / "images" / "image-manifest.json"


def _image_debug_log(message: str) -> None:
    if PRINT_IMAGE_PARAMS_AND_PROMPT:
        print(message, flush=True)


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
            "raw": entry,  # 分镜图阶段读取其中可选的参考图字段
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
    # 只受 PRINT_IMAGE_PARAMS_AND_PROMPT 控制，不影响其他日志。
    _image_debug_log("[image-api] ===== HTTP 请求参数开始 =====")
    _image_debug_log("[image-api] method=POST")
    _image_debug_log(f"[image-api] url={provider['url']}")
    _image_debug_log("[image-api] headers=" + json.dumps({
        "Authorization": "Bearer <REDACTED>",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }, ensure_ascii=False))
    _image_debug_log("[image-api] timeout_seconds=" + str(provider["timeout"]))
    _image_debug_log("[image-api] json_body=" + json.dumps(body, ensure_ascii=False, indent=2))
    _image_debug_log("[image-api] ===== HTTP 请求参数结束 =====")

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


# ---------------------------------------------------------------- 画风层
# 设定集里的 image.prompt / image.sheet 刻意不写画风（见 novel-characters 的 profile-pass.md）：
# 画风是出图那一刻才定的。原来这里直接把它们发给图片接口，等于没有画风，模型按自己的默认口味出，
# 常常是插画 / CG 感。这里把 character-refs 技能的画风预设（skills/character-refs/scripts/looks.mjs，
# 默认写实照片）读进来拼到角色提示词后面，和 character-refs 用的是同一份，不另存一套。

_IMAGE_STYLE_DEFAULTS = {"character_mode": "sheet", "negative": "omit", "look": ""}
_DESIGN_SHEET_LEAD = re.compile(r"^\s*(Single )?(Character|Model)( design| model)? sheet( of)?\s*[:,\-]?\s*", re.I)

# 设定集提示词里夹着两个渲染引擎术语。写实照片画风下，它们会把图往 CG 渲染带；换成摄影里的说法，意思不变。
_CG_TERMS = ((re.compile(r"ambient occlusion", re.I), "soft contact shadow"),
             (re.compile(r"subsurface scattering", re.I), "translucency"))

_LOOKS_JS = (
    "const m = await import(process.argv[1]);"
    "console.log(JSON.stringify({looks: m.LOOKS, default: m.DEFAULT_LOOK_ID}));"
)


def _load_looks(skills_dir: Path, node_bin: str = "node") -> dict:
    """读 character-refs 的画风预设表，返回 {"looks": [...], "default": id}。"""
    script = Path(skills_dir) / "character-refs" / "scripts" / "looks.mjs"
    if not script.is_file():
        raise FileNotFoundError(f"找不到画风预设：{script}")
    done = subprocess.run(
        [node_bin, "--input-type=module", "-e", _LOOKS_JS, script.resolve().as_uri()],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    if done.returncode != 0:
        raise RuntimeError(f"读取画风预设失败：{done.stderr.strip()[:300]}")
    return json.loads(done.stdout)


def _pick_look(table: dict, name: str = "") -> dict:
    wanted = (name or table["default"]).strip().lower()
    for look in table["looks"]:
        names = [look["id"], *look.get("names", [])]
        if wanted in (str(n).strip().lower() for n in names):
            return look
    known = "、".join(look["id"] for look in table["looks"])
    raise ValueError(f"models.json 的 image_style.look 写的是 {name!r}，没有这个画风。可用：{known}")


def _image_style_config() -> dict:
    """models.json 里可选的 image_style：{"look": 画风名, "character_mode": portrait|sheet, "negative": omit|append}。"""
    config = dict(_IMAGE_STYLE_DEFAULTS)
    path = models_file_path()
    if path.is_file():
        try:
            extra = json.loads(path.read_text(encoding="utf-8-sig")).get("image_style") or {}
        except (OSError, ValueError):
            extra = {}
        if not isinstance(extra, dict):
            raise ValueError("models.json 的 image_style 必须是对象")
        config.update({k: str(v) for k, v in extra.items() if k in config})
    if config["character_mode"] not in ("portrait", "sheet"):
        raise ValueError("image_style.character_mode 只能是 sheet（多视图设定图，默认）或 portrait（单人半身像）")
    if config["negative"] not in ("omit", "append"):
        raise ValueError("image_style.negative 只能是 omit（不发负面词，默认）或 append（拼成 Avoid 句子）")
    return config


def _character_prompt(record: dict, style: dict, look: dict) -> tuple[str, str]:
    """角色出图提示词 = 人物描述 + 画风层。

    portrait：用 image.prompt（单人四分之三半身像），去掉 "character sheet" 这类设定图措辞，
      再接画风的相机 / 皮肤质感段和固定的单人中性背景。没有拼画风自带的 clean 段：它写着
      "empty hands, no props"，会和设定里"手里拿着箱子 / 佩剑"打架。
    sheet（默认）：沿用 image.sheet（16:9 三区多视图设定图），只在后面补画风段。脸在图里很小，写实质感会差，
      设定图要的是版面，不是照片感。
    negative：omit（默认）不发负面词。这类接口大多只有正向提示词，把"塑料皮肤、娃娃眼"写进去，
      模型反而可能画出来；想要的质感已经用正向写法放在画风段里。append 才拼成 Avoid 句子。
    """
    image = record.get("image") or {}
    sheet = str(image.get("sheet") or "").strip()
    single = str(image.get("prompt") or "").strip()
    base = (sheet or single) if style["character_mode"] == "sheet" else (single or sheet)
    if not base:
        raise ValueError(f"角色「{record.get('name', record.get('id', '?'))}」没有 image.sheet 或 image.prompt")
    negative = str(image.get("negativePrompt") or "").strip()
    if look.get("medium") == "photo":
        for pattern, plain in _CG_TERMS:
            base = pattern.sub(plain, base)
    if style["character_mode"] == "portrait":
        stripped = _DESIGN_SHEET_LEAD.sub("", base, count=1).strip()
        if stripped != base and stripped:
            base = "Portrait of " + stripped if not stripped[:2].lower() in ("a ", "an") else stripped[0].upper() + stripped[1:]
        prompt = f"{base}\n\n{look['style']} Exactly one person, plain neutral studio backdrop, no text, no watermark."
    else:
        prompt = f"{base}\n\n{look['style']}"
    if style["negative"] == "append":
        merged = ", ".join(x for x in (negative, str(look.get("neg") or "").strip()) if x)
        if merged:
            prompt += "\n\nAvoid the following visual elements: " + merged
            negative = merged
    return prompt, negative if style["negative"] == "append" else ""


def _prompt_for(record: dict, kind: str) -> tuple[str, str]:
    image = record.get("image") or {}
    prompt = str(image.get("sheet") or image.get("prompt") or "").strip()
    negative = str(image.get("negativePrompt") or "").strip()
    if not prompt:
        raise ValueError(f"{kind}「{record.get('name', record.get('id', '?'))}」没有 image.sheet 或 image.prompt")
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

    style = _image_style_config()
    look = _pick_look(_load_looks(ctx.settings.skills_dir, ctx.settings.node_bin), style["look"])
    ctx.log(f"角色出图：画风「{look['id']}」，{'单人半身像' if style['character_mode'] == 'portrait' else '多视图设定图'}，"
            f"负面词{'拼进提示词' if style['negative'] == 'append' else '不发送'}")

    for index, character in enumerate(cast.get("characters", []), 1):
        prompt, negative = _character_prompt(character, style, look)
        ident = str(character.get("id") or f"C{index:03d}")
        suffix = "portrait" if style["character_mode"] == "portrait" else "sheet"
        filename = f"{safe_name(character['name'])}-{safe_name(ident)}-{suffix}"
        tasks.append({
            "id": f"character:{ident}", "kind": "character", "asset_id": ident,
            "name": character.get("name", ident), "prompt": prompt,
            "negative_prompt": negative, "output_base": Path("images") / "characters" / filename,
            "look": look["id"], "character_mode": style["character_mode"],
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


def _matches(item: dict, wanted: List[str]) -> bool:
    """--images-only 的名字能对上这张图：任务名（徐今朝）、编号（C03）或完整 id（character:C03）。"""
    keys = {str(item.get("name", "")), str(item.get("asset_id", "")), str(item.get("id", ""))}
    return any(w in keys for w in wanted)


def run(ctx: Context, generate: bool = False, only: Optional[List[str]] = None) -> Path:
    path = artifact(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    tasks = _collect_tasks(ctx)
    previous = {}
    if path.is_file():
        try:
            previous = {item["id"]: item for item in read_json(path).get("items", [])}
        except (OSError, ValueError, KeyError):
            previous = {}

    if only:
        unknown = [w for w in only if not any(_matches(t, [w]) for t in tasks)]
        if unknown:
            known = "、".join(f"{t['name']}（{t['asset_id']}）" for t in tasks)
            raise ValueError(f"--images-only 里的 {'、'.join(unknown)} 找不到对应的图。可选：{known}")

    items = []
    for task in tasks:
        picked = not only or _matches(task, only)
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
        # --images-only 时，--force 只重置被选中的图，其余已完成的不动
        if old_ok and not (ctx.force and generate and picked):
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
        if only and not _matches(item, only):
            continue
        target = ctx.out / str(item.get("output_path", ""))
        if (item.get("state") == "succeeded" and target.is_file() and not ctx.force):
            ctx.log(f"跳过已完成：{item['name']} → {item['output_path']}")
            continue

        failures = []
        item["state"], item["error"] = "generating", None
        write_json(path, manifest)
        for provider in providers:
            try:
                if PRINT_IMAGE_PARAMS_AND_PROMPT:
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
    _refresh_reports(ctx)
    return path


def _refresh_reports(ctx: Context) -> None:
    """把最新 manifest 关联到角色/美术报告，并在生图后自动重渲染。"""
    cast_path = ctx.artifact("characters")
    art_path = ctx.artifact("art")
    if cast_path.is_file():
        ctx.render_to(
            ctx.skill("novel-characters"),
            ["render", str(cast_path), "--html"],
            ctx.out / "characters" / "report.html",
        )
    if art_path.is_file():
        ctx.render_to(
            ctx.skill("novel-art"),
            ["render", str(art_path), "--html"],
            ctx.out / "art" / "art-report.html",
        )
