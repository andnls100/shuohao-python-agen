"""阶段 2：novel-characters —— 大纲 + 原文 → 角色设定集（cast.json）。

流程（对应 skills/novel-characters/SKILL.md）：
  seed      有 outline.json 就预填角色清单（id / 分档 / 人物弧），不再自己判断谁重要
  扫描      chunk 分块 → 每块一次模型调用抽角色（roster）
  归并      merge 脚本按名字别名收敛；候选同人交给模型复核 → merges.json → --apply
  出卡      每个角色一次模型调用写角色卡（人物画像 / 形象提示词 / 音色提示词）
  合成      assemble → validate（引文逐字、提示词不含人名、语言分工……）→ 按角色名回炉
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..llm import Conversation, ask_text
from ..skill_loader import build_system
from ..validator import NodeError, Result
from ..visual_style import visual_style_instruction
from .base import Context, Item, compact, no_validate, read_json, run_items, safe_name, write_json

SKILL = "novel-characters"
BUILTIN_LANGS = ("zh", "en", "ja")
IMPORTANCE = ("protagonist", "major", "supporting", "minor")

REVIEW_SYSTEM = """你在复核角色归并候选。脚本按名字精确收敛后，剩下一批"可能是同一个人"的候选。
判断规则：名字有包含关系只是强信号，不是判决——同姓的父子、兄弟、同事不能合并；
同一个人的不同称呼（全名与简称、官职与姓名、绰号）才合并。
只输出 JSON：{"merges":[{"keep":"保留的名字","absorb":["并入的名字或别名"]}]}。
没有要合并的就输出 {"merges":[]}。不要解释。"""

SUMMARY_SYSTEM = """你是编辑。为下面这几个角色所在的故事写一段「故事摘要」。
要求：{lang_name}，3–5 句，交代时空背景、核心情境、这几个人聚在一起的由头；
不剧透结局，不写成推荐语。只输出摘要正文。"""


def artifact(ctx: Context) -> Path:
    return ctx.artifact("characters")


def check(ctx: Context) -> Result:
    return ctx.run(ctx.skill(SKILL), ["validate", artifact(ctx), ctx.book_file, "--lang", ctx.lang])


def _match_records(seed_char: Dict[str, Any], merged: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把大纲里的角色对到原文扫描出的记录（名字 / 别名 / 来源说明互相包含）。"""
    hay = " ".join(str(seed_char.get(k) or "") for k in ("name", "from", "seedNote"))
    name = seed_char["name"]
    hits = []
    for rec in merged:
        names = [rec.get("name", ""), *rec.get("aliases", [])]
        if any(n and len(n) >= 2 and (n in hay or name in n or n in name) for n in names):
            hits.append(rec)
    return hits


def _normalize_roster(data: Any) -> Dict[str, Any]:
    if isinstance(data, list):
        data = {"characters": data}
    if not isinstance(data, dict) or not isinstance(data.get("characters"), list):
        raise ValueError("roster 应当是 {\"characters\": [...]}")
    for c in data["characters"]:
        c.setdefault("aliases", [])
        c.setdefault("quotes", [])
        c.setdefault("note", "")
    return data


