"""阶段 7：character-refs —— 角色设定 → 角色参考图（asset.json + PNG）。

和前六个阶段不同：这个技能会真的调用出图模型（Qwen/ComfyUI、codex、OpenAI 兼容接口或自定义命令），
出图模型由 character-refs 自己的 config 管理，不使用 ZHIPU_* 的对话模型。

本模块负责：
  1. 让对话模型把角色卡拆成 intake.json（技能的"一次性输入"）
  2. intake-check 校验 → 按角色名回炉
  3. new 建资产；如果指定了 gen 且出图模型已配置好，再 gen / check / render

没有配置出图模型时只做到 new，并告诉你该运行哪条命令。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..skill_loader import build_system
from ..validator import Result
from .base import Context, Item, compact, read_json, run_items, safe_name, write_json

SKILL = "character-refs"
DEFAULT_IMPORTANCE = ("protagonist", "major")


def artifact(ctx: Context) -> Path:
    return ctx.out / "character-refs"


def check(ctx: Context) -> Result:
    return Result(0)


def run(
    ctx: Context,
    names: Optional[List[str]] = None,
    look: str = "动漫",
    gen: bool = False,
) -> Path:
    sk = ctx.skill(SKILL)
    work = ctx.work("refs")
    refs_dir = artifact(ctx)
    cast_path = ctx.artifact("characters")
    if not cast_path.is_file():
        raise FileNotFoundError(f"需要先有角色设定：{cast_path}（先运行 characters 阶段）")
    cast = read_json(cast_path)

    chosen = [c for c in cast["characters"]
              if (c["name"] in names if names else c.get("importance") in DEFAULT_IMPORTANCE)]
    if not chosen:
        raise ValueError("没有选中任何角色（用 --refs-names 指定，或确认 cast.json 里有主角 / 重要配角）")
    ctx.log(f"为 {len(chosen)} 个角色整理 intake：{'、'.join(c['name'] for c in chosen)}")

    template = ctx.run(sk, ["intake-template"]).stdout
    system = build_system(
        sk, ["intake.md"], schema=False,
        extra=f"# intake 模板（输出必须是这个结构）\n\n{template}",
    )
    files: Dict[str, Path] = {c["name"]: work / f"{safe_name(c['name'])}-intake.json" for c in chosen}
    items: List[Item] = []
    for c in chosen:
        persona = c.get("persona", {})
        material = compact({
            "name": c["name"], "oneLiner": c.get("oneLiner"), "aliases": c.get("aliases"),
            "persona": {k: persona.get(k) for k in ("gender", "ageRange", "identity", "appearance", "temperament")},
            "已有的英文形象提示词（可参考，不要直接照搬人名等）": (c.get("image") or {}).get("prompt"),
        }, 600)
        prompt = (
            f"## 任务\n把角色「{c['name']}」的资料拆成 intake.json。\n\n"
            f"## 角色资料\n{json.dumps(material, ensure_ascii=False, indent=1)}\n\n"
            f"## 故事背景\n{cast.get('summary', '')}\n\n"
            f"lang = {cast.get('lang', ctx.lang)}。资料里写明的标 stated，由资料推出来的标 inferred，按惯例补的标 default。\n"
            "资料里已有性别和年龄区间，年代取自故事背景。\n\n"
            "## 输出\n只输出 intake.json 的 JSON 对象。"
        )
        items.append(Item(c["name"], system, prompt, tokens=[f"[{c['name']}]", c["name"]],
                          cache=files[c["name"]]))

    def apply(key: str, data: Any) -> None:
        if not isinstance(data, dict):
            raise ValueError("intake 应当是一个 JSON 对象")
        data = {k: v for k, v in data.items() if k != "_说明"}
        data["name"] = key
        write_json(files[key], data)

    def validate() -> Result:
        failed: List[str] = []
        for name, file in files.items():
            if not file.is_file():
                continue
            result = ctx.run(sk, ["intake-check", file])
            if not result.ok:
                failed += [f"[{name}] {line}" for line in (result.violations or [result.text])]
        return Result(1 if failed else 0, "\n".join(failed))

    run_items(ctx, items, apply=apply, validate=validate, label="refs-intake")

    refs_dir.mkdir(parents=True, exist_ok=True)
    assets: List[Path] = []
    for name, file in files.items():
        result = ctx.run(sk, ["new", file, "--out", refs_dir, "--look", look])
        if not result.ok:
            ctx.log(f"⚠ 建资产失败（{name}）：{result.text[:200]}")
            continue
        assets.append(refs_dir / name / "asset.json")
    ctx.log(f"已建 {len(assets)} 个角色资产：{refs_dir}")

    config = ctx.run(sk, ["config", "--show"])
    if not gen:
        ctx.log("没有指定出图。要真正出图，先配置出图模型，再运行：\n"
                f"  node {sk.script} config --model qwen --confirm-anchor yes ...\n"
                f"  node {sk.script} gen <asset.json>")
        return refs_dir
    if "还缺" in config.text:
        ctx.log("⚠ 出图模型还没配置（character-refs 的 config 里缺主图 / 派生图模型），已跳过出图。\n"
                "  见 skills/character-refs/references/models.md；配置好后重新运行本阶段并加 --refs-gen。")
        return refs_dir

    for asset in assets:
        gen_result = ctx.run(sk, ["gen", asset])
        ctx.log(f"出图 {asset.parent.name}：{'完成' if gen_result.ok else '失败 ' + gen_result.text[:200]}")
        ctx.run(sk, ["check", asset])
    if assets:
        report = refs_dir / "character-refs.html"
        rendered = ctx.run(sk, ["render", *assets, "--out", report])
        ctx.log(f"已生成 {report}" if rendered.ok else f"⚠ 渲染报告失败：{rendered.text[:200]}")
    return refs_dir
