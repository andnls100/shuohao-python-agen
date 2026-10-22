"""阶段 6：novel-storyboard —— 剧本 → 分镜（storyboard.json：段 → 分镜 → 分镜图提示词 + H3 视频提示词）。

流程（对应 skills/novel-storyboard/SKILL.md）：
  seed      每批 ≤3 集，把剧本节拍展开成工作底稿 seedScenes（每拍几秒是算出来的）
  切镜      批内每集一次模型调用：先按剧情单元分段（≤20 秒），段内切 4–8 秒的分镜，写 frame / shot / h3Prompt
  校验      先自动修复轻微时长超限并重算 H3 时间；其余可定位错误只修单个分镜
            → 不因一个分镜的问题重新生成整集；无法定位或修复失败则保存批次并停止
  输出      md / html 报告 + export 导出投产包（每段一个文件夹）
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..skill_loader import build_system
from ..visual_style import visual_style_instruction
from ..llm import Conversation
from ..validator import Result
from .base import (
    Context, Item, batches, compact, ep_tokens, read_json, run_items, write_json,
)
from .storyboard_repair import (
    balance_episode_durations, cut_violation_refs, extract_h3_cut_text,
    repair_deterministic_storyboard, repair_dialogue_fit, repair_minor_cut_durations,
    replace_h3_cut_text, segment_h3_violation_refs, sync_segment_timing,
)

SKILL = "novel-storyboard"
BATCH = 3
PASSES = ["storyboard-pass.md", "shot-writing.md", "h3-prompt.md", "frame.md"]


def _params_override(h3_gates: bool = True) -> Dict[str, Any]:
    """环境变量 AGENT_STORYBOARD_PARAMS（JSON）可覆盖技能包默认分镜参数，如 {"minCutSeconds":2,"maxCutSeconds":10}。"""
    raw = os.environ.get("AGENT_STORYBOARD_PARAMS", "").strip()
    data: Dict[str, Any] = {}
    if raw:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"AGENT_STORYBOARD_PARAMS 不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("AGENT_STORYBOARD_PARAMS 必须是 JSON 对象")
    if not h3_gates:
        data = {**data, "h3Gates": False}
    return data


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


def _repair_existing_h3(ctx: Context, doc: Dict[str, Any], path: Path,
                        prompt_lang: str) -> Result:
    """Repair H3 prompts in the merged artifact too, including episodes reused from cache."""
    sk = ctx.skill(SKILL)
    sb_dir = ctx.dir("storyboard")

    # These fixes are deterministic and safe to apply to every existing segment.
    fixes = repair_deterministic_storyboard(doc, prompt_lang)
    fixes += repair_minor_cut_durations(doc, prompt_lang)
    fixes += repair_dialogue_fit(doc, read_json(ctx.artifact("script")), prompt_lang)
    # Do not rebalance episode totals here: existing artifacts may still carry an old
    # explicit tolerance override, and this pass is for fixing prompts, not trimming cuts.
    if fixes:
        for fix in fixes:
            ctx.log(f"[storyboard] 已有分镜自动修正：{fix}")
        write_json(path, doc)

    # Validate/repair up to two rounds. Unlike generation-time repair, this also runs
    # when all episodes were already present and the generation loop was skipped.
    for repair_round in range(2):
        result = ctx.run(sk, ["validate", path, *_refs(ctx)], cwd=sb_dir)
        if result.ok:
            return result

        targets = segment_h3_violation_refs(result.violations)
        if not targets:
            return result

        repaired = 0
        for segment_id, errors in targets.items():
            segment = next((s for ep in doc.get("episodes", [])
                            for s in ep.get("segments", [])
                            if s.get("id") == segment_id), None)
            if not segment:
                continue
            old_prompt = str(segment.get("h3Prompt") or "")
            repair_system = (
                "你是 H3 视频提示词的局部修复器。只修复一个 segment 的 h3Prompt，"
                "不许改 cuts、秒数、镜头顺序、beats、台词原文、Seedance shot/frame、音效或配乐字段。"
                "严格按 promptLang 输出完整 H3 提示词：zh 用简体中文对齐行、[镜头 k]、"
                "整体视听描述：、整体音景：、非叙事配乐：；en 用英文官方骨架、[Shot k]、"
                "integrated_multimodal_description:、overall_soundscape:、non_diegetic_music:。"
                "每个镜头段落必须存在且按顺序；每切运镜词必须与对应 cut.camera 匹配；"
                "对齐行与每个切点时间必须由 cuts 秒数累计得出。人名不写进正文，"
                "只允许在 <d> 台词块中保留剧本原文。只输出 JSON：{\"h3Prompt\":\"...\"}。"
            )
            repair_user = (
                f"segment={segment_id}; promptLang={prompt_lang}\n"
                f"校验错误：{json.dumps(errors, ensure_ascii=False)}\n"
                f"固定 cuts（只读）：{json.dumps(segment.get('cuts', []), ensure_ascii=False)}\n"
                f"原始 h3Prompt：{json.dumps(old_prompt, ensure_ascii=False)}\n"
                "请完整重写 h3Prompt，严格保留每个 <d>...</d> 块的内容和顺序；"
                "除协议骨架、语言、运镜词和切点时间外，不改剧情语义。"
            )
            try:
                patch = Conversation(repair_system, ctx.settings).ask_json(repair_user)
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"[storyboard] {segment_id} H3 修复失败：{exc}")
                continue
            new_prompt = patch.get("h3Prompt") if isinstance(patch, dict) else None
            if isinstance(new_prompt, str) and new_prompt.strip() and new_prompt != old_prompt:
                segment["h3Prompt"] = new_prompt
                sync_segment_timing(segment, prompt_lang)
                repaired += 1
                ctx.log(f"[storyboard] 已修复已有分镜 {segment_id} 的 H3 语言/结构")

        if not repaired:
            return result
        write_json(path, doc)

    return ctx.run(sk, ["validate", path, *_refs(ctx)], cwd=sb_dir)


def _blocking_lines(ctx: Context) -> List[str]:
    staging = ctx.artifact("staging")
    if not staging.is_file():
        return []
    result = ctx.run(ctx.skill("novel-staging"), ["blocking", staging])
    return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()] if result.ok else []



def _episode_cache_candidates(work: Path, ep: int) -> List[Path]:
    """Support both the current ep-001.json name and legacy ep001.json caches."""
    return [work / f"ep{ep:03d}.json", work / f"ep-{ep:03d}.json"]


def _normalize_episode_segment_ids(episode: Dict[str, Any], ep: int) -> bool:
    """Force segment IDs to match the authoritative episode number and list order."""
    changed = False
    segments = episode.get("segments", [])
    if not isinstance(segments, list):
        return False
    for index, segment in enumerate(segments, start=1):
        if not isinstance(segment, dict):
            continue
        expected = f"E{ep:02d}-{index:02d}"
        if segment.get("id") != expected:
            segment["id"] = expected
            changed = True
    episode["ep"] = ep
    return changed


def _read_cached_episode(cache: Path, ep: int) -> Optional[Dict[str, Any]]:
    """Return a reusable episode cache, or None if it is incomplete/invalid."""
    try:
        data = read_json(cache)
    except (OSError, ValueError, TypeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("episode"), dict):
        data = data["episode"]
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list) or not data["segments"]:
        return None
    episode = {k: v for k, v in data.items() if k != "seedScenes"}
    _normalize_episode_segment_ids(episode, ep)
    return episode

def run(
    ctx: Context,
    eps: Optional[List[int]] = None,
    aspect: str = "16:9",
    prompt_lang: str = "en",
    visual_style: str = "manju",
    fight_enhancement: str = "auto",
    h3_gates: bool = True,
) -> Path:
    if fight_enhancement not in {"off", "auto", "enhanced"}:
        raise ValueError("fight_enhancement 只能是 off、auto 或 enhanced")
    style_hint = visual_style_instruction(visual_style)
    sk = ctx.skill(SKILL)
    work = ctx.work("storyboard")
    sb_dir = ctx.dir("storyboard")
    script_path = ctx.artifact("script")
    if not script_path.is_file():
        raise FileNotFoundError(f"需要先有剧本：{script_path}（先运行 script 阶段）")
    script = read_json(script_path)
    wanted = eps or [e["ep"] for e in script["episodes"]]
    _eff = _params_override(h3_gates)
    ctx.log(f"[storyboard] 分镜参数覆盖：{json.dumps(_eff, ensure_ascii=False) if _eff else '无（使用技能包默认）'}；h3 检查={'开' if h3_gates else '关（seedance）'}")
    ctx.log(f"[storyboard] 打戏增强模式：{fight_enhancement}" + ("（已有分镜需 --force 才会按新模式重做）" if artifact(ctx).is_file() and not ctx.force else ""))
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
    if _params_override(h3_gates):
        main["params"] = {**(main.get("params") or {}), **_params_override(h3_gates)}
    # Existing merged output can also contain stale IDs from older runs. Repair them
    # before deciding which episodes are complete, and persist the deterministic fix.
    normalized_existing = False
    for episode in main.get("episodes", []):
        if isinstance(episode, dict) and isinstance(episode.get("ep"), int):
            normalized_existing = _normalize_episode_segment_ids(episode, episode["ep"]) or normalized_existing
    if normalized_existing:
        write_json(path, main)
        ctx.log("[storyboard] 已校正已有分镜的集号/分镜 ID")
    # Recover completed episode caches before deciding what still needs generation.
    # This prevents restarting the seed/model calls when only work/epNNN.json survived.
    if not ctx.force:
        merged_episodes = {
            e["ep"]: e for e in main.get("episodes", [])
            if isinstance(e, dict) and e.get("segments") and "ep" in e
        }
        recovered = False
        for ep in wanted:
            if ep in merged_episodes:
                continue
            for cache in _episode_cache_candidates(work, ep):
                if not cache.is_file():
                    continue
                episode = _read_cached_episode(cache, ep)
                if episode is None:
                    ctx.log(f"[storyboard] 缓存 {cache.name} 不完整或无效，检查其他缓存")
                    try:
                        cache.unlink()
                    except OSError:
                        pass
                    continue
                merged_episodes[ep] = episode
                recovered = True
                ctx.log(f"[storyboard] 恢复缓存 {cache.name}，跳过第 {ep} 集生成")
                break
        if recovered:
            main["episodes"] = [merged_episodes[k] for k in sorted(merged_episodes)]
            main["aspectRatio"] = aspect
            main["promptLang"] = prompt_lang
            write_json(path, main)

    done = {e["ep"] for e in main.get("episodes", []) if e.get("segments")}
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
        if _params_override(h3_gates):
            batch_doc["params"] = _params_override(h3_gates)
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
            fight_instruction = ""
            if fight_enhancement != "off":
                fight_instruction = (
                    "\n\n## 打戏增强规则\n"
                    "先判断各场是否存在真实的肢体或兵器交锋；不要仅凭“冲突、争吵”等词判断为打戏。"
                    "打戏按动作因果规划：发起动作 → 对手可见的防守/闪避/反制 → 接触或落空结果 → 双方位置与重心变化 → 下一动作承接。"
                    "不要只写“双方激烈打斗”；每个动作必须说明谁对谁做什么、方向/距离如何、对手如何回应、结果是什么，并确保动作能在对应镜头秒数内完成。"
                    "保持剧本已确定的胜负、伤势、武器、人物能力和剧情结果，不得擅自新增致命攻击、伤害或剧情转折。"
                    "对被增强的打戏 segment 增加可选字段 fightPlan，结构为："
                    "{\"importance\":\"high\",\"actions\":["
                    "{\"actionId\":\"A001\",\"attacker\":\"角色ID或剧本身份\",\"defender\":\"角色ID或剧本身份\","
                    "\"attack\":\"具体动作\",\"response\":\"防守/闪避/反击\",\"result\":\"动作结果\","
                    "\"positionsBefore\":\"动作前双方相对位置\",\"positionsAfter\":\"动作后双方相对位置\","
                    "\"seconds\":2.0,\"cutIndexes\":[1]}]}。"
                    "importance 只允许 high 或 medium；cutIndexes 从 1 开始，对应该 segment.cuts 的序号；动作计划覆盖关键攻防节拍，且时间安排与对应镜头相容。"
                    "必须把动作计划落实到 cut.frame、cut.shot、cut.sfx、segment.blocking 和 h3Prompt，不能只写 fightPlan 而不改变镜头内容。"
                    "每镜明确动作衔接、双方空间位置、攻防反应和动势承接；仍遵守现有时长、台词、画幅、角色名和 H3 协议校验。\n"
                    + (
                        "模式=auto：判断打戏对剧情的作用。只有 high 重要程度（决定胜负、揭示主角实力、改变剧情走向或构成高潮）的打戏才写 fightPlan 并强化；普通推搡、短暂冲突、重点在对白/情绪的挥拳不强化，沿用普通分镜。"
                        if fight_enhancement == "auto" else
                        "模式=enhanced：所有真实打戏都写 fightPlan 并强化；非打戏场景完全沿用普通分镜，不得强行增加打斗。"
                    )
                    + "\n"
                )
            prompt = (
                f"## 任务\n给第 {ep} 集切分镜：先按剧情单元分段（每段 9–20 秒、不跨场），段内切 4–8 秒的分镜。\n\n"
                + f"## 画幅与语言\naspectRatio = {aspect}；promptLang = {prompt_lang}（h3Prompt 正文、对齐行、镜头标记、声景和配乐字段都必须使用该语言；台词在 <d> 里保留原文）。\n\n"
                + ("## 中文模式硬约束\n除协议固定 token 和台词外，frame、shot、h3Prompt 全部使用简体中文。h3Prompt 使用“参考图与目标视频的对齐——”开头，字段为“整体视听描述：”“整体音景：”“非叙事配乐：”，镜头标记使用连续的 [镜头 1]、[镜头 2]。禁止使用英文版 How the reference pictures align、integrated_multimodal_description、overall_soundscape、non_diegetic_music 或 [Shot k]。\n\n" if prompt_lang == "zh" else "")
                + ("## 英文模式硬约束\nh3Prompt 正文、对齐行、镜头标记、声景和配乐字段全部使用英文及官方英文骨架，镜头标记使用连续的 [Shot 1]、[Shot 2]。\n\n" if prompt_lang == "en" else "")
                + f"## 视觉风格（frame、shot 和 h3Prompt 必须统一遵守）\n{style_hint}\n保持同一风格贯穿所有镜头，不要在漫剧插画与真人摄影之间混用。\n\n"
                + "## 多人同框说明\n单个分镜 characters 超过 3 人时，该分镜必须写非空 note：说明前景主体是谁、其余人物作为背景群像如何站位，以及哪些人物不单独特写。不超过 3 人可不写。\n\n"
                + "## 连续性状态表（必须落实到 JSON）\n每个 segment 增加 continuityMode：continuous 或 hard_cut；每个 cut 增加 continuity：{mode, stateBefore, stateAfter, referenceFrame}。mode 只允许 inherit 或 cut。stateBefore/stateAfter 用简短中文记录所有可见角色的位置（画面左右/前后、朝向、姿势）、服装、手持道具、伤势，以及关键物件位置；状态要具体可画，不能写“保持一致”。\n同场连续动作默认 inherit：下一切 stateBefore 必须承接上一切 stateAfter，动作按“起始姿态→动作中→结果姿态”推进；referenceFrame 填上一切的帧标识（如 E01-01/f1.png），供下游把前一帧作为构图/姿态参考。只有明确的时间跳跃、换地点或剧情要求重置构图时才用 cut，并写明原因；不要为了继承而强行复制上一镜构图。跨 segment 若同场且动作连续，也要在下一段首切的 stateBefore 承接前段末切 stateAfter，并把 referenceFrame 指向前段末帧。\nframe 只负责当前瞬间的构图、人物位置/姿态和状态；shot 负责几秒内动作过程、镜头运动与动作衔接。角色脸、服装、场景材质以设定图为准。必须在输出 JSON 中提供上述字段，不得只写在说明文字里。\n\n"
                + f"## 工作底稿 seedScenes（节拍编号、台词、每拍秒数已算好，不要重估；镜头必须认领这些节拍）\n{json.dumps(seed_ep.get('seedScenes', []), ensure_ascii=False, indent=1)}\n\n"
                + f"## 场景卡\n{json.dumps(scene_cards, ensure_ascii=False, indent=1)}\n\n"
                + f"## 角色卡（frame 里直呼角色名；视频正文用通用身份，不写名字）\n{json.dumps(char_cards, ensure_ascii=False, indent=1)}\n\n"
                + f"## 叙事道具\n{json.dumps(prop_cards, ensure_ascii=False, indent=1)}\n\n"
                + (f"## 每场第一段的开场走位句（blocking 请照抄对应那一场的这一句）\n" + "\n".join(first_blocking) + "\n\n"
                   if first_blocking else "")
                + "## 输出\n只输出这一集的 JSON 对象：{\"ep\": N, \"segments\": [...]}，不要 seedScenes。"
                  "改秒数必须同步改 h3Prompt（对齐指令和 [Shot k]/[镜头 k] 切点时刻由分镜秒数推导，逐字对账；标记语言必须与 promptLang 一致）。"
            )
            prompt += fight_instruction
            existing_cache = next((p for p in _episode_cache_candidates(work, ep) if p.is_file()), None)
            cache_path = existing_cache or (work / f"ep{ep:03d}.json")
            items.append(Item(str(ep), system, prompt, tokens=ep_tokens(ep), cache=cache_path))

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

        def validate_batch(_p=batch_path, _doc=batch_doc) -> Result:
            # Deterministic repairs: missing size terms, stale H3 timing, tiny cut overruns,
            # and episode overruns that can be trimmed from action-only cuts without touching dialogue.
            basic_fixes = repair_deterministic_storyboard(_doc, prompt_lang)
            timing_fixes = repair_minor_cut_durations(_doc, prompt_lang)
            fit_fixes = repair_dialogue_fit(_doc, script, prompt_lang)
            duration_fixes = balance_episode_durations(_doc, script)
            for fix in [*basic_fixes, *timing_fixes, *fit_fixes, *duration_fixes]:
                ctx.log(f"[storyboard] 自动修正：{fix}")
            if basic_fixes or timing_fixes or fit_fixes or duration_fixes:
                write_json(_p, _doc)

            result = ctx.run(sk, ["validate", _p, *_refs(ctx)], cwd=sb_dir)
            if result.ok:
                return result

            # Repair H3 at segment scope when its language or protocol skeleton is wrong.
            # Cuts, timings, dialogue, and Seedance fields are immutable in this pass.
            segment_targets = segment_h3_violation_refs(result.violations)
            h3_repaired = 0
            # Repair every affected segment in this batch. A fixed cap left later
            # segments untouched, especially when several episodes had H3 failures.
            for segment_id, errors in segment_targets.items():
                segment = next((s for e in _doc.get("episodes", [])
                                for s in e.get("segments", []) if s.get("id") == segment_id), None)
                if not segment:
                    continue
                old_prompt = str(segment.get("h3Prompt") or "")
                repair_system = (
                    "你是 H3 视频提示词的局部修复器。只修复一个 segment 的 h3Prompt，"
                    "不许改 cuts、秒数、镜头顺序、beats、台词原文、Seedance shot/frame、音效或配乐字段。"
                    "严格按 promptLang 输出完整 H3 提示词：zh 用简体中文对齐行、[镜头 k]、"
                    "整体视听描述：、整体音景：、非叙事配乐：；en 用英文官方骨架、[Shot k]、"
                    "integrated_multimodal_description:、overall_soundscape:、non_diegetic_music:。"
                    "每个镜头段落必须存在且按顺序；运镜词必须与对应 cut.camera 匹配；"
                    "对齐行与每个切点时间必须由 cuts 秒数累计得出。人名不写进正文，"
                    "只允许在 <d> 台词块中保留剧本原文。只输出 JSON：{\"h3Prompt\":\"...\"}。"
                )
                repair_user = (
                    f"segment={segment_id}; promptLang={prompt_lang}\n"
                    f"校验错误：{json.dumps(errors, ensure_ascii=False)}\n"
                    f"固定 cuts（只读）：{json.dumps(segment.get('cuts', []), ensure_ascii=False)}\n"
                    f"原始 h3Prompt：{json.dumps(old_prompt, ensure_ascii=False)}\n"
                    "请完整重写 h3Prompt，严格保留每个 <d>...</d> 块的内容和顺序；"
                    "除协议骨架/语言/运镜词/切点时间外，不改剧情语义。"
                )
                try:
                    patch = Conversation(repair_system, ctx.settings).ask_json(repair_user)
                except Exception as exc:  # noqa: BLE001
                    ctx.log(f"[storyboard] {segment_id} H3 局部修复失败：{exc}")
                    continue
                new_prompt = patch.get("h3Prompt") if isinstance(patch, dict) else None
                if isinstance(new_prompt, str) and new_prompt.strip() and new_prompt != old_prompt:
                    segment["h3Prompt"] = new_prompt
                    sync_segment_timing(segment, prompt_lang)
                    h3_repaired += 1
                    ctx.log(f"[storyboard] 已修复 {segment_id} 的 H3 语言/结构")
            if h3_repaired:
                write_json(_p, _doc)
                result = ctx.run(sk, ["validate", _p, *_refs(ctx)], cwd=sb_dir)
                if result.ok:
                    return result

            # Only route errors that name a precise segment/cut. Never send a whole episode
            # back to the model for a single bad cut. At most one local repair call per cut.
            # If the H3 protocol vocabulary contradicts promptLang, repair the complete
            # prompt for that segment. Language errors are sometimes reported without a cut id.
            repaired_language = False
            for episode in _doc.get("episodes", []):
                for segment in episode.get("segments", []):
                    h3 = segment.get("h3Prompt")
                    if not isinstance(h3, str):
                        continue
                    mismatch = (
                        prompt_lang == "zh" and any(t in h3 for t in (
                            "How the reference pictures align", "integrated_multimodal_description:",
                            "overall_soundscape:", "non_diegetic_music:", "[Shot "
                        ))
                    ) or (
                        prompt_lang == "en" and any(t in h3 for t in (
                            "参考图与目标视频的对齐", "整体视听描述：", "整体音景：", "非叙事配乐：", "[镜头 "
                        ))
                    )
                    if not mismatch:
                        continue
                    repair_system = (
                        "只修复给定的 H3 提示词语言与格式，返回 JSON 对象，字段 h3Prompt 为完整修复后的字符串。"
                        "不得改变剧情、镜头数量和顺序、秒数、画面锚点、声音意图。保留 <d> 内台词原文。"
                        "promptLang=zh 时使用中文对齐句、整体视听描述、整体音景、非叙事配乐和 [镜头 k]；"
                        "promptLang=en 时使用官方英文字段和 [Shot k]。"
                    )
                    repair_user = json.dumps({
                        "promptLang": prompt_lang, "segmentId": segment.get("id"),
                        "cuts": segment.get("cuts", []), "h3Prompt": h3
                    }, ensure_ascii=False)
                    try:
                        patch = Conversation(repair_system, ctx.settings).ask_json(repair_user)
                    except Exception as exc:  # noqa: BLE001
                        ctx.log(f"[storyboard] {segment.get('id')} H3 语言修复失败：{exc}")
                        continue
                    if isinstance(patch, dict) and isinstance(patch.get("h3Prompt"), str) and patch["h3Prompt"].strip():
                        segment["h3Prompt"] = patch["h3Prompt"].strip()
                        sync_segment_timing(segment, prompt_lang)
                        repaired_language = True
                        ctx.log(f"[storyboard] 已修复 {segment.get('id')} 的 H3 语言与格式")
                        write_json(_p, _doc)

            if repaired_language:
                result = ctx.run(sk, ["validate", _p, *_refs(ctx)], cwd=sb_dir)
                if result.ok:
                    return result

            # Repair prompts whose visible H3 protocol tokens contradict promptLang.
            # These errors may be reported at segment level, without a specific cut reference.
            language_repairs = 0
            for episode in _doc.get("episodes", []):
                for segment in episode.get("segments", []):
                    h3 = segment.get("h3Prompt")
                    if not isinstance(h3, str):
                        continue
                    mismatch = (
                        prompt_lang == "zh" and any(t in h3 for t in (
                            "How the reference pictures align", "integrated_multimodal_description:",
                            "overall_soundscape:", "non_diegetic_music:", "[Shot "
                        ))
                    ) or (
                        prompt_lang == "en" and any(t in h3 for t in (
                            "参考图与目标视频的对齐", "整体视听描述：", "整体音景：", "非叙事配乐：", "[镜头 "
                        ))
                    )
                    if not mismatch:
                        continue
                    repair_system = (
                        "只修复给定 H3 提示词的语言和格式，返回 JSON 对象，字段 h3Prompt 为完整字符串。"
                        "保留剧情、镜头数量与顺序、秒数、画面锚点、声音意图；保留 <d> 内台词原文。"
                        "promptLang=zh 使用中文对齐句、整体视听描述、整体音景、非叙事配乐及 [镜头 k]；"
                        "promptLang=en 使用官方英文字段及 [Shot k]。"
                    )
                    repair_user = json.dumps({
                        "promptLang": prompt_lang,
                        "segmentId": segment.get("id"),
                        "cuts": segment.get("cuts", []),
                        "h3Prompt": h3,
                    }, ensure_ascii=False)
                    try:
                        patch = Conversation(repair_system, ctx.settings).ask_json(repair_user)
                    except Exception as exc:  # noqa: BLE001
                        ctx.log(f"[storyboard] {segment.get('id')} H3 语言修复失败：{exc}")
                        continue
                    if isinstance(patch, dict) and isinstance(patch.get("h3Prompt"), str) and patch["h3Prompt"].strip():
                        segment["h3Prompt"] = patch["h3Prompt"].strip()
                        sync_segment_timing(segment, prompt_lang)
                        language_repairs += 1
                        ctx.log(f"[storyboard] 已修复 {segment.get('id')} 的 H3 语言与格式")
                        write_json(_p, _doc)

            if language_repairs:
                result = ctx.run(sk, ["validate", _p, *_refs(ctx)], cwd=sb_dir)
                if result.ok:
                    return result

            targets = cut_violation_refs(result.violations)
            if not targets:
                ctx.log("[storyboard] 剩余错误无法定位到具体分镜；停止自动回炉，保留批次文件供检查")
                return result

            repaired = 0
            # Do not silently leave later cuts untouched when a batch has many violations.
            # Every target is a cut-scoped call; no episode-wide regeneration is performed.
            for (segment_id, cut_index), errors in targets.items():
                episode = next((e for e in _doc.get("episodes", [])
                                if any(s.get("id") == segment_id for s in e.get("segments", []))), None)
                segment = next((s for s in (episode or {}).get("segments", [])
                                if s.get("id") == segment_id), None)
                cuts = (segment or {}).get("cuts", [])
                if not segment or not isinstance(cuts, list) or not (1 <= cut_index <= len(cuts)):
                    continue
                cut = cuts[cut_index - 1]
                old_cut = json.loads(json.dumps(cut, ensure_ascii=False))
                old_shot_text = extract_h3_cut_text(segment, cut_index, prompt_lang)
                repair_system = (
                    "你是分镜 JSON 的局部修复器。只能修复提供的一个 cut 和它对应的一段 H3 镜头正文，"
                    "绝不能重写整集、其他分镜或剧情。保留 beats、characters、props 原样；"
                    "不要新增剧情或台词。只输出 JSON："
                    "{\"cut\":{...需要修改的字段...},\"h3ShotText\":\"该镜完整的 [Shot k]/[镜头 k] 正文\"}。"
                    "若不需要修改 h3ShotText，原样返回它。"
                )
                repair_user = (
                    f"分镜段：{segment_id}；第 {cut_index} 切。画幅：{_doc.get('aspectRatio', aspect)}；"
                    f"H3 提示词语言：{prompt_lang}。\n"
                    f"本切校验错误：{json.dumps(errors, ensure_ascii=False)}\n"
                    f"段级必要上下文：{json.dumps({k: segment.get(k) for k in ('sceneIndex', 'blocking', 'soundscape', 'music')}, ensure_ascii=False)}\n"
                    f"当前 cut：{json.dumps(old_cut, ensure_ascii=False)}\n"
                    f"当前 H3 镜头正文：{json.dumps(old_shot_text, ensure_ascii=False)}\n"
                    "仅返回修复后的当前 cut 字段和当前 H3 镜头正文。不要返回整个 segment 或 episode。"
                )
                try:
                    patch = Conversation(repair_system, ctx.settings).ask_json(repair_user)
                except Exception as exc:  # noqa: BLE001
                    ctx.log(f"[storyboard] {segment_id}#{cut_index} 局部修复调用失败：{exc}")
                    continue
                if not isinstance(patch, dict):
                    continue
                patch_cut = patch.get("cut")
                if isinstance(patch_cut, dict):
                    # Structural/script ownership stays immutable; only cut-level presentation fields can change.
                    forbidden = {"beats", "characters", "props", "sceneIndex", "id"}
                    allowed = set(cut) | {
                        "seconds", "size", "camera", "stability", "lens", "cameraPosition",
                        "composition", "eyeline", "focus", "lighting", "frame", "shot", "sfx",
                        "note", "recipe",
                    }
                    for key, value in patch_cut.items():
                        if key in allowed and key not in forbidden:
                            cut[key] = value
                shot_text = patch.get("h3ShotText")
                if isinstance(shot_text, str) and shot_text.strip():
                    replace_h3_cut_text(segment, cut_index, shot_text, prompt_lang)
                sync_segment_timing(segment, prompt_lang)
                if cut != old_cut or extract_h3_cut_text(segment, cut_index, prompt_lang) != old_shot_text:
                    repaired += 1
                    ctx.log(f"[storyboard] 已局部修复 {segment_id}#{cut_index}")
                    write_json(_p, _doc)

            if repaired:
                result = ctx.run(sk, ["validate", _p, *_refs(ctx)], cwd=sb_dir)
            return result

        run_items(
            ctx, items, apply=apply, label=f"storyboard-第{group[0]}–{group[-1]}集",
            validate=validate_batch,
            rounds=0,  # no episode-wide retries; repairs above are deterministic or cut-scoped
        )
        merged = {e["ep"]: e for e in main["episodes"]}
        merged.update({e["ep"]: e for e in batch_doc["episodes"]})
        main["episodes"] = [merged[k] for k in sorted(merged)]
        write_json(path, main)

    result = _repair_existing_h3(ctx, main, path, prompt_lang)
    if not result.ok:
        ctx.log("⚠ 全剧校验未通过：\n" + result.text)

    refs = _refs(ctx)
    ctx.render_to(sk, ["render", path, "--md", *refs], sb_dir / f"{path.stem}.md", cwd=sb_dir)
    ctx.render_to(sk, ["render", path, "--html", *refs], sb_dir / "storyboard-report.html", cwd=sb_dir)
    export = ctx.run(sk, ["export", path, *refs, "--out", sb_dir], cwd=sb_dir)
    ctx.log("已导出投产包（每段一个文件夹 + manifest.json）" if export.ok
            else f"⚠ export 失败：{export.text[:200]}")
    return path