def run(ctx: Context, max_chars: int = 30, visual_style: str = "manju") -> Path:
    if ctx.lang not in BUILTIN_LANGS:
        raise ValueError(f"目前只支持 {BUILTIN_LANGS} 作为角色报告语言（其他语言需要先翻译界面文案）")
    style_hint = visual_style_instruction(visual_style)
    sk = ctx.skill(SKILL)
    work = ctx.work("characters")
    cast_path = artifact(ctx)
    cast_path.parent.mkdir(parents=True, exist_ok=True)  # assemble 不会自己建输出目录

    # ---------- seed
    outline = ctx.artifact("outline")
    seed: Optional[Dict[str, Any]] = None
    if outline.is_file():
        seed = ctx.node_json(sk, ["seed", outline])
        write_json(work / "seed.json", seed)
        ctx.log(f"已从大纲预填 {len(seed['characters'])} 个角色")
    else:
        ctx.log("没有 outline.json，直接从原文拆角色表")

    # ---------- 第一趟：扫描
    info = ctx.node_json(sk, ["chunk", ctx.book_file, work])
    if info.get("truncated"):
        ctx.log("⚠ 原文过长，尾部没有扫到（chunk 报 truncated）")
    chunks = sorted(work.glob("chunk-*.txt"))
    roster_system = build_system(sk, ["roster-pass.md"], schema=False)
    items = []
    targets: Dict[str, Path] = {}
    for i, chunk in enumerate(chunks):
        roster_file = work / chunk.name.replace("chunk-", "roster-").replace(".txt", ".json")
        if roster_file.is_file() and not ctx.force:
            continue
        key = chunk.stem
        targets[key] = roster_file
        items.append(Item(key, roster_system,
                          f"这是第 {i + 1} 块原文（共 {len(chunks)} 块）：\n\n{chunk.read_text(encoding='utf-8')}"))
    if items:
        run_items(
            ctx, items, label="characters-扫描", validate=no_validate,
            apply=lambda key, data: write_json(targets[key], _normalize_roster(data)),
        )

    # ---------- 归并 + 复核
    merged = ctx.node_json(sk, ["merge", work])
    write_json(work / "merged.json", merged)
    candidates = merged.get("mergeCandidates") or []
    if candidates:
        ctx.log(f"复核 {len(candidates)} 条归并候选")
        review = {
            "characters": [{"name": c["name"], "aliases": c.get("aliases", []), "note": (c.get("note") or "")[:120]}
                           for c in merged["characters"]],
            "mergeCandidates": candidates,
        }
        decision = Conversation(REVIEW_SYSTEM, ctx.settings).ask_json(json.dumps(review, ensure_ascii=False, indent=1))
        merges = decision.get("merges", []) if isinstance(decision, dict) else []
        if merges:
            write_json(work / "merges.json", {"merges": merges})
            try:
                merged = ctx.node_json(sk, ["merge", work, "--apply", work / "merges.json"])
                write_json(work / "merged.json", merged)
                ctx.log(f"已合并 {len(merges)} 组")
            except NodeError as exc:
                ctx.log(f"⚠ 合并决定没能落地，保留未合并结果：{str(exc)[:200]}")

    # ---------- 选角
    records: List[Dict[str, Any]] = merged["characters"]
    if seed:
        cast_targets = seed["characters"]
    else:
        cast_targets = [{"name": r["name"]} for r in records[:max_chars]]
    seed_by_name = {c["name"]: c for c in cast_targets}
    names = list(seed_by_name)
    if len(records) > len(cast_targets) and not seed:
        ctx.log(f"还识别出 {len(records) - max_chars} 位角色没做画像")

    # ---------- 故事摘要
    summary_file = work / "summary.txt"
    if not summary_file.is_file() or ctx.force:
        lang_name = {"zh": "中文", "en": "English", "ja": "日本語"}[ctx.lang]
        notes = [{"name": r["name"], "note": (r.get("note") or "")[:200]} for r in records[:12]]
        core = ""
        if outline.is_file():
            core = json.dumps(compact(read_json(outline).get("adaptation", {}).get("core", "")), ensure_ascii=False)
        summary_file.write_text(
            ask_text(SUMMARY_SYSTEM.format(lang_name=lang_name),
                     f"角色观察记录：\n{json.dumps(notes, ensure_ascii=False, indent=1)}\n\n改编核心（可能为空）：{core}",
                     ctx.settings),
            encoding="utf-8",
        )

    # ---------- 第二趟：出卡
    card_system = build_system(sk, ["profile-pass.md"])
    items = []
    card_files: Dict[str, Path] = {}
    for target in cast_targets:
        name = target["name"]
        card_files[name] = work / f"card-{safe_name(name)}.json"
        observed = _match_records(target, records) if seed else [r for r in records if r["name"] == name]
        obs_text = (json.dumps(observed, ensure_ascii=False, indent=1) if observed
                    else "（原文扫描里没有匹配到这个角色的记录，只能依据大纲；外观没有依据的地方明确标缺失，不要编造）")
        seed_text = json.dumps(compact(target, 300), ensure_ascii=False, indent=1) if seed else "（无大纲，按原文拆分）"
        lang_name = {"zh": "中文", "en": "English", "ja": "日本語"}[ctx.lang]
        prompt = (
            f"## 任务\n为角色「{name}」产出一张完整的角色卡。\n\n"
            f"报告语言 lang = {ctx.lang}（{lang_name}）：人类可读字段用这个语言；出图和 TTS 提示词永远英文。\n\n"
            f"## 视觉风格（image.prompt 必须遵守）\n{style_hint}\n\n"
            f"## 大纲里的定位（已拍板，不要推翻分档）\n{seed_text}\n\n"
            f"## 原文观察记录与可引用的逐字片段\n{obs_text}\n\n"
            f"## 同批其他角色的名字（避免长相声线撞车）\n{'、'.join(n for n in names if n != name) or '（无）'}\n\n"
            "## 输出\n只输出这一个角色的角色卡 JSON 对象（结构见《数据结构》的「单张角色卡」）。"
            "persona.evidence 里的引文必须逐字取自上面的片段；image.prompt 等出图提示词不得出现任何角色名。"
        )
        # 缓存文件名不能以 card- 开头：校验器会把 work 目录里所有 card-*.json 当成角色卡，重名就报「有两份卡」。
        cache_path = work / f"cache-{safe_name(name)}-{visual_style}.json"
        legacy = work / f"card-{safe_name(name)}-{visual_style}.json"
        if legacy.is_file():  # 旧版本留下的缓存：改名沿用，同时让它不再被当成角色卡
            if cache_path.exists():
                legacy.unlink()
            else:
                legacy.replace(cache_path)
        # 报错里角色名有两种写法："[陆行远] image.prompt …" 和 "胡二爷 与 岸上挑灯的更夫 的提示词雷同"
        items.append(Item(name, card_system, prompt,
                          tokens=[f"[{name}]", f"card-{safe_name(name)}", name], cache=cache_path))

    def apply_card(key: str, data: Any) -> None:
        if isinstance(data, dict) and isinstance(data.get("character"), dict):
            data = data["character"]
        if not isinstance(data, dict):
            raise ValueError("角色卡应当是一个 JSON 对象")
        card = dict(data)
        card["name"] = key
        seed_char = seed_by_name.get(key, {})
        if seed_char.get("id"):
            card["id"] = seed_char["id"]
        wanted = seed_char.get("importance")
        if wanted:  # 大纲定的分档不推翻；主角组内部 protagonist/major 可以细分
            if not (wanted == "protagonist" and card.get("importance") in ("protagonist", "major")):
                card["importance"] = wanted
        elif card.get("importance") not in IMPORTANCE:
            card["importance"] = "minor"
        write_json(card_files[key], card)

    def validate() -> Result:
        assembled = ctx.run(sk, ["assemble", work, "--source", ctx.title, "--lang", ctx.lang, "--out", cast_path])
        return assembled if not assembled.ok else check(ctx)

    run_items(ctx, items, apply=apply_card, validate=validate, label="characters-角色卡")

    out_dir = ctx.dir("characters")
    ctx.render_to(sk, ["render", cast_path, "--md"], out_dir / f"{cast_path.stem}.md")
    ctx.render_to(sk, ["render", cast_path, "--html"], out_dir / "report.html")
    return cast_path
