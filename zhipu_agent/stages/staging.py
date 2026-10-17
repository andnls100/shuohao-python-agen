"""阶段 5：novel-staging —— 剧本 + 场景图 → 每场开场站位（staging.json）。

流程（对应 skills/novel-staging/SKILL.md）：
  seed      从 script.json 搬运地点、场次、出场人物
  认图      每个地点一次模型调用，认出能站能坐的东西。
            有场景图 + 配置了视觉模型（ZHIPU_VISION_MODEL）→ 看图认；否则按美术提示词推算（草稿）
  排站位    每个地点的场次按顺序分块，每块一次模型调用
  校验      先单独校验认图结果，再校验整份 staging.json；报错按地点名 / "第 N 集第 M 场" 回炉
  输出      staging-report.html（可拖动调整）+ blocking.txt（开场走位句，交给分镜照抄）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..skill_loader import build_system
from ..validator import Result
from .base import (
    Context, Item, batches, compact, read_json, run_items, scene_tokens, write_json,
)

SKILL = "novel-staging"
CHUNK = 8  # 同一个地点一次最多排几场


def artifact(ctx: Context) -> Path:
    return ctx.artifact("staging")


def _refs(ctx: Context) -> List[Any]:
    args: List[Any] = ["--script", ctx.artifact("script")]
    art = ctx.artifact("art")
    if art.is_file():
        args += ["--art", art]
    return args


def check(ctx: Context) -> Result:
    return ctx.run(ctx.skill(SKILL), ["validate", artifact(ctx), *_refs(ctx)])


def _find_image(ctx: Context, sk, name: str) -> Optional[Path]:
    """在场景图目录里找 <地点 slug>-sheet.png。"""
    folder = ctx.images_dir or (ctx.out / "art" / "images")
    slug = ctx.run(sk, ["slug", name]).stdout.strip() or name
    for stem in (slug, name):
        for suffix in (".png", ".jpg", ".jpeg", ".webp"):
            candidate = folder / f"{stem}-sheet{suffix}"
            if candidate.is_file():
                return candidate
    return None


def run(ctx: Context, eps: Optional[List[int]] = None) -> Path:
    sk = ctx.skill(SKILL)
    work = ctx.work("staging")
    script_path = ctx.artifact("script")
    art_path = ctx.artifact("art")
    for need in (script_path, art_path):
        if not need.is_file():
            raise FileNotFoundError(f"需要先有：{need}（先运行前面的阶段）")
    script = read_json(script_path)
    art_scenes = {s["id"]: s for s in read_json(art_path).get("scenes", [])}
    path = artifact(ctx)

    seed_args: List[Any] = ["seed", script_path, "--art", art_path]
    outline = ctx.artifact("outline")
    if outline.is_file():
        seed_args += ["--outline", outline]
    if eps:
        seed_args += ["--eps", f"{eps[0]}-{eps[-1]}"]
    doc = ctx.node_json(sk, seed_args)
    write_json(path, doc)

    # ---------- 阶段一：认图
    read_system = build_system(sk, ["read-pass.md"])
    modes: Dict[str, str] = {}
    items: List[Item] = []
    for loc in doc["locations"]:
        art = art_scenes.get(loc["sceneId"], {})
        image = _find_image(ctx, sk, loc["name"])
        mode = "prompt"
        images: List[Path] = []
        kind = "text"
        if image and ctx.settings.vision_endpoints():
            mode, images, kind = "picture", [image], "vision"
        elif image:
            ctx.log(f"⚠ 找到了「{loc['name']}」的场景图，但没有配置视觉模型（ZHIPU_VISION_MODEL 或 models.json 的 vision），只能按提示词推算（草稿）")
        else:
            ctx.log(f"「{loc['name']}」没有场景图，按美术提示词推算（草稿，出图后需要重新认一遍）")
        modes[loc["sceneId"]] = mode
        prompt = (
            f"## 任务\n认出地点「{loc['name']}」（{loc['sceneId']}）里人能站、能坐、能走过去的东西。\n\n"
            f"## 这个地点的美术设定\n{json.dumps(compact({k: art.get(k) for k in ('summary', 'anchors')}, 240), ensure_ascii=False, indent=1)}\n"
            f"主视角提示词：{(art.get('image') or {}).get('prompt', '')[:800]}\n\n"
            + ("## 场景图\n图已随消息附上：只看主视角（左上约 72% 宽 × 70% 高）认东西，box 按整张图量。\n"
               "readFrom 写 \"picture\"。\n\n" if mode == "picture" else
               "## 没有图\n按美术提示词和锚点推算。readFrom 写 \"prompt\"，不要写 box。\n\n")
            + f"## 输出\n只输出这个地点的 JSON 对象：{{sceneId, name, readFrom, items}}，items 共 4–20 件，"
              f"sceneId 固定为 {loc['sceneId']}，name 固定为「{loc['name']}」。"
        )
        items.append(Item(loc["sceneId"], read_system, prompt, tokens=[loc["name"], loc["sceneId"]],
                          images=images, kind=kind, cache=work / f"location-{loc['sceneId']}.json"))

    def apply_location(key: str, data: Any) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("地点应当是带 items 数组的 JSON 对象")
        for i, loc in enumerate(doc["locations"]):
            if loc["sceneId"] == key:
                new = dict(data)
                new["sceneId"], new["name"] = loc["sceneId"], loc["name"]
                new["readFrom"] = modes[key]
                if modes[key] == "prompt":
                    new["items"] = [{k: v for k, v in it.items() if k != "box"} for it in new["items"]]
                doc["locations"][i] = new
        write_json(path, doc)

    probe = work / "staging-locations.json"

    def validate_locations() -> Result:
        write_json(probe, {**doc, "scenes": []})
        return ctx.run(sk, ["validate", probe, *_refs(ctx)])

    run_items(ctx, items, apply=apply_location, validate=validate_locations, label="staging-认图")

    # ---------- 阶段二：排站位
    script_eps = {e["ep"]: e for e in script["episodes"]}
    by_loc: Dict[str, List[Dict[str, Any]]] = {}
    for scene in doc["scenes"]:
        by_loc.setdefault(scene["sceneId"], []).append(scene)
    loc_by_id = {loc["sceneId"]: loc for loc in doc["locations"]}
    place_system = build_system(sk, ["staging-pass.md"])
    items = []
    groups: Dict[str, List[tuple]] = {}
    for sid, scenes in by_loc.items():
        for n, chunk in enumerate(batches(scenes, CHUNK)):
            key = f"{sid}#{n}"
            groups[key] = [(s["ep"], s["scene"]) for s in chunk]
            blocks = []
            for s in chunk:
                ep_doc = script_eps.get(s["ep"], {})
                scenes_list = ep_doc.get("scenes", [])
                script_scene = scenes_list[s["scene"] - 1] if 0 < s["scene"] <= len(scenes_list) else {}
                blocks.append({"seed": s, "剧本": compact(script_scene.get("flow", []), 200)})
            prompt = (
                f"## 任务\n给地点「{loc_by_id[sid]['name']}」的这几场排开场站位（按顺序，保持位置连贯）。\n\n"
                f"## 这个地点认出来的东西（只能用这些）\n{json.dumps(loc_by_id[sid]['items'], ensure_ascii=False, indent=1)}\n\n"
                f"## 各场（seed 骨架 + 剧本节拍）\n{json.dumps(blocks, ensure_ascii=False, indent=1)}\n\n"
                "## 输出\n只输出 JSON 数组，每个元素是一场：{ep, scene, places, extras, extrasWhere, why}，"
                "places 里每个出场人物都要定 present / item / side / along / posture / facing。"
            )
            tokens = [t for ep, sc in groups[key] for t in scene_tokens(ep, sc)] + [loc_by_id[sid]["name"]]
            items.append(Item(key, place_system, prompt, tokens=tokens,
                              cache=work / f"scenes-{sid}-{n}.json"))

    def apply_scenes(key: str, data: Any) -> None:
        if isinstance(data, dict):
            data = data.get("scenes") or [data]
        got = {(s.get("ep"), s.get("scene")): s for s in data if isinstance(s, dict)}
        missing = [k for k in groups[key] if k not in got]
        if missing:
            raise ValueError(f"缺少这几场：{missing}")
        for scene in doc["scenes"]:
            new = got.get((scene["ep"], scene["scene"]))
            if new and (scene["ep"], scene["scene"]) in groups[key]:
                for field in ("places", "extras", "extrasWhere", "why"):
                    if field in new:
                        scene[field] = new[field]
        write_json(path, doc)

    run_items(ctx, items, apply=apply_scenes, validate=lambda: check(ctx), label="staging-排站位")

    # ---------- 输出
    out_dir = ctx.dir("staging")
    ctx.render_to(sk, ["render", path], out_dir / "staging-report.html")
    blocking = ctx.run(sk, ["blocking", path])
    if blocking.ok:
        (out_dir / "blocking.txt").write_text(blocking.stdout, encoding="utf-8")
        ctx.log("已生成 staging/blocking.txt（每场开场走位句，分镜第一段照抄）")
    return path
