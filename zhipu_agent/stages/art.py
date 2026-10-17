"""阶段 3：novel-art —— 大纲 → 美术设定集（art.json：场景 + 叙事道具）。

流程（对应 skills/novel-art/SKILL.md）：
  seed      从 outline.json 搬运场景 / 道具清单（id、出现集、承载爽点）
  场景      每个场景一次模型调用：一致性锚点、光照状态、空景提示词
  道具      大纲有 props 就逐件填设计字段；没有就让模型从材料里挑 3–8 件
  校验      validate（10 道质量门；给了 cast 就查提示词不含角色名）→ 按场景名 / 道具名回炉
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..skill_loader import build_system
from ..validator import Result
from ..visual_style import visual_style_instruction
from .base import Context, Item, compact, read_json, run_items, write_json

SKILL = "novel-art"
DROP = ("image", "usage")  # 参考资料里去掉的长字段


def artifact(ctx: Context) -> Path:
    return ctx.artifact("art")


def _cast_args(ctx: Context) -> List[Any]:
    cast = ctx.artifact("characters")
    return ["--cast", cast] if cast.is_file() else []


def check(ctx: Context) -> Result:
    return ctx.run(ctx.skill(SKILL), ["validate", artifact(ctx), *_cast_args(ctx)])


def _episodes_using(outline: Dict[str, Any], key: str, ident: str) -> List[Dict[str, Any]]:
    return [
        {"ep": e.get("ep"), "synopsis": (e.get("synopsis") or "")[:300]}
        for e in outline.get("episodes", []) if ident in (e.get(key) or [])
    ]


def run(ctx: Context, visual_style: str = "manju") -> Path:
    style_hint = visual_style_instruction(visual_style)
    sk = ctx.skill(SKILL)
    work = ctx.work("art")
    outline_path = ctx.artifact("outline")
    if not outline_path.is_file():
        raise FileNotFoundError(f"需要先有大纲：{outline_path}（先运行 outline 阶段）")
    outline = read_json(outline_path)
    path = artifact(ctx)

    doc = ctx.node_json(sk, ["seed", outline_path])
    doc.setdefault("props", [])
    write_json(path, doc)

    cast_names: List[str] = []
    cast_path = ctx.artifact("characters")
    if cast_path.is_file():
        cast_names = [c["name"] for c in read_json(cast_path).get("characters", [])]
    else:
        cast_names = [c.get("name", "") for c in outline.get("characters", [])]
    core = (outline.get("adaptation") or {}).get("core", "")
    scene_names = [s["name"] for s in doc["scenes"]]
    avoid = (f"出图提示词（image.prompt、各光照状态的 prompt 等）不得出现这些角色名：{'、'.join(n for n in cast_names if n)}；"
             "场景必须是无人空景。") if cast_names else "场景必须是无人空景。"

    scene_system = build_system(sk, ["scene-pass.md"])
    prop_system = build_system(sk, ["prop-pass.md"])
    items: List[Item] = []
    kinds: Dict[str, str] = {}

    for scene in doc["scenes"]:
        key = f"scene:{scene['id']}"
        kinds[key] = "scene"
        prompt = (
            f"## 任务\n为场景「{scene['name']}」（{scene['id']}）产出完整的场景设定。\n\n"
            f"## 骨架（seed 已预填，id / 名称 / 主场景标记 / 出现集不要改）\n"
            f"{json.dumps(compact(scene, 300), ensure_ascii=False, indent=1)}\n\n"
            f"## 改编核心\n{core}\n\n"
            f"## 用到这个场景的分集（光照状态要从分集反推，不写用不上的）\n"
            f"{json.dumps(_episodes_using(outline, 'sceneIds', scene['id']), ensure_ascii=False, indent=1)}\n\n"
            f"## 同批其他场景（空间气质要区分开）\n{'、'.join(n for n in scene_names if n != scene['name']) or '（无）'}\n\n"
            f"## 视觉风格（场景图提示词必须遵守）\n{style_hint}\n\n"
            f"## 约束\n{avoid}\n\n"
            "## 输出\n只输出这一个场景的 JSON 对象（结构见《数据结构》的 scenes 段）。"
        )
        items.append(Item(key, scene_system, prompt, tokens=[scene["name"], scene["id"]],
                          cache=work / f"scene-{scene['id']}-{visual_style}.json"))

    if doc["props"]:
        for prop in doc["props"]:
            key = f"prop:{prop['id']}"
            kinds[key] = "prop"
            prompt = (
                f"## 任务\n为叙事道具「{prop['name']}」（{prop['id']}）填设计字段。\n\n"
                f"## 骨架（seed 已预填，id / 名称 / 戏剧功能不要改）\n"
                f"{json.dumps(compact(prop, 300), ensure_ascii=False, indent=1)}\n\n"
                f"## 改编核心\n{core}\n\n"
                f"## 出现这件道具的分集\n"
                f"{json.dumps(_episodes_using(outline, 'propIds', prop['id']), ensure_ascii=False, indent=1)}\n\n"
                f"## 登记过的场景\n{'、'.join(scene_names)}\n\n"
                f"## 视觉风格（道具图提示词必须遵守）\n{style_hint}\n\n"
                f"## 约束\n{avoid}道具图要白底、无手、提示词里带尺度短语。\n\n"
                "## 输出\n只输出这一件道具的 JSON 对象（结构见《数据结构》的 props 段）。"
            )
            items.append(Item(key, prop_system, prompt, tokens=[prop["name"], prop["id"]],
                              cache=work / f"prop-{prop['id']}-{visual_style}.json"))
    else:
        kinds["props"] = "props"
        prompt = (
            f"## 任务\n大纲没有登记叙事道具。请先选再填：从下面的材料里挑 3–8 件有特写、跨集出现、承载剧情的道具。\n\n"
            f"## 改编核心\n{core}\n\n"
            f"## 分集梗概\n{json.dumps(compact(outline.get('episodes', []), 300), ensure_ascii=False, indent=1)}\n\n"
            f"## 登记过的场景\n{'、'.join(scene_names)}\n\n"
            f"## 视觉风格（道具图提示词必须遵守）\n{style_hint}\n\n## 约束\n{avoid}\n\n"
            "## 输出\n只输出 JSON 数组，每个元素是一件道具（结构见《数据结构》的 props 段）。"
        )
        items.append(Item("props", prop_system, prompt, tokens=["道具", "props"], cache=work / f"props-{visual_style}.json"))

    def apply(key: str, data: Any) -> None:
        kind = kinds[key]
        if kind == "props":
            if isinstance(data, dict):
                data = data.get("props") or [data]
            if not isinstance(data, list) or not data:
                raise ValueError("道具应当是非空的 JSON 数组")
            doc["props"] = data
        else:
            if not isinstance(data, dict):
                raise ValueError("应当是一个 JSON 对象")
            ident = key.split(":", 1)[1]
            slot = "scenes" if kind == "scene" else "props"
            for i, old in enumerate(doc[slot]):
                if old["id"] == ident:
                    new = dict(data)
                    new["id"], new["name"] = old["id"], old["name"]
                    if "primary" in old:
                        new["primary"] = old["primary"]
                    if old.get("usage"):
                        new["usage"] = old["usage"]
                    doc[slot][i] = new
                    break
        write_json(path, doc)

    run_items(ctx, items, apply=apply, validate=lambda: check(ctx), label="art-场景与道具")

    out_dir = ctx.dir("art")
    ctx.render_to(sk, ["render", path, "--md"], out_dir / f"{path.stem}.md")
    ctx.render_to(sk, ["render", path, "--html"], out_dir / "art-report.html")
    return path
