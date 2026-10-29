"""阶段 4：novel-script —— 分集梗概 → 剧本（script.json：场次 + 节拍流 + 台词）。

流程（对应 skills/novel-script/SKILL.md）：
  seed      从 outline.json 搬运每集的目标秒数、钩子、悬念、认领的爽点
  写戏      每批 ≤3 集；批内每集一次模型调用并发写；下一批接着上一批的结尾
  校验      每批 validate（时长 ±15%、单句 ≤35 字、钩子前 3 拍兑现、对账大纲和美术……）
            → 按"第 N 集"把报错归给那一集回炉
  输出      全剧再校验一遍，渲染 md / html
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..skill_loader import build_system
from ..validator import Result
from .base import (
    Context, Item, batches, compact, ep_tokens, read_json, run_items, write_json,
)

SKILL = "novel-script"
BATCH = 3  # 剧本是全管线改得最凶的一层，小批量出（skill 规定默认 ≤3 集）


def artifact(ctx: Context) -> Path:
    return ctx.artifact("script")


def _upstream(ctx: Context) -> List[Any]:
    args: List[Any] = ["--outline", ctx.artifact("outline")]
    art = ctx.artifact("art")
    if art.is_file():
        args += ["--art", art]
    return args


def check(ctx: Context) -> Result:
    return ctx.run(ctx.skill(SKILL), ["validate", artifact(ctx), *_upstream(ctx)])


def _scene_card(scene: Dict[str, Any]) -> Dict[str, Any]:
    return compact(
        {k: scene.get(k) for k in ("id", "name", "summary", "anchors", "lighting")},
        maxlen=140,
    )


def _ending(prev: Optional[Dict[str, Any]], outline_prev: Optional[Dict[str, Any]]) -> str:
    """上一集怎么收尾：优先用已写好的最后两拍，否则用大纲里的悬念。"""
    if prev and prev.get("scenes"):
        flow = prev["scenes"][-1].get("flow", [])[-2:]
        return json.dumps(flow, ensure_ascii=False)
    if outline_prev:
        return f"（上一集尚未写成，大纲里它的悬念是：{outline_prev.get('suspense', '')}）"
    return "（这是第一集，没有上一集）"


def repair_hook_beat(episode: Dict[str, Any]) -> bool:
    """hookBeat 是 [场号, 拍号]，都从 1 数起；模型常按 0 起写成 [0, 0]。含 0 时整体 +1（仍须通过后续校验）。"""
    hb = episode.get("hookBeat")
    if (isinstance(hb, list) and len(hb) == 2
            and all(isinstance(x, int) and not isinstance(x, bool) for x in hb) and 0 in hb):
        episode["hookBeat"] = [x + 1 for x in hb]
        return True
    return False


def repair_speakers(episode: Dict[str, Any], known_ids: set) -> List[str]:
    """说话人是 outline 里已有的角色、却没写进本场 characters 时，把他补进本场人物（VO 不动，未知 ID 不动）。"""
    fixed: List[str] = []
    for scene in episode.get("scenes", []) or []:
        if not isinstance(scene, dict):
            continue
        cast = scene.setdefault("characters", [])
        if not isinstance(cast, list):
            continue
        for beat in scene.get("flow", []) or []:
            speaker = beat.get("speaker") if isinstance(beat, dict) else None
            if isinstance(speaker, str) and speaker != "VO" and speaker in known_ids and speaker not in cast:
                cast.append(speaker)
                fixed.append(f"{scene.get('sceneId', '?')}: 补入说话人 {speaker}")
    return fixed


def run(ctx: Context, eps: Optional[List[int]] = None) -> Path:
    sk = ctx.skill(SKILL)
    work = ctx.work("script")
    outline_path = ctx.artifact("outline")
    if not outline_path.is_file():
        raise FileNotFoundError(f"需要先有大纲：{outline_path}（先运行 outline 阶段）")
    outline = read_json(outline_path)
    outline_eps = {e["ep"]: e for e in outline.get("episodes", [])}
    wanted = eps or sorted(outline_eps)
    path = artifact(ctx)

    art_scenes: Dict[str, Any] = {}
    art_path = ctx.artifact("art")
    if art_path.is_file():
        art_scenes = {s["id"]: s for s in read_json(art_path).get("scenes", [])}
    cast_info: Dict[str, Any] = {}
    cast_path = ctx.artifact("characters")
    if cast_path.is_file():
        for c in read_json(cast_path).get("characters", []):
            p = c.get("persona", {})
            cast_info[c["name"]] = compact({
                "oneLiner": c.get("oneLiner"), "personality": p.get("personality"),
                "temperament": p.get("temperament"), "identity": p.get("identity"),
            }, 120)
    outline_chars = {c["id"]: c for c in outline.get("characters", [])}

    seed = ctx.node_json(sk, ["seed", outline_path, "--eps", f"{wanted[0]}-{wanted[-1]}"])
    seed_eps = {e["ep"]: e for e in seed["episodes"]}

    main: Dict[str, Any] = {"source": ctx.title, "episodes": []}
    if path.is_file() and not ctx.force:
        main = read_json(path)
    done = {e["ep"] for e in main["episodes"] if e.get("scenes")}
    todo = [e for e in wanted if e not in done and e in seed_eps]
    if not todo:
        if not path.is_file():
            raise ValueError(
                f"没有可写的集：--eps {eps} 在大纲里找不到对应集（大纲现有集：{sorted(outline_eps)}）")
        ctx.log("所有集都已写好，跳过写戏")
    system = build_system(sk, ["script-pass.md"])

    for group in batches(todo, BATCH):
        batch_doc: Dict[str, Any] = {"source": ctx.title, "episodes": []}
        batch_path = work / "script-batch.json"
        items: List[Item] = []
        for ep in group:
            seed_ep = seed_eps[ep]
            oe = outline_eps.get(ep, {})
            scene_ids = oe.get("sceneIds", [])
            char_ids = oe.get("characterIds", [])
            chars = [
                {**compact({k: outline_chars[i].get(k) for k in ("id", "name", "role", "arc")}, 200),
                 **cast_info.get(outline_chars[i].get("name", ""), {})}
                for i in char_ids if i in outline_chars
            ]
            prev_main = next((e for e in main["episodes"] if e["ep"] == ep - 1), None)
            prev = batch_doc_get(batch_doc, ep - 1) or prev_main
            prompt = (
                f"## 任务\n写第 {ep} 集的完整剧本（把 scenes 从空写满）。\n\n"
                f"## seed 骨架（目标秒数、钩子、悬念、认领的爽点已定，不要改）\n{json.dumps(seed_ep, ensure_ascii=False, indent=1)}\n\n"
                f"## 大纲里这一集\n{json.dumps(compact(oe, 600), ensure_ascii=False, indent=1)}\n\n"
                f"## 场景卡（光照状态 lighting 只能用这里登记过的 state 名）\n"
                f"{json.dumps([_scene_card(art_scenes[i]) for i in scene_ids if i in art_scenes], ensure_ascii=False, indent=1) or '（没有美术设定，lighting 可省略）'}\n\n"
                f"## 出场角色（台词要谁的话像谁）\n{json.dumps(chars, ensure_ascii=False, indent=1)}\n\n"
                f"## 上一集的结尾（这一集的开场要接得上）\n{_ending(prev, outline_eps.get(ep - 1))}\n\n"
                "## 输出\n只输出这一集的 JSON 对象（ep、targetSeconds、hook、cliff、beatsClaimed、hookBeat、scenes），"
                "不要 seedNote。动作节拍只写常见动作；写完先自己按 4.5 字/秒、2.5 秒/拍估一下时长。"
            )
            items.append(Item(str(ep), system, prompt, tokens=ep_tokens(ep),
                              cache=work / f"ep-{ep:03d}.json"))

        def apply(key: str, data: Any, _seed=seed_eps, _doc=batch_doc, _path=batch_path) -> None:
            if isinstance(data, dict) and isinstance(data.get("episode"), dict):
                data = data["episode"]
            if isinstance(data, list) and len(data) == 1:
                data = data[0]
            if not isinstance(data, dict) or "scenes" not in data:
                raise ValueError("一集应当是带 scenes 的 JSON 对象")
            ep = int(key)
            episode = {k: v for k, v in data.items() if k != "seedNote"}
            episode["ep"] = ep
            for fix in repair_speakers(episode, {c.get("id") for c in outline.get("characters", []) if isinstance(c, dict)}):
                ctx.log(f"[script] 第 {ep} 集自动修正：{fix}")
            if repair_hook_beat(episode):
                ctx.log(f"[script] 第 {ep} 集 hookBeat 按 0 起编号，已自动 +1 → {episode['hookBeat']}")
            for k in ("targetSeconds", "hook", "cliff"):
                episode.setdefault(k, _seed[ep].get(k))
            episode.setdefault("beatsClaimed", _seed[ep].get("beatsClaimed", []))
            _doc["episodes"] = sorted(
                [e for e in _doc["episodes"] if e["ep"] != ep] + [episode], key=lambda e: e["ep"])
            write_json(_path, _doc)

        run_items(
            ctx, items, apply=apply, label=f"script-第{group[0]}–{group[-1]}集",
            validate=lambda _p=batch_path: ctx.run(sk, ["validate", _p, *_upstream(ctx)]),
        )
        merged = {e["ep"]: e for e in main["episodes"]}
        merged.update({e["ep"]: e for e in batch_doc["episodes"]})
        main["episodes"] = [merged[k] for k in sorted(merged)]
        write_json(path, main)

    result = check(ctx)
    if not result.ok:
        ctx.log("⚠ 全剧校验未通过（单批通过但合并后有问题）：\n" + result.text)
    out_dir = ctx.dir("script")
    render_args = [*_upstream(ctx), *(["--cast", cast_path] if cast_path.is_file() else [])]
    ctx.render_to(sk, ["render", path, "--md", *_upstream(ctx)], out_dir / f"{path.stem}.md")
    ctx.render_to(sk, ["render", path, "--html", *render_args], out_dir / "script-report.html")
    return path


def batch_doc_get(batch_doc: Dict[str, Any], ep: int) -> Optional[Dict[str, Any]]:
    return next((e for e in batch_doc["episodes"] if e["ep"] == ep), None)
