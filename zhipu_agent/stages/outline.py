"""阶段 1：novel-outline —— 小说 → 短剧改编大纲（outline.json）。

流程（对应 skills/novel-outline/SKILL.md 的 Step 0–7）：
  材料    短篇直接用全文；长篇 chunk 分卷 → 每卷一次模型调用写摘要
  骨架    模型写 adaptation / characters / scenes / props / beats → validate --stage beats
  拍板    交互模式下把"砍了哪条线、合了哪些人、大爆点在哪集"摆给用户，可输入修改意见
  分集    每批 ≤10 集并发写 → 合并 → validate（14 道质量门）→ 按集号回炉
  输出    outline.json + .md + outline-report.html
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..skill_loader import build_system
from ..validator import Result
from .base import (
    Context, Item, batches, ep_tokens, no_validate, read_json, run_items, short, write_json,
)

SKILL = "novel-outline"
SKELETON_KEYS = ("adaptation", "characters", "scenes", "props", "beats")
BATCH = 10  # 每批最多几集（skill 规定）


def artifact(ctx: Context) -> Path:
    return ctx.artifact("outline")


def check(ctx: Context) -> Result:
    return ctx.run(ctx.skill(SKILL), ["validate", artifact(ctx)])


def _materials(ctx: Context, work: Path) -> tuple:
    """返回 (材料说明, 材料正文)。"""
    sk = ctx.skill(SKILL)
    text = ctx.book_file.read_text(encoding="utf-8")
    if len(text) <= ctx.settings.direct_chars:
        ctx.log(f"原文 {len(text)} 字，直接使用全文（跳过分卷摘要）")
        return "整本原文", text

    info = ctx.node_json(sk, ["chunk", ctx.book_file, work, "--per-volume", "15"])
    if info.get("truncated"):
        ctx.log("⚠ 原文过长，尾部没有扫到（chunk 报 truncated），大纲不含被截掉的部分")
    volumes = sorted(work.glob("vol-*.txt"))
    system = build_system(sk, ["volume-pass.md"], schema=False)
    items = [
        Item(
            key=vol.stem,
            system=system,
            prompt=f"这是第 {i + 1} 卷（共 {len(volumes)} 卷）的原文：\n\n{vol.read_text(encoding='utf-8')}",
            cache=work / vol.name.replace("vol-", "summary-").replace(".txt", ".json"),
        )
        for i, vol in enumerate(volumes)
    ]
    summaries: Dict[str, Any] = {}
    run_items(
        ctx, items, label="outline-分卷摘要",
        apply=lambda key, data: summaries.__setitem__(key, data),
        validate=no_validate,
    )
    ordered = [{"volume": k, **summaries[k]} for k in sorted(summaries)]
    return f"全文分卷摘要（共 {len(ordered)} 卷；你看不到原文，只能依据摘要）", json.dumps(ordered, ensure_ascii=False, indent=1)


def _print_decisions(ctx: Context, doc: Dict[str, Any]) -> None:
    adaptation = doc.get("adaptation", {})
    ctx.log("—— 骨架已通过 beats 档校验，请拍板这三件事 ——")
    ctx.log(f"1) 砍了哪条线：{adaptation.get('cutNote') or short(adaptation.get('cut'), 600)}")
    ctx.log(f"2) 合了哪些人：{adaptation.get('mergeNote') or short(adaptation.get('merge'), 600)}")
    majors = [
        f"第 {b.get('episode')} 集「{b.get('type')}」"
        for b in doc.get("beats", []) if str(b.get("weight")) in ("major", "大", "大爆点")
    ]
    ctx.log(f"3) 大爆点落在：{'、'.join(majors) if majors else '（未标出 major，请看 beats）'}")


def run(ctx: Context, params: Dict[str, Any]) -> Path:
    sk = ctx.skill(SKILL)
    work = ctx.work("outline")
    path = artifact(ctx)
    total = int(params["episodes"])

    doc: Dict[str, Any] = {
        "source": ctx.title,
        "lang": ctx.lang,
        "params": {k: v for k, v in params.items() if v not in (None, "")},
        "adaptation": {}, "characters": [], "scenes": [], "props": [], "beats": [], "episodes": [],
    }
    resumed = False
    if path.is_file() and not ctx.force:
        old = read_json(path)
        if old.get("beats") and old.get("characters"):
            doc.update({k: old[k] for k in (*SKELETON_KEYS, "episodes") if k in old})
            resumed = True
            ctx.log("发现已有骨架，沿用（用 --force 可重做）")

    # ---------- 骨架
    if not resumed:
        kind, materials = _materials(ctx, work)
        system = build_system(sk, ["outline-pass.md"])
        prompt = (
            f"## 任务\n为小说《{ctx.title}》产出短剧改编大纲的「骨架」。\n\n"
            f"## 参数\n{json.dumps(doc['params'], ensure_ascii=False, indent=1)}\n\n"
            f"## 原料（{kind}）\n{materials}\n\n"
            "## 输出\n只输出一个 JSON 对象，键为：adaptation、characters、scenes、props、beats。\n"
            "不要写 episodes（分集梗概下一步才写）。字段与取值范围以《数据结构》为准；"
            "adaptation.keep 里的 evidence 必须逐字取自原料。"
        )

        def apply_skeleton(key: str, data: Any) -> None:
            if not isinstance(data, dict):
                raise ValueError("骨架应当是一个 JSON 对象")
            for k in SKELETON_KEYS:
                if k in data:
                    doc[k] = data[k]
            write_json(path, doc)

        def validate_skeleton() -> Result:
            return ctx.run(sk, ["validate", path, "--stage", "beats"])

        run_items(ctx, [Item("skeleton", system, prompt, tokens=[])],
                  apply=apply_skeleton, validate=validate_skeleton, label="outline-骨架")

        # ---------- 拍板（最多 3 轮修改）
        for _ in range(3):
            _print_decisions(ctx, doc)
            answer = ctx.ask("回车确认骨架并继续；或输入修改意见：")
            if not answer:
                break
            run_items(
                ctx,
                [Item("skeleton", system, f"用户对骨架的意见：{answer}\n请据此修改后，重新输出完整的 JSON 对象（五个键）。")],
                apply=apply_skeleton, validate=validate_skeleton, label="outline-骨架",
            )
        else:
            ctx.log("已达 3 轮修改上限，按当前骨架继续")

    # ---------- 分集梗概
    todo = [e for e in range(1, total + 1)
            if not (resumed and any(x.get("ep") == e for x in doc.get("episodes", [])))]
    if todo:
        system = build_system(sk, ["episode-pass.md"])
        skeleton = {k: doc[k] for k in SKELETON_KEYS}
        ranges: Dict[str, tuple] = {}
        items: List[Item] = []
        for group in batches(todo, BATCH):
            lo, hi = group[0], group[-1]
            key = f"{lo}-{hi}"
            ranges[key] = (lo, hi)
            beats_here = [b for b in doc["beats"] if isinstance(b.get("episode"), int) and lo <= b["episode"] <= hi]
            prompt = (
                f"## 任务\n写第 {lo}–{hi} 集（共 {len(group)} 集）的分集梗概。\n\n"
                f"## 已拍板的骨架\n{json.dumps(skeleton, ensure_ascii=False, indent=1)}\n\n"
                f"## 落在本区间的爽点\n{json.dumps(beats_here, ensure_ascii=False, indent=1)}\n\n"
                f"## 输出\n只输出 JSON 数组，每个元素是一集（ep、synopsis、hook、suspense、sceneIds、"
                f"characterIds、propIds、crowdPlan、warnings，按《数据结构》），ep 必须覆盖 {lo} 到 {hi}，不能漏集。"
            )
            tokens = [t for e in group for t in ep_tokens(e)]
            items.append(Item(key, system, prompt, tokens=tokens, cache=work / f"eps-{lo:03d}-{hi:03d}.json"))

        def apply_eps(key: str, data: Any) -> None:
            lo, hi = ranges[key]
            if isinstance(data, dict):
                data = data.get("episodes") or [data]
            got = {e["ep"]: e for e in data
                   if isinstance(e, dict) and isinstance(e.get("ep"), int) and lo <= e["ep"] <= hi}
            missing = sorted(set(range(lo, hi + 1)) - set(got))
            if missing:
                raise ValueError(f"缺少第 {missing} 集")
            merged = {e["ep"]: e for e in doc.get("episodes", [])}
            merged.update(got)
            doc["episodes"] = [merged[k] for k in sorted(merged)]
            write_json(path, doc)

        run_items(
            ctx, items, apply=apply_eps, label="outline-分集",
            validate=lambda: ctx.run(sk, ["validate", path]),
            hint="只输出你负责的这批集的 JSON 数组。",
        )
    else:
        result = check(ctx)
        if not result.ok:
            ctx.log("⚠ 已有大纲未通过校验：\n" + result.text)

    # ---------- 输出
    out_dir = ctx.dir("outline")
    ctx.render_to(sk, ["render", path, "--md"], out_dir / f"{path.stem}.md")
    ctx.render_to(sk, ["render", path, "--html"], out_dir / "outline-report.html")
    return path
