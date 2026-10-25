"""出图提示词优化：设定集的 image.prompt / image.sheet 在写进 JSON 之前，先交给远程文本模型按
FLUX 最佳实践（skills/flux-image-best-practices）重写一遍。

角色如果没有 image.sheet，则在优化阶段补齐多视角角色设定图：16:9 横构图、左侧面部基准半身像、右上正/侧/背全身三视图、右下细节条。
只改写法不改事实；输出要过本地检查（英文、无人名、版面/尺度/白底等关键短语还在），不过就带着原因重试一次，仍不过就保留原提示词。
关闭：环境变量 AGENT_FLUX_OPTIMIZE=0。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .llm import Conversation, extract_json
from .skill_loader import SkillError, build_system

SKILL = "flux-image-best-practices"
MODES = {
    "flux": (SKILL, "optimize-pass.md", ("core-principles.md", "t2i-prompting.md", "negative-prompt-alternatives.md")),
    "wan": ("video-wan-r2v", "optimize-pass.md", ()),
}
FIELDS = ("prompt", "sheet")
CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")
STRUCTURE_PHRASES = ("16:9", "white background", "front", "side", "back")
SHEET_REQUIRED = (
    "16:9",
    "white background",
    "LEFT ZONE",
    "RIGHT-TOP ZONE",
    "RIGHT-BOTTOM ZONE",
    "front",
    "side",
    "back",
)
STYLE_WORDS = re.compile(r"\b(photorealistic|anime|3d render|cgi|cel[- ]shad\w*|manga)\b", re.I)
RULE_FILES = ("core-principles.md", "t2i-prompting.md", "negative-prompt-alternatives.md")
RULE_CHARS = 6000


def enabled() -> bool:
    return os.environ.get("AGENT_FLUX_OPTIMIZE", "1").strip().lower() not in ("0", "false", "no", "off", "")


def _system(ctx, mode: str = "flux") -> str:
    skill, pass_name, rule_files = MODES[mode]
    sk = ctx.skill(skill)
    rules = []
    for name in rule_files:
        path = sk.root / "rules" / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            text = re.sub(r"\A---.*?\n---\n", "", text, flags=re.S)
            rules.append(f"## FLUX 规则：{name}\n{text[:RULE_CHARS]}")
    extra = ("# FLUX 最佳实践（上游规则节选）\n\n" + "\n\n".join(rules)) if rules else ""
    return build_system(sk, [pass_name], schema=False, extra=extra)


def _problems(original: Dict[str, str], new: Dict[str, str], forbid: Iterable[str], must_keep: Iterable[str], *, character_sheet: bool = False) -> List[str]:
    out: List[str] = []
    for field, old in original.items():
        text = new.get(field)
        if not isinstance(text, str) or not text.strip():
            out.append(f"{field} 缺失或为空")
            continue
        if CJK.search(text):
            out.append(f"{field} 含中文，必须全英文")
        for name in forbid:
            if name and name in text:
                out.append(f"{field} 出现了名字「{name}」，不许出现")
        low, old_low = text.lower(), old.lower()
        for phrase in STRUCTURE_PHRASES:
            if phrase in old_low and phrase not in low:
                out.append(f"{field} 丢了原有的结构短语「{phrase}」")
        for phrase in must_keep:
            if phrase and phrase.lower() in old_low and phrase.lower() not in low:
                out.append(f"{field} 丢了必须保留的短语「{phrase}」")
        if STYLE_WORDS.search(text) and not STYLE_WORDS.search(old):
            out.append(f"{field} 写进了画风词，画风由出图阶段统一加")
        if len(text) < len(old) * 0.35:
            out.append(f"{field} 比原文短太多（{len(text)} < {len(old)}×0.35），像是丢了内容")
    if character_sheet:
        sheet = new.get("sheet", "") if isinstance(new, dict) else ""
        low = sheet.lower()
        for phrase in SHEET_REQUIRED:
            if phrase.lower() not in low:
                out.append(f"sheet 缺少多视角设定图硬要求「{phrase}」")
    return out


def optimize_image(ctx, image: Dict[str, Any], *, kind: str, label: str,
                   forbid: Iterable[str] = (), must_keep: Iterable[str] = (),
                   mode: str = "flux", always: bool = False) -> Dict[str, Any]:
    """返回 image 字典的副本，prompt / sheet 已优化（失败则原样返回）。其他字段不动。

    character 若缺少 image.sheet，会把现有 image.prompt 作为事实来源，要求文本模型额外生成多视角
    角色设定图提示词；这样即使上游角色卡只有半身主视角，实际 sheet 出图也仍然采用多视角布局。
    """
    if not (always or enabled()) or not isinstance(image, dict):
        return image
    original: Dict[str, str] = {
        f: image[f] for f in FIELDS if isinstance(image.get(f), str) and image[f].strip()
    }
    character_sheet = kind == "character"
    if character_sheet and "prompt" in original and "sheet" not in original:
        original["sheet"] = original["prompt"]
    if not original:
        return image
    forbid = [n for n in forbid if n]
    must_keep = [m for m in must_keep if m]
    digest = hashlib.sha1(json.dumps([mode, kind, original, forbid, must_keep], ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()[:16]
    cache: Path = ctx.work("flux" if mode == "flux" else mode) / f"{digest}.json"
    result = dict(image)
    if cache.is_file() and not ctx.force:
        try:
            result.update(json.loads(cache.read_text(encoding="utf-8")))
            return result
        except (OSError, ValueError):
            pass

    try:
        conv = Conversation(_system(ctx, mode), ctx.settings, purpose=f"image-prompt-optimizer:{mode}")
    except SkillError as exc:
        ctx.log(f"⚠ FLUX 优化跳过（{exc}）")
        return image
    kind_hint = {
        "character": (
            "这是角色提示词（主体是人物）。角色必须产出可直接用于角色一致性参考的多视角设定图："
            "ONE 16:9 landscape canvas; LEFT ZONE about 34% of the canvas width with one bust portrait as the face anchor; "
            "RIGHT-TOP ZONE with three FULL-BODY views (front, side, back); RIGHT-BOTTOM ZONE with 4–5 detail studies; "
            "thin hairline rules; PROPORTIONS ARE CRITICAL; the detail studies give way, not the figures. "
            "不要只输出单人半身肖像。"
        ),
        "scene": "这是场景提示词（无人空景）。",
        "prop": "这是叙事道具提示词（白底、无手、带尺度短语）。",
    }.get(kind, "")
    ask = (f"## 任务\n优化「{label}」的出图提示词。{kind_hint}\n\n## 原提示词\n"
           f"{json.dumps(original, ensure_ascii=False, indent=1)}\n\n"
           f"## 必须原样保留的短语\n{json.dumps(sorted(set(must_keep)), ensure_ascii=False)}")
    try:
        for attempt in range(2):
            raw = conv.ask(ask)
            try:
                data = extract_json(raw)
            except Exception as exc:  # noqa: BLE001
                problems = [f"输出不是合法 JSON：{exc}"]
            else:
                new = {f: data.get(f) for f in original} if isinstance(data, dict) else {}
                problems = _problems(original, new, forbid, must_keep, character_sheet=character_sheet)
                if not problems:
                    result.update({f: new[f].strip() for f in original})
                    cache.write_text(json.dumps({f: result[f] for f in original}, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
                    ctx.log(f"[{mode}] {label}：提示词已优化")
                    return result
            ask = "上一次输出没通过检查，逐条修改后重新只输出 JSON：\n- " + "\n- ".join(problems)
        ctx.log(f"⚠ [{mode}] {label}：优化结果没过检查，保留原提示词（{'；'.join(problems)[:160]}）")
    except Exception as exc:  # noqa: BLE001 - 优化失败不拖垮流水线
        ctx.log(f"⚠ [{mode}] {label}：优化调用失败，保留原提示词（{str(exc)[:160]}）")
    return image
