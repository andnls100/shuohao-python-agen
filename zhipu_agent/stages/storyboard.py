"""阶段 6：novel-storyboard —— 剧本 → 分镜（storyboard.json：段 → 分镜 → 分镜图提示词 + H3 视频提示词）。

流程（对应 skills/novel-storyboard/SKILL.md）：
  seed      每批 ≤3 集，把剧本节拍展开成工作底稿 seedScenes（每拍几秒是算出来的）
  切镜      批内每集一次模型调用：先按剧情单元分段（≤15 秒），段内切 2–5 秒的分镜，写 frame / shot / h3Prompt
  校验      validate 18 道质量门（节拍全覆盖、H3 对齐指令逐字对账、台词逐字进 <d> 块……）
            → 按 "E01-02" 这样的段号 / "第 N 集" 把报错归给那一集回炉
  输出      md / html 报告 + export 导出投产包（每段一个文件夹）
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

SKILL = "novel-storyboard"
BATCH = 3
PASSES = ["storyboard-pass.md", "shot-writing.md", "h3-prompt.md", "frame.md"]


def artifact(ctx: Context) -> Path:
    return ctx.artifact("storyboard")


def _refs(ctx: Context, *, names: bool = True) -> List[Any]:
    args: List[Any] = ["--script", ctx.artifact("script")]
    for flag, stage in (("--outline", "outline"), ("--cast", "characters"), ("--art", "art")):
        p = ctx.artifact(stage)
        if p.is_file() and (names or flag != "--art"):
            args += [flag, p]
    return args


def check(ctx: Context) -> Result:
    return ctx.run(ctx.skill(SKILL), ["validate", artifact(ctx), *_refs(ctx)], cwd=ctx.dir("storyboard"))


def _blocking_lines(ctx: Context) -> List[str]:
    staging = ctx.artifact("staging")
    if not staging.is_file():
        return []
    result = ctx.run(ctx.skill("novel-staging"), ["blocking", staging])
    return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()] if result.ok else []


def run(
    ctx: Context,
    eps: Optional[List[int]] = None,
    aspect: str = "16:9",
    prompt_lang: str = "en",
) -> Path:
    sk = ctx.skill(SKILL)
    work = ctx.work("storyboard")
    sb_dir = ctx.dir("storyboard")
    script_path = ctx.artifact("script")
    if not script_path.is_file():
        raise FileNotFoundError(f"需要先有剧本：{script_path}（先运行 script 阶段）")
    script = read_json(script_path)
    wanted = eps or [e["ep"] for e in script["episodes"]]
    path = artifact(ctx)

    art_scenes: Dict[str, Any] = {}
    if ctx.artifact("art").is_file():
        art_scenes = {s["id"]: s for s in read_json(ctx.artifact("art")).get("scenes", [])}
    cast_by_id: Dict[str, Any] = {}
    cast_by_name: Dict[str, Any] = {}
    if ctx.artifact("characters").is_file():
        for c in read_json(ctx.artifact("characters")).get("characters", []):
            cast_by_name[c["name"]] = c
            if c.get("id"):
                cast_by_id[c["id"]] = c
    outline_chars: Dict[str, Any] = {}
    outline_props: Dict[str, Any] = {}
    if ctx.artifact("outline").is_file():
        o = read_json(ctx.artifact("outline"))
        outline_chars = {c["id"]: c for c in o.get("characters", [])}
        outline_props = {p["id"]: p for p in o.get("props", [])}
    blocking = _blocking_lines(ctx)

    main: Dict[str, Any] = {"source": ctx.title, "aspectRatio": aspect, "promptLang": prompt_lang, "episodes": []}
    if path.is_file() and not ctx.force:
        main = read_json(path)
        main.setdefault("promptLang", prompt_lang)
    done = {e["ep"] for e in main["episodes"] if e.get("segments")}
    todo = [e for e in wanted if e not in done]
    if not todo:
        ctx.log("所有集都已切好，跳过切镜")
    language_contract = (
        "HARD OUTPUT CONTRACT: promptLang=zh requires Simplified Chinese for frame, shot, h3Prompt, alignment, shot markers, soundscape, and music fields. Preserve dialogue inside <d>, lyrics, and visible in-frame text. Use [镜头 k], 整体视听描述：, 整体音景：, 非叙事配乐：, and the Chinese alignment format from h3-prompt.md. Do not use English field names or [Shot k]."
        if prompt_lang == "zh" else
        "HARD OUTPUT CONTRACT: promptLang=en requires English for h3Prompt, alignment, shot markers, soundscape, and music fields. Use [Shot k] and official English field names. Preserve dialogue inside <d>, lyrics, and visible in-frame text in their source language."
    )
    system = build_system(sk, PASSES, extra=language_contract)

    for group in batches(todo, BATCH):
        seed = ctx.node_json(sk, ["seed", script_path, "--eps", f"{group[0]}-{group[-1]}", "--aspect", aspect])
        seed_eps = {e["ep"]: e for e in seed["episodes"]}
        batch_doc: Dict[str, Any] = {"source": ctx.title, "aspectRatio": aspect,
                                     "promptLang": prompt_lang, "episodes": []}
        batch_path = work / "storyboard-batch.json"
        items: List[Item] = []
        for ep in group:
            seed_ep = seed_eps[ep]
            scene_ids = {s.get("sceneId") for s in seed_ep.get("seedScenes", [])}
            char_ids = {c for s in seed_ep.get("seedScenes", []) for c in s.get("characters", [])}
            prop_ids = {p for s in seed_ep.get("seedScenes", []) for p in s.get("props", [])}
            scene_cards = [
                compact({k: art_scenes[i].get(k) for k in ("id", "name", "anchors", "lighting")}, 200)
                for i in sorted(scene_ids) if i in art_scenes
            ]
            char_cards = []
            for cid in sorted(char_ids):
                oc = outline_chars.get(cid, {})
                card = cast_by_id.get(cid) or cast_by_name.get(oc.get("name", ""), {})
                char_cards.append(compact({
                    "id": cid, "name": oc.get("name") or card.get("name"),
                    "appearance": (card.get("persona") or {}).get("appearance"),
                }, 220))
            prop_cards = [compact(outline_props[p], 160) for p in sorted(prop_ids) if p in outline_props]
            first_blocking = [ln for ln in blocking
                              if any(t in ln for t in (f"第 {ep} 集", f"第{ep}集", f"E{ep:02d}"))]
            prompt = (
                f"## 任务\n给第 {ep} 集切分镜：先按剧情单元分段（每段 9–15 秒、不跨场），段内切 2–5 秒的分镜。\n\n"
                + f"## 画幅与语言\naspectRatio = {aspect}；promptLang = {prompt_lang}（h3Prompt 正文、对齐行、镜头标记、声景和配乐字段都必须使用该语言；台词在 <d> 里保留原文）。\n\n"
                + ("## 中文模式硬约束\n除协议固定 token 和台词外，frame、shot、h3Prompt 全部使用简体中文。h3Prompt 使用“参考图与目标视频的对齐——”开头，字段为“整体视听描述：”“整体音景：”“非叙事配乐：”，镜头标记使用连续的 [镜头 1]、[镜头 2]。禁止使用英文版 How the reference pictures align、integrated_multimodal_description、overall_soundscape、non_diegetic_music 或 [Shot k]。\n\n" if prompt_lang == "zh" else "")
                + ("## 英文模式硬约束\nh3Prompt 正文、对齐行、镜头标记、声景和配乐字段全部使用英文及官方英文骨架，镜头标记使用连续的 [Shot 1]、[Shot 2]。\n\n" if prompt_lang == "en" else "")
                + f"## 工作底稿 seedScenes（节拍编号、台词、每拍秒数已算好，不要重估；镜头必须认领这些节拍）\n{json.dumps(seed_ep.get('seedScenes', []), ensure_ascii=False, indent=1)}\n\n"
                + f"## 场景卡\n{json.dumps(scene_cards, ensure_ascii=False, indent=1)}\n\n"
                + f"## 角色卡（frame 里直呼角色名；视频正文用通用身份，不写名字）\n{json.dumps(char_cards, ensure_ascii=False, indent=1)}\n\n"
                + f"## 叙事道具\n{json.dumps(prop_cards, ensure_ascii=False, indent=1)}\n\n"
                + (f"## 每场第一段的开场走位句（blocking 请照抄对应那一场的这一句）\n" + "\n".join(first_blocking) + "\n\n"
                   if first_blocking else "")
                + "## 输出\n只输出这一集的 JSON 对象：{\"ep\": N, \"segments\": [...]}，不要 seedScenes。"
                  "改秒数必须同步改 h3Prompt（对齐指令和 [Shot k]/[镜头 k] 切点时刻由分镜秒数推导，逐字对账；标记语言必须与 promptLang 一致）。"
            )
            items.append(Item(str(ep), system, prompt, tokens=ep_tokens(ep), cache=work / f"ep-{ep:03d}.json"))

        def apply(key: str, data: Any, _doc=batch_doc, _path=batch_path) -> None:
            if isinstance(data, dict) and isinstance(data.get("episode"), dict):
                data = data["episode"]
            if isinstance(data, list) and len(data) == 1:
                data = data[0]
            if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
                raise ValueError("一集应当是带 segments 数组的 JSON 对象")
            ep = int(key)
            episode = {k: v for k, v in data.items() if k != "seedScenes"}
            episode["ep"] = ep
            # The task key is authoritative; models sometimes copy segment IDs from examples.
            for segment_index, segment in enumerate(episode["segments"], start=1):
                if not isinstance(segment, dict):
                    raise ValueError(f"第 {segment_index} 段必须是 JSON 对象")
                segment["id"] = f"E{ep:02d}-{segment_index:02d}"
            _doc["episodes"] = sorted(
                [e for e in _doc["episodes"] if e["ep"] != ep] + [episode], key=lambda e: e["ep"])
            write_json(_path, _doc)

        run_items(
            ctx, items, apply=apply, label=f"storyboard-第{group[0]}–{group[-1]}集",
            validate=lambda _p=batch_path: ctx.run(sk, ["validate", _p, *_refs(ctx)], cwd=sb_dir),
        )
        merged = {e["ep"]: e for e in main["episodes"]}
        merged.update({e["ep"]: e for e in batch_doc["episodes"]})
        main["episodes"] = [merged[k] for k in sorted(merged)]
        write_json(path, main)

    result = check(ctx)
    if not result.ok:
        ctx.log("⚠ 全剧校验未通过：\n" + result.text)

    refs = _refs(ctx)
    ctx.render_to(sk, ["render", path, "--md", *refs], sb_dir / f"{path.stem}.md", cwd=sb_dir)
    ctx.render_to(sk, ["render", path, "--html", *refs], sb_dir / "storyboard-report.html", cwd=sb_dir)
    export = ctx.run(sk, ["export", path, *refs, "--out", sb_dir], cwd=sb_dir)
    ctx.log("已导出投产包（每段一个文件夹 + manifest.json）" if export.ok
            else f"⚠ export 失败：{export.text[:200]}")
    return path
