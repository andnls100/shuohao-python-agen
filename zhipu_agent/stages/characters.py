"""阶段 2：novel-characters —— 大纲 + 原文 → 角色设定集（cast.json）。

流程（对应 skills/novel-characters/SKILL.md）：
  seed      有 outline.json 就预填角色清单（id / 分档 / 人物弧），不再自己判断谁重要
  扫描      chunk 分块 → 每块一次模型调用抽角色（roster）
  归并      merge 脚本按名字别名收敛；候选同人交给模型复核 → merges.json → --apply
  出卡      同批角色合并为一次模型调用，整体优化角色卡（人物画像 / 形象提示词 / 音色提示词）
  合成      assemble → validate（引文逐字、提示词不含人名、语言分工……）→ 按批次回炉
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..llm import Conversation, ask_text
from ..image_prompt_optimizer import optimize_image
from ..skill_loader import build_system
from ..validator import NodeError, Result
from ..visual_style import visual_style_instruction
from .base import Context, Item, compact, no_validate, parallel_map, read_json, run_items, safe_name, write_json

SKILL = "novel-characters"
BUILTIN_LANGS = ("zh", "en", "ja")
IMPORTANCE = ("protagonist", "major", "supporting", "minor")
CARD_BATCH = 10  # 每次请求合并几个角色；环境变量 AGENT_CARD_BATCH 可改

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

    # ---------- 第二趟：出卡（每 CARD_BATCH 个角色合并成一次请求；一批不合格只重来这一批）
    card_system = build_system(sk, ["profile-pass.md"])
    card_files: Dict[str, Path] = {
        target["name"]: work / f"card-{safe_name(target['name'])}.json"
        for target in cast_targets
    }
    # 名单之外的旧 card-*.json（已被合并 / 删掉的角色、旧版本的缓存）会被 assemble 一并读进去，先清掉
    keep_files = set(card_files.values())
    for stale in work.glob("card-*.json"):
        if stale not in keep_files:
            stale.unlink()
    lang_name = {"zh": "中文", "en": "English", "ja": "日本語"}[ctx.lang]
    size = max(1, int(os.environ.get("AGENT_CARD_BATCH", CARD_BATCH)))
    batches = [cast_targets[i:i + size] for i in range(0, len(cast_targets), size)]
    items: List[Item] = []
    batch_names: Dict[str, List[str]] = {}
    for number, group in enumerate(batches, 1):
        parts: List[str] = []
        group_names = [t["name"] for t in group]
        for index, target in enumerate(group, 1):
            name = target["name"]
            observed = _match_records(target, records) if seed else [r for r in records if r["name"] == name]
            obs_text = (json.dumps(observed, ensure_ascii=False, indent=1) if observed
                        else "（原文扫描里没有匹配到这个角色的记录，只能依据大纲；外观没有依据的地方明确标缺失，不要编造）")
            seed_text = json.dumps(compact(target, 300), ensure_ascii=False, indent=1) if seed else "（无大纲，按原文拆分）"
            parts.append(
                f"### 角色 {index}：{name}\n"
                f"#### 大纲里的定位（已拍板，不要推翻分档）\n{seed_text}\n\n"
                f"#### 原文观察记录与可引用的逐字片段\n{obs_text}\n"
            )
        others = [n for n in names if n not in group_names]
        prompt = (
            "## 任务\n"
            f"一次性为下面这 {len(group)} 个角色产出完整角色卡，并在同一次生成中整体优化人物之间的差异性。\n"
            "不要逐个角色单独套模板；先在这批角色之间建立区分，再分别完成每张卡。\n"
            f"报告语言 lang = {ctx.lang}（{lang_name}）：人类可读字段用这个语言；出图和 TTS 提示词永远英文。\n\n"
            f"## 视觉风格（image.prompt 必须遵守）\n{style_hint}\n\n"
            f"## 本批角色名单\n{'、'.join(group_names)}\n\n"
            f"## 其他批次的角色名（长相声线也要和他们区分开）\n{'、'.join(others) or '（无）'}\n\n"
            "## 角色资料\n"
            + "\n".join(parts)
            + "\n## 输出\n"
            "只输出 JSON 对象，结构为 {\"characters\":[...]}，数组中的每一项都是一张完整角色卡，且 name 必须与本批角色名单一一对应。\n"
            "persona.evidence 里的引文必须逐字取自对应角色的可引用片段；image.prompt、image.promptLocal、image.sheet 等出图提示词不得出现任何角色名、别名、作者名或作品名。"
        )
        key = f"characters-batch-{number}"
        batch_names[key] = group_names
        items.append(Item(
            key, card_system, prompt,
            tokens=[f"[{n}]" for n in group_names] + [f"card-{safe_name(n)}" for n in group_names] + group_names,
            cache=work / f"cache-{key}-{visual_style}.json",
        ))

    def _normalize_batch(key: str, data: Any) -> List[Dict[str, Any]]:
        expected_names = batch_names[key]
        if isinstance(data, dict) and isinstance(data.get("characters"), list):
            chars = data["characters"]
        elif isinstance(data, list):
            chars = data
        elif isinstance(data, dict) and isinstance(data.get("character"), dict):
            chars = [data["character"]]
        else:
            raise ValueError("批量角色卡应当是 {\"characters\": [...]}")
        if len(chars) != len(expected_names):
            raise ValueError(f"批量角色卡数量不正确：收到 {len(chars)} 张，应为 {len(expected_names)} 张")
        actual = [c.get("name") for c in chars if isinstance(c, dict)]
        if len(actual) != len(chars) or set(actual) != set(expected_names) or len(set(actual)) != len(actual):
            raise ValueError("批量角色卡的 name 必须与本批角色名单一一对应且不能重复")
        return chars

    def apply_batch(key: str, data: Any) -> None:
        by_name = {card["name"]: card for card in _normalize_batch(key, data)}
        cards: Dict[str, Dict[str, Any]] = {}
        for name in batch_names[key]:
            card = dict(by_name[name])
            seed_char = seed_by_name.get(name, {})
            card["name"] = name
            if seed_char.get("id"):
                card["id"] = seed_char["id"]
            wanted = seed_char.get("importance")
            if wanted:
                if not (wanted == "protagonist" and card.get("importance") in ("protagonist", "major")):
                    card["importance"] = wanted
            elif card.get("importance") not in IMPORTANCE:
                card["importance"] = "minor"
            cards[name] = card

        def optimize(name: str) -> Any:
            ban = [name, *[a for a in cards[name].get("aliases", []) if isinstance(a, str)]]
            return optimize_image(ctx, cards[name].get("image"), kind="character", label=name, forbid=ban)

        # 同批角色的 FLUX 优化并发跑（optimize_image 自己兜底，失败保留原提示词）
        results = parallel_map(optimize, list(cards), ctx.settings.concurrency)
        for name, image in zip(cards, results):
            if not isinstance(image, Exception):
                cards[name]["image"] = image
            write_json(card_files[name], cards[name])

    def validate() -> Result:
        assembled = ctx.run(sk, ["assemble", work, "--source", ctx.title, "--lang", ctx.lang, "--out", cast_path])
        return assembled if not assembled.ok else check(ctx)

    run_items(ctx, items, apply=apply_batch, validate=validate, label="characters-角色卡")

    out_dir = ctx.dir("characters")
    ctx.render_to(sk, ["render", cast_path, "--md"], out_dir / f"{cast_path.stem}.md")
    ctx.render_to(sk, ["render", cast_path, "--html"], out_dir / "report.html")
    return cast_path
