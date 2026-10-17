"""Dialogue TTS stage with persistent per-character voice IDs.

Preview mode never calls paid APIs. With --audio-submit, CosyVoice voices
are created only when the output registry has no voice_id for that character.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from ..config import models_file_path
from ..validator import Result
from .base import Context, read_json, write_json


def artifact(ctx: Context) -> Path:
    return ctx.out / "audio" / "audio-manifest.json"


def voice_registry_artifact(ctx: Context) -> Path:
    return ctx.out / "audio" / "character-voices.json"


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._") or "audio"


def _load_audio_config() -> list[dict]:
    path = models_file_path()
    if not path.is_file():
        raise ValueError(f"找不到模型配置文件：{path}；请先复制 models.example.json 为 models.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 {path}：{exc}") from exc
    entries = data.get("audio", [])
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path} 中必须配置非空的 audio 列表")
    result = []
    for i, item in enumerate(entries, 1):
        if not isinstance(item, dict):
            raise ValueError(f"audio[{i}] 必须是对象")
        name = str(item.get("name") or f"audio-{i}").strip()
        url = str(item.get("speech_url") or "").strip()
        model = str(item.get("model") or "").strip()
        key = str(item.get("api_key") or "").strip()
        env = str(item.get("api_key_env") or "").strip()
        if not key and env:
            key = os.environ.get(env, "").strip()
        missing = [k for k, v in (("speech_url", url), ("model", model), ("api_key 或 api_key_env", key))
                   if not v]
        if missing:
            raise ValueError(f"models.json 的 audio[{i}]（{name}）缺少：{'、'.join(missing)}")
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError(f"models.json 的 audio[{i}]（{name}）speech_url 必须是 HTTP(S) URL")
        default_provider = (
            "cosyvoice_tts" if model.startswith("cosyvoice-")
            else "qwen_audio_tts" if model.startswith("qwen-audio-")
            else "openai_speech"
        )
        provider = str(item.get("provider") or default_provider).strip().lower()
        design_url = str(item.get("voice_design_url") or "").strip()
        if provider in ("qwen_audio_tts", "cosyvoice_tts") and not design_url:
            suffix = "/api/v1/services/audio/tts/SpeechSynthesizer"
            if url.endswith(suffix):
                design_url = url[:-len(suffix)] + "/api/v1/services/audio/tts/customization"
        if provider in ("qwen_audio_tts", "cosyvoice_tts") and not design_url:
            raise ValueError(
                f"models.json 的 audio[{i}]（{name}）使用 Qwen-Audio-TTS/CosyVoice 时，"
                "speech_url 应为百炼 SpeechSynthesizer 地址，或显式配置 voice_design_url"
            )
        result.append({
            "name": name, "url": url, "design_url": design_url, "model": model, "key": key,
            "provider": provider,
            "voice": str(item.get("voice") or "alloy"),
            "voice_map": item.get("voice_map") if isinstance(item.get("voice_map"), dict) else {},
            "response_format": str(item.get("response_format") or "mp3").lower(),
            "timeout": max(10, int(item.get("timeout", 120))),
            "instructions_field": str(item.get("instructions_field") or "instructions"),
            "extra_body": item.get("extra_body") if isinstance(item.get("extra_body"), dict) else {},
            "voice_language_hints": item.get("voice_language_hints") if isinstance(item.get("voice_language_hints"), list) else ["zh"],
            "voice_preview_text": str(item.get("voice_preview_text") or "你好，我是这个故事中的角色，很高兴认识你。"),
            "sample_rate": int(item.get("sample_rate", 24000)),
            "language_hints": item.get("language_hints") if isinstance(item.get("language_hints"), list) else [],
        })
    return result


def _voice_description(character: dict) -> str:
    voice = character.get("voice") if isinstance(character.get("voice"), dict) else {}
    prompt = str(voice.get("prompt") or "").strip()
    if prompt:
        return prompt
    return "；".join(str(voice.get(k) or "").strip()
                     for k in ("timbre", "pitch", "pace", "accent", "emotion", "referenceHint")
                     if str(voice.get(k) or "").strip())


def _delivery_instruction(delivery: str) -> str:
    return str(delivery or "").strip()


def _voice_instruction(character: dict, delivery: str) -> str:
    description = _voice_description(character)
    instruction = _delivery_instruction(delivery)
    if description and instruction:
        return description + "\n本句表演要求：" + instruction
    return description or instruction


def _collect(ctx: Context, eps: Optional[list]) -> dict:
    script_path, cast_path = ctx.artifact("script"), ctx.artifact("characters")
    if not script_path.is_file():
        raise FileNotFoundError(f"找不到剧本文件：{script_path}；请先运行 script 阶段")
    if not cast_path.is_file():
        raise FileNotFoundError(f"找不到角色文件：{cast_path}；请先运行 characters 阶段")
    script, cast = read_json(script_path), read_json(cast_path)
    by_id, by_name = {}, {}
    for c in cast.get("characters", []):
        if not isinstance(c, dict):
            continue
        if c.get("id"):
            by_id[str(c["id"])] = c
        if c.get("name"):
            by_name[str(c["name"])] = c
        for alias in c.get("aliases", []):
            by_name[str(alias)] = c

    outline_path = ctx.artifact("outline")
    if outline_path.is_file():
        for entry in read_json(outline_path).get("characters", []):
            cid, name = str(entry.get("id") or ""), str(entry.get("name") or "")
            if cid and name and cid not in by_id:
                match = by_name.get(name)
                if match:
                    by_id[cid] = match

    wanted, items = set(eps or []), []
    for episode in script.get("episodes", []):
        ep = int(episode.get("ep", 0))
        if wanted and ep not in wanted:
            continue
        for scene_idx, scene in enumerate(episode.get("scenes", []), 1):
            for beat_idx, beat in enumerate(scene.get("flow", []), 1):
                if not isinstance(beat, dict) or not isinstance(beat.get("line"), str) or not beat["line"].strip():
                    continue
                speaker = str(beat.get("speaker") or "").strip()
                character = by_id.get(speaker) or by_name.get(speaker) or {}
                name = str(character.get("name") or speaker or "VO")
                character_id = str(character.get("id") or speaker or name)
                description = _voice_description(character)
                delivery = _delivery_instruction(str(beat.get("delivery") or ""))
                item_id = f"E{ep:02d}-S{scene_idx:02d}-L{beat_idx:03d}"
                items.append({
                    "id": item_id, "ep": ep, "sceneId": scene.get("sceneId"),
                    "speaker": speaker, "character_id": character_id, "character_name": name,
                    "text": beat["line"].strip(), "delivery": delivery,
                    "voice_description": description,
                    "delivery_instruction": delivery,
                    "voice_instruction": _voice_instruction(character, delivery),
                    "voice_id": None, "voice_status": "unresolved",
                    "state": "planned", "audio_path": None, "provider": None, "error": None,
                })
    if not items:
        raise ValueError("所选集没有可配音的台词；检查 script.json 或 --eps")
    return {"version": 2, "source": ctx.title, "items": items}


def _load_or_sync_registry(ctx: Context, items: list[dict], model: str) -> dict:
    path = voice_registry_artifact(ctx)
    if path.is_file():
        try:
            registry = read_json(path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"角色音色注册表不可读：{path}：{exc}") from exc
    else:
        registry = {"version": 1, "characters": {}}
    if not isinstance(registry.get("characters"), dict):
        registry["characters"] = {}
    for item in items:
        cid = str(item["character_id"])
        entry = registry["characters"].get(cid)
        if not isinstance(entry, dict):
            entry = {
                "character_id": cid, "name": item["character_name"],
                "model": model, "voice_id": None,
                "voice_description": item.get("voice_description") or "",
                "status": "needs_voice",
            }
            registry["characters"][cid] = entry
        else:
            entry["character_id"] = cid
            entry["name"] = item["character_name"]
            # Once an ID exists, keep its original design description unless the user edits the registry.
            if not entry.get("voice_id") and item.get("voice_description"):
                entry["voice_description"] = item["voice_description"]
            entry.setdefault("model", model)
            entry.setdefault("status", "ready" if entry.get("voice_id") else "needs_voice")
        voice_id = str(entry.get("voice_id") or "").strip()
        item["voice_id"] = voice_id or None
        item["voice_status"] = str(entry.get("status") or ("ready" if voice_id else "needs_voice"))
        item["voice_description"] = str(entry.get("voice_description") or item.get("voice_description") or "")
    registry["provider"] = "aliyun_bailian" if model.startswith(("qwen-audio-", "cosyvoice-")) else registry.get("provider", "manual")
    registry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_json(path, registry)
    return registry


def _request_json(url: str, key: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"API HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:800]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"API 网络错误：{exc}") from exc
    try:
        result = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("API 没有返回有效 JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("API 返回 JSON 但顶层不是对象")
    if result.get("code") and not result.get("output"):
        raise RuntimeError(f"API 错误 {result.get('code')}: {result.get('message') or result.get('error') or result}")
    return result


def _save_preview(ctx: Context, character_id: str, output: dict) -> Optional[str]:
    preview = output.get("preview_audio")
    encoded = preview.get("data") if isinstance(preview, dict) else None
    if not encoded:
        return None
    try:
        data = base64.b64decode(encoded)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("音色创建成功，但预览音频 Base64 无法解码") from exc
    # Character IDs may contain Chinese or Windows-reserved filename characters.
    # Use a deterministic ASCII-only filename to avoid invalid paths and collisions.
    digest = hashlib.sha256(str(character_id).encode("utf-8")).hexdigest()[:16]
    path = ctx.out / "audio" / "voice-previews" / f"voice_{digest}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".wav.part")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError as exc:
        # Preview audio is optional: the voice_id has already been persisted,
        # so a preview-file failure must not abort all dialogue generation.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        ctx.log(f"  警告：音色创建成功，但预览音频保存失败（{exc}）；将继续处理音频生成")
        return None
    return str(path.relative_to(ctx.out))


def _query_qwen_voice(cfg: dict, voice_id: str) -> str:
    result = _request_json(cfg["design_url"], cfg["key"], {
        "model": "voice-enrollment",
        "input": {"action": "query_voice", "voice_id": voice_id},
    }, cfg["timeout"])
    output = result.get("output") or {}
    return str(output.get("status") or "").upper()


def _wait_qwen_voice_ready(ctx: Context, registry: dict, cid: str, cfg: dict) -> None:
    entry = registry["characters"][cid]
    voice_id = str(entry.get("voice_id") or "")
    if not voice_id:
        raise RuntimeError(f"角色 {cid} 尚无 voice_id")
    for attempt in range(8):
        status = _query_qwen_voice(cfg, voice_id)
        if status == "OK":
            entry["status"] = "ready"
            entry["model"] = cfg["model"]
            registry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            write_json(voice_registry_artifact(ctx), registry)
            return
        if status == "UNDEPLOYED":
            entry["status"] = "rejected"
            write_json(voice_registry_artifact(ctx), registry)
            raise RuntimeError(f"角色 {cid} 的音色未通过审核（UNDEPLOYED），请修改描述后手动重建音色")
        entry["status"] = "pending"
        write_json(voice_registry_artifact(ctx), registry)
        if attempt < 7:
            time.sleep(2)
    raise RuntimeError(f"角色 {cid} 的音色尚未就绪；voice_id 已保存，稍后重跑会继续检查，不会自动重复创建")


def _create_qwen_voice(ctx: Context, item: dict, registry: dict, cfg: dict) -> str:
    cid = str(item["character_id"])
    entry = registry["characters"][cid]
    description = str(entry.get("voice_description") or "").strip()
    if not description:
        raise RuntimeError(
            f"角色 {cid}（{item['character_name']}）没有音色描述。请先在角色卡的 voice.prompt "
            "或 output/audio/character-voices.json 的 voice_description 中填写描述"
        )
    if len(description) > 500:
        description = description[:500]
    preview_text = cfg["voice_preview_text"].strip()
    if len(preview_text) < 15 or len(preview_text) > 200:
        raise ValueError("voice_preview_text 必须为 15–200 个字符")
    prefix = re.sub(r"[^A-Za-z0-9]", "", cid)[:10] or "character"
    hints = cfg["voice_language_hints"] or ["zh"]
    result = _request_json(cfg["design_url"], cfg["key"], {
        "model": "voice-enrollment",
        "input": {
            "action": "create_voice",
            "target_model": cfg["model"],
            "voice_prompt": description,
            "preview_text": preview_text,
            "prefix": prefix,
            "language_hints": [str(hints[0])],
        },
        "parameters": {"sample_rate": cfg["sample_rate"], "response_format": "wav"},
    }, cfg["timeout"])
    output = result.get("output") or {}
    voice_id = str(output.get("voice_id") or "").strip()
    if not voice_id:
        raise RuntimeError("音色创建接口没有返回 output.voice_id，不能继续合成")
    entry.update({
        "voice_id": voice_id, "model": cfg["model"], "status": "pending",
        "voice_description": description, "preview_text": preview_text,
        "voice_design_request_id": result.get("request_id"),
    })
    preview_path = _save_preview(ctx, cid, output)
    if preview_path:
        entry["preview_audio_path"] = preview_path
    registry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_json(voice_registry_artifact(ctx), registry)
    ctx.log(f"  角色音色已创建并保存：{item['character_name']} -> {voice_id}")
    _wait_qwen_voice_ready(ctx, registry, cid, cfg)
    return voice_id


def _ensure_qwen_voices(ctx: Context, items: list[dict], registry: dict, cfg: dict) -> None:
    for item in items:
        cid = str(item["character_id"])
        entry = registry["characters"][cid]
        voice_id = str(entry.get("voice_id") or "").strip()
        if voice_id:
            saved_model = str(entry.get("model") or "")
            if saved_model and saved_model != cfg["model"]:
                raise RuntimeError(
                    f"角色 {cid} 的 voice_id 绑定模型 {saved_model}，当前配置为 {cfg['model']}。"
                    "请恢复原模型，或手动创建新音色并修改 character-voices.json"
                )
            if str(entry.get("status") or "").lower() != "ready":
                _wait_qwen_voice_ready(ctx, registry, cid, cfg)
        else:
            voice_id = _create_qwen_voice(ctx, item, registry, cfg)
        entry["last_used_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        item["voice_id"] = voice_id
        item["voice_status"] = str(entry.get("status") or "ready")
        write_json(voice_registry_artifact(ctx), registry)


def _download_audio(url: str, target: Path, timeout: int) -> None:
    req = urllib.request.Request(url, headers={"Accept": "audio/*, application/octet-stream"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"音频下载 HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"音频下载网络错误：{exc}") from exc
    if not payload:
        raise RuntimeError("音频下载结果为空")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        tmp.write_bytes(payload)
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink()


def _synthesize_qwen(item: dict, cfg: dict, target: Path) -> None:
    voice_id = str(item.get("voice_id") or "").strip()
    if not voice_id:
        raise RuntimeError(f"角色 {item.get('character_id')} 没有 voice_id；拒绝使用随机默认音色")
    input_body = {
        "text": item["text"], "voice": voice_id, "format": cfg["response_format"],
        "sample_rate": cfg["sample_rate"],
    }
    if item.get("delivery_instruction"):
        input_body["instruction"] = item["delivery_instruction"]
    if cfg["language_hints"]:
        input_body["language_hints"] = [str(cfg["language_hints"][0])]
    input_body.update(cfg["extra_body"])
    result = _request_json(cfg["url"], cfg["key"], {"model": cfg["model"], "input": input_body}, cfg["timeout"])
    output = result.get("output") or {}
    audio = output.get("audio") or {}
    audio_url = str(audio.get("url") or "").strip()
    if audio_url:
        _download_audio(audio_url, target, cfg["timeout"])
        return
    encoded = str(audio.get("data") or "").strip()
    if encoded:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(encoded))
        return
    raise RuntimeError(
        "Qwen-Audio-TTS 返回 JSON，但没有 output.audio.url 或 output.audio.data；"
        f"request_id={result.get('request_id', 'unknown')}"
    )


def _synthesize_generic(item: dict, cfg: dict, target: Path) -> None:
    voice = (cfg["voice_map"].get(item.get("character_id"))
             or cfg["voice_map"].get(item.get("speaker"))
             or cfg["voice_map"].get(item.get("character_name"))
             or item.get("voice_id") or cfg["voice"])
    body = {
        "model": cfg["model"], "input": item["text"],
        "voice": str(voice), "response_format": cfg["response_format"],
    }
    if cfg["instructions_field"] and item.get("voice_instruction"):
        body[cfg["instructions_field"]] = item["voice_instruction"]
    body.update(cfg["extra_body"])
    req = urllib.request.Request(
        cfg["url"], data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + cfg["key"], "Content-Type": "application/json",
                 "Accept": "audio/*, application/octet-stream, application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as response:
            payload = response.read()
            content_type = response.headers.get("Content-Type", "").lower()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"TTS API HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:600]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"TTS API 网络错误：{exc}") from exc
    if not payload:
        raise RuntimeError("TTS API 返回空音频")
    if "json" in content_type or payload.lstrip().startswith((b"{", b"[")):
        raise RuntimeError("通用 TTS 适配器收到 JSON 而不是音频二进制；请使用对应供应商的适配器")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        tmp.write_bytes(payload)
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink()


def _synthesize(item: dict, cfg: dict, target: Path) -> None:
    if cfg["provider"] in ("qwen_audio_tts", "cosyvoice_tts"):
        _synthesize_qwen(item, cfg, target)
    else:
        _synthesize_generic(item, cfg, target)


def check(ctx: Context) -> Result:
    path = artifact(ctx)
    if not path.is_file():
        return Result(1, "音频任务清单尚未生成")
    try:
        items = read_json(path).get("items", [])
    except (OSError, ValueError) as exc:
        return Result(1, f"音频任务清单不可读：{exc}")
    pending = [x for x in items if x.get("state") != "succeeded" or not x.get("audio_path")]
    return Result(1, f"{len(pending)}/{len(items)} 句台词尚未生成音频") if not items or pending else Result(0, f"{len(items)} 句台词音频已生成")


def run(ctx: Context, eps: Optional[list] = None, submit: bool = False) -> Path:
    root = ctx.out / "audio"
    root.mkdir(parents=True, exist_ok=True)
    path = artifact(ctx)
    manifest = _collect(ctx, eps)
    old_items = {}
    if path.is_file():
        try:
            old = read_json(path)
            old_items = {str(x.get("id")): x for x in old.get("items", [])}
        except (OSError, ValueError):
            pass

    providers = _load_audio_config() if submit else []
    cfg = providers[0] if providers else None
    if submit and cfg is None:
        raise ValueError("正式生成需要在 models.json 配置 audio")
    model = cfg["model"] if cfg else "cosyvoice-v3.5-flash"
    registry = _load_or_sync_registry(ctx, manifest["items"], model)

    # IDs are copied into every dialogue item, so the manifest is self-contained and auditable.
    for item in manifest["items"]:
        entry = registry["characters"][str(item["character_id"])]
        item["voice_id"] = str(entry.get("voice_id") or "") or None
        item["voice_status"] = str(entry.get("status") or ("ready" if item["voice_id"] else "needs_voice"))
        old = old_items.get(item["id"])
        if (old and old.get("text") == item["text"]
                and old.get("delivery") == item["delivery"]
                and old.get("voice_id") == item["voice_id"]):
            for key in ("state", "audio_path", "provider", "error"):
                if key in old:
                    item[key] = old[key]

    manifest["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_json(path, manifest)

    prompts = root / "prompts"
    prompts.mkdir(exist_ok=True)
    for item in manifest["items"]:
        prompt_file = prompts / (_safe(item["id"]) + ".txt")
        prompt_file.write_text(
            f"Character: {item['character_name']}\nCharacter ID: {item['character_id']}\n"
            f"Voice ID: {item.get('voice_id') or '(not created yet)'}\n"
            f"Voice description: {item['voice_description']}\n"
            f"Delivery: {item['delivery_instruction']}\nDialogue: {item['text']}\n",
            encoding="utf-8")

    qc = ["# 配音人工质检清单", "", "- [ ] 同一角色跨场、跨集音色一致",
          "- [ ] 台词完整，无漏字、错字或重复", "- [ ] 情绪、语速、停顿符合剧本",
          "- [ ] 发音清晰，无爆音、杂音或截断", "", "## 台词状态", ""]
    qc += [f"- [ ] {x['id']}：{x['state']}（{x['character_name']}：{x['text']}）"
           for x in manifest["items"]]
    (root / "qc-checklist.md").write_text("\n".join(qc) + "\n", encoding="utf-8")
    ctx.log(f"音频阶段：已整理 {len(manifest['items'])} 句台词；角色音色注册表：{voice_registry_artifact(ctx)}")
    if not submit:
        ctx.log("预览模式：未调用 API。确认 models.json、音色描述和价格后，使用 --audio-submit 才会创建缺失音色并生成音频。")
        return path

    if cfg["provider"] in ("qwen_audio_tts", "cosyvoice_tts"):
        _ensure_qwen_voices(ctx, manifest["items"], registry, cfg)
        for item in manifest["items"]:
            entry = registry["characters"][str(item["character_id"])]
            item["voice_id"] = entry.get("voice_id")
            item["voice_status"] = entry.get("status", "ready")
        write_json(path, manifest)

    for item in manifest["items"]:
        extension = cfg["response_format"]
        target = root / "lines" / (_safe(item["id"]) + "." + extension)
        if item.get("state") == "succeeded" and item.get("audio_path") and (ctx.out / item["audio_path"]).is_file():
            continue
        failures = []
        for provider in providers:
            try:
                if provider["provider"] in ("qwen_audio_tts", "cosyvoice_tts") and not item.get("voice_id"):
                    raise RuntimeError(f"角色 {item['character_id']} 没有 voice_id")
                actual_target = target.with_suffix("." + provider["response_format"])
                _synthesize(item, provider, actual_target)
                item.update(
                    state="succeeded",
                    audio_path=str(actual_target.relative_to(ctx.out)),
                    provider=provider["name"], error=None,
                )
                break
            except Exception as exc:
                failures.append(f"{provider['name']}: {exc}")
        else:
            item.update(state="failed", error="；".join(failures))
        write_json(path, manifest)
        ctx.log(f"  {item['id']}：{item['state']}" + (f"；{item['error']}" if item.get("error") else ""))
    return path
