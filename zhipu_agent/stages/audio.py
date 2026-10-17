"""Optional dialogue TTS stage. Preview is the default; --audio-submit is required for paid API calls."""
from __future__ import annotations

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


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "audio"


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
        result.append({
            "name": name, "url": url, "model": model, "key": key,
            "voice": str(item.get("voice") or "alloy"),
            "response_format": str(item.get("response_format") or "mp3"),
            "timeout": max(10, int(item.get("timeout", 120))),
            "instructions_field": str(item.get("instructions_field") or "instructions"),
            "extra_body": item.get("extra_body") if isinstance(item.get("extra_body"), dict) else {},
        })
    return result


def _voice_instruction(character: dict, delivery: str) -> str:
    voice = character.get("voice") if isinstance(character.get("voice"), dict) else {}
    # voice.prompt is the English TTS voice-design description; local-language fields are a fallback.
    parts = [str(voice.get("prompt") or "").strip()]
    if not parts[0]:
        parts = [str(voice.get(k) or "").strip()
                 for k in ("timbre", "pitch", "pace", "accent", "emotion", "referenceHint")]
    if delivery.strip():
        parts.append("Performance for this line: " + delivery.strip())
    return "\n".join(p for p in parts if p)


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
                item_id = f"E{ep:02d}-S{scene_idx:02d}-L{beat_idx:03d}"
                items.append({
                    "id": item_id, "ep": ep, "sceneId": scene.get("sceneId"),
                    "speaker": speaker, "character_name": name,
                    "text": beat["line"].strip(), "delivery": str(beat.get("delivery") or ""),
                    "voice_instruction": _voice_instruction(character, str(beat.get("delivery") or "")),
                    "state": "planned", "audio_path": None, "provider": None, "error": None,
                })
    if not items:
        raise ValueError("所选集没有可配音的台词；检查 script.json 或 --eps")
    return {"version": 1, "source": ctx.title, "items": items}


def _synthesize(item: dict, cfg: dict, target: Path) -> None:
    body = {
        "model": cfg["model"], "input": item["text"], "voice": cfg["voice"],
        "response_format": cfg["response_format"],
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
    if "json" in content_type:
        try:
            result = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("TTS API 返回 JSON 类型但内容不可解析") from exc
        raise RuntimeError("TTS API 返回 JSON 而不是音频二进制；请使用直接返回音频文件的 speech endpoint。响应字段：" +
                           ", ".join(result.keys() if isinstance(result, dict) else ["非对象 JSON"]))
    if payload.lstrip().startswith((b"{", b"[")):
        raise RuntimeError("TTS API 返回了 JSON 文本而不是音频；请检查 speech_url")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        tmp.write_bytes(payload)
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink()


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
    if path.is_file() and not ctx.force:
        try:
            old = read_json(path)
            old_items = {str(x.get("id")): x for x in old.get("items", [])}
        except (OSError, ValueError):
            pass
    for item in manifest["items"]:
        old = old_items.get(item["id"])
        if old and old.get("text") == item["text"] and old.get("voice_instruction") == item["voice_instruction"]:
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
            f"Character: {item['character_name']}\nVoice instruction: {item['voice_instruction']}\n"
            f"Delivery: {item['delivery']}\nDialogue: {item['text']}\n", encoding="utf-8")
    qc = ["# 配音人工质检清单", "", "- [ ] 角色音色与角色卡一致",
          "- [ ] 台词完整，无漏字、错字或重复", "- [ ] 情绪、语速、停顿符合剧本",
          "- [ ] 发音清晰，无爆音、杂音或截断", "", "## 台词状态", ""]
    qc += [f"- [ ] {x['id']}：{x['state']}（{x['character_name']}：{x['text']}）"
           for x in manifest["items"]]
    (root / "qc-checklist.md").write_text("\n".join(qc) + "\n", encoding="utf-8")
    ctx.log(f"音频阶段：已整理 {len(manifest['items'])} 句台词及音色指令：{path}")
    if not submit:
        ctx.log("预览模式：未调用 TTS API。配置 models.json 的 audio 后加 --audio-submit 才会生成音频。")
        return path

    providers = _load_audio_config()
    for item in manifest["items"]:
        target = root / "lines" / (_safe(item["id"]) + "." + providers[0]["response_format"])
        if item.get("state") == "succeeded" and item.get("audio_path") and (ctx.out / item["audio_path"]).is_file():
            continue
        failures = []
        for cfg in providers:
            try:
                _synthesize(item, cfg, target.with_suffix("." + cfg["response_format"]))
                item.update(state="succeeded", audio_path=str(target.with_suffix("." + cfg["response_format"]).relative_to(ctx.out)),
                            provider=cfg["name"], error=None)
                break
            except Exception as exc:
                failures.append(f"{cfg['name']}: {exc}")
        else:
            item.update(state="failed", error="；".join(failures))
        write_json(path, manifest)
        ctx.log(f"  {item['id']}：{item['state']}" + (f"；{item['error']}" if item.get("error") else ""))
    return path
