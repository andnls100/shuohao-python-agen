"""阶段 6：novel-storyboard —— 剧本 → 分镜（storyboard.json：段 → 分镜 → 分镜图提示词 + 最终 Ref2V 视频提示词）。

流程（对应 skills/novel-storyboard/SKILL.md）：
  seed      每批 ≤3 集，把剧本节拍展开成工作底稿 seedScenes（每拍几秒是算出来的）
  切镜      批内每集一次模型调用：直接按所选 Ref2V Skill 生成最终视频提示词，同时写 frame / shot
  校验      自动修复时长与分镜字段；不再重建旧式 H3/I2VA 对齐文本
            → 不因一个分镜的问题重新生成整集；无法定位或修复失败则保存批次并停止
  输出      md / html 报告 + export 导出投产包（每段一个文件夹）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..ref_tags import build_ref_order, resolve_placeholders
from ..skill_loader import build_system
from ..visual_style import visual_style_instruction
from ..llm import ask_text
from ..validator import Result
from .base import (
    Context, Item, batches, compact, ep_tokens, read_json, run_items, write_json,
)
from .storyboard_repair import (
    balance_episode_durations, repair_deterministic_storyboard,
    repair_dialogue_fit, repair_minor_cut_durations,
)

SKILL = "novel-storyboard"
BATCH = 3
PASSES = ["storyboard-pass.md", "shot-writing.md", "frame.md"]

# MiniMax H3 Ref2VA 的参考图写法。模型写提示词时还不知道视频阶段最终上传哪几张图、按什么顺序，
# 所以只写素材 ID；程序绑定 cut.assetRefs 之后算出 refOrder（本段参考图顺序），再把 ID 换成最终编号。
H3_REFERENCE_CONTRACT = (
    "\n\nREFERENCE TAG CONTRACT (MiniMax H3 Ref2VA, overrides any other statement about <Picture N>): "
    "the video model is given one reference image per character, per scene and per prop that appears in the segment "
    "(character/scene/prop design sheets) — it is NOT given the storyboard frames, and <Picture N> is never a shot number. "
    "You do not know the final image numbers, so refer to every reference image ONLY by the canonical card id inside the tag: "
    "<Picture C02> for a character card id, <Picture S01> for the id of the scene card that matches the segment's sceneIndex "
    "(seedScenes[].sceneId), <Picture P03> for a prop card id. Copy ids exactly from the cards. "
    "NEVER write a digit tag such as <Picture 1> or <Picture 2>; the program replaces ids with the final numbers after it binds assets. "
    "Define every character, the scene and every prop that appears in any cut of the segment exactly once in subject_definitions, "
    "each with its own tag (several characters never share one tag), and use the same tags in retention_analysis and detailed_description "
    "where that subject, the environment or the prop is used. Only use ids that are listed in some cut's characters/props of this segment "
    "or that are the segment's scene id; do not invent ids."
)


_PARAMS_KEY = "AGENT_STORYBOARD_PARAMS"


def _params_raw() -> tuple:
    """返回 (JSON 文本, 来源说明)。先看环境变量（非空才算），再直接读项目根目录 .env（不受"空环境变量占位"影响）。"""
    value = os.environ.get(_PARAMS_KEY, "").strip()
    if value:
        return value, "环境变量"
    from ..config import REPO_ROOT
    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        return "", f"未找到 {env_file}"
    try:
        lines = env_file.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        return "", f"无法读取 {env_file}：{exc}"
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == _PARAMS_KEY:
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'" and val[1:2] != "{":
                val = val[1:-1]
            if val:
                return val, f".env（{env_file}）"
    return "", f"{env_file} 里没有 {_PARAMS_KEY} 这一行"


def _params_override(h3_gates: bool = True) -> Dict[str, Any]:
    """环境变量 AGENT_STORYBOARD_PARAMS（JSON）可覆盖技能包默认分镜参数，如 {"minCutSeconds":2,"maxCutSeconds":10}。"""
    raw, _src = _params_raw()
    data: Dict[str, Any] = {}
    if raw:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"AGENT_STORYBOARD_PARAMS 不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("AGENT_STORYBOARD_PARAMS 必须是 JSON 对象")
    for key in ("h3Gates", "nameGate"):
        if isinstance(data.get(key), str):
            data[key] = data[key].strip().lower() not in ("false", "0", "no", "off", "关", "否")
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
    """Apply deterministic storyboard fixes and validate; never rewrite final Ref2V prompts."""
    sk = ctx.skill(SKILL)
    fixes = repair_deterministic_storyboard(doc, prompt_lang, read_json(ctx.artifact("script")))
    fixes += repair_minor_cut_durations(doc, prompt_lang)
    fixes += repair_dialogue_fit(doc, read_json(ctx.artifact("script")), prompt_lang)
    if fixes:
        for fix in fixes:
            ctx.log(f"[storyboard] 已有分镜自动修正：{fix}")
        write_json(path, doc)
    return ctx.run(sk, ["validate", path, *_refs(ctx)], cwd=ctx.dir("storyboard"))


def _repair_ref2v_prompts(ctx: Context, doc: Dict[str, Any], path: Path, result: Result,
                         video_model: str) -> bool:
    """Only repair prompts explicitly named by the Ref2V validation gate; never regenerate cuts/segments."""
    if result.ok or "所选 Ref2V 提示词结构、参考图编号、台词原文与顺序" not in result.text:
        return False
    segment_ids = set(re.findall(r"\bE\d{2}-\d{2}\b", result.text))
    if not segment_ids:
        return False
    skill_root = Path(__file__).resolve().parents[2] / "skills"
    skill_path = (skill_root / "h3-prompt-writing" / "SKILL.md"
                  if video_model == "comfyui_h3"
                  else skill_root / "wan-3-0-ref2v-prompt-writing" / "SKILL.md")
    skill_text = skill_path.read_text(encoding="utf-8")
    system = (
        "Repair only the final Ref2V video prompt for one existing segment. Follow the model skill below. "
        "Do not rewrite the storyboard, cuts, timing, action order, character/prop assignments, or story facts. "
        "Correct only the listed validation errors, preserve every supplied dialogue verbatim, and preserve the "
        "mapping between each reference-image number and its actual image. You MAY move reference tags within "
        "detailed_description to correct shot order; do not renumber images or change which image a number denotes. "
        "For an H3 error saying detailed_description shot reference order is wrong, make the FIRST occurrence of "
        "<Picture 1>, <Picture 2>, ... appear in strictly ascending numeric order inside detailed_description, "
        "with each tag attached to its corresponding shot. Check the entire detailed_description from beginning to end; "
        "do not rely on occurrences in subject_definitions, summary, or retention_analysis to satisfy this rule. "
        "Keep every shot's action and dialogue in its original order, retain all required section headings, and do not "
        "introduce, omit, duplicate, or renumber reference images. Before returning, self-check the order of the first "
        "occurrence of each <Picture N> within detailed_description. "
        "Return only the corrected final video prompt, without JSON, headings outside the required model format, "
        "or explanation.\n\n## Selected model skill\n" + skill_text
    )
    # 新参考图编号协议：这一段有固定的参考图表（refOrder），编号 N 就是表里的第 N 张图，和镜头序号无关。
    ref_system = (
        "Repair only the final Ref2V video prompt for one existing segment. Follow the model skill below. "
        "Do not rewrite the storyboard, cuts, timing, action order, character/prop assignments, or story facts. "
        "Correct only the listed validation errors and preserve every supplied dialogue verbatim. "
        "This segment has a FIXED reference-image table (referenceImages in the user message): <Picture N> is exactly "
        "image N of that table. It is never a shot number, so do not reorder tags by shot and do not renumber. "
        "Write only the final numbers from the table (never card ids, never numbers that are not in the table). "
        "Define EVERY image of the table once in subject_definitions with its own <Picture N> tag, use the same tags in "
        "retention_analysis and detailed_description where that subject, the environment or the prop is used, and never let "
        "two different subjects share one tag. Keep every shot's action and dialogue in its original order and retain all "
        "required section headings. "
        "Return only the corrected final video prompt, without JSON, headings outside the required model format, "
        "or explanation.\n\n## Selected model skill\n" + skill_text
    )
    repaired = False
    for episode in doc.get("episodes", []):
        for segment in episode.get("segments", []):
            sid = str(segment.get("id") or "")
            if sid not in segment_ids:
                continue
            order = segment.get("refOrder") if video_model == "comfyui_h3" else None
            errors = [part.strip() for line in result.text.splitlines() for part in line.split("；") if sid in part]
            payload = {
                "videoModel": video_model,
                "segmentId": sid,
                "validationErrors": errors,
                "currentFinalPrompt": segment.get("h3Prompt", ""),
                "cuts": segment.get("cuts", []),
                "soundscape": segment.get("soundscape", ""),
                "music": segment.get("music", ""),
            }
            if order:
                payload["referenceImages"] = [
                    {"number": e["no"], "kind": e["kind"], "cardId": e.get("tag"), "name": e.get("name")} for e in order
                ]
            user = json.dumps(payload, ensure_ascii=False, indent=2)
            fixed = ask_text(
                ref_system if order else system, user, ctx.settings,
                purpose=f"ref2v-prompt-repair:{sid}",
            ).strip()
            if not fixed:
                raise ValueError(f"{sid} Ref2V 局部修复返回空提示词")
            if order:  # 模型万一还是写了素材 ID，这里补一次解析
                fixed, _unresolved = resolve_placeholders(fixed, order)
            if video_model == "comfyui_h3" and len(fixed) > 7000:
                raise ValueError(f"{sid} H3 Ref2VA 修复后提示词超过 7000 字符")
            segment["h3Prompt"] = fixed
            segment.pop("videoPrompt", None)
            repaired = True
            ctx.log(f"[storyboard] {sid}：仅按校验错误修复 Ref2V 提示词；未重写分镜")
    if repaired:
        write_json(path, doc)
    return repaired


def _apply_ref_order(ctx: Context, segment: Dict[str, Any]) -> None:
    """H3：算出这一段的参考图顺序 refOrder，并把提示词里的素材 ID 占位符（<Picture C02>）换成最终编号。

    refOrder 是视频阶段上传图片的唯一依据，也是校验器检查 <Picture N> 的依据，
    所以提示词里的编号和上传的图片一定是同一套。对不上的占位符原样保留，由校验器报错并走局部修复。
    """
    order = build_ref_order(segment)
    segment["refOrder"] = order
    prompt = segment.get("h3Prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return
    resolved, unresolved = resolve_placeholders(prompt, order)
    segment["h3Prompt"] = resolved
    if unresolved:
        ctx.log(f"[storyboard] {segment.get('id', '?')}：提示词里的 {'、'.join(f'<Picture {t}>' for t in unresolved)} "
                f"对不上本段参考图（{', '.join(e['tag'] for e in order) or '本段没有绑定素材'}），交给校验修复")


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
    video_model: str = "comfyui_h3",
) -> Path:
    if video_model not in {"comfyui_h3", "comfyui_wan"}:
        raise ValueError("--video-model 只支持 comfyui_h3 或 comfyui_wan")
    # Retire the old I2VA-style validation contract; the selected Ref2V skill is authoritative.
    h3_gates = False
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
    ctx.log(f"[storyboard] 分镜参数来源：{_params_raw()[1]}")
    ctx.log(f"[storyboard] 分镜参数覆盖：{json.dumps(_eff, ensure_ascii=False) if _eff else '无（使用技能包默认）'}；Ref2V 专属旧式检查={'开' if h3_gates else '关闭'}")
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

    main: Dict[str, Any] = {"source": ctx.title, "aspectRatio": aspect, "promptLang": prompt_lang, "videoModel": video_model, "episodes": []}
    if path.is_file() and not ctx.force:
        main = read_json(path)
        main.setdefault("promptLang", prompt_lang)
    main["videoModel"] = video_model
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
    # Video prompt text is generated directly in the selected Ref2V skill format.
    # prompt_lang controls storyboard/frame language only; Ref2V prompt language is English.
    skill_root = Path(__file__).resolve().parents[2] / "skills"
    skill_path = (skill_root / "h3-prompt-writing" / "SKILL.md"
                  if video_model == "comfyui_h3"
                  else skill_root / "wan-3-0-ref2v-prompt-writing" / "SKILL.md")
    if not skill_path.is_file():
        raise FileNotFoundError(f"缺少 Ref2V 提示词 Skill：{skill_path}")
    ref2v_skill = skill_path.read_text(encoding="utf-8")
    language_contract = (
        "HARD OUTPUT CONTRACT: frame and shot are Simplified Chinese. "
        "The h3Prompt field contains the FINAL video prompt written directly for the selected Ref2V model, "
        "following the Ref2V skill included below; do not write I2V/T2V prompts or an intermediate prompt. "
        "Write video prompt text in English, preserving supplied dialogue verbatim in its original language. "
        "Preserve all supplied <Picture N> reference numbers for MiniMax H3 Ref2VA; for Wan 3.0 Ref2V use @ImageN "
        "with the same numeric indices and no MiniMax-only markup. Do not invent references or story events."
    )
    if video_model == "comfyui_h3":
        language_contract += H3_REFERENCE_CONTRACT
    language_contract += (
        "\n\nCUT FIELD CONTRACT: every cut.shot is always Simplified Chinese, regardless of promptLang; "
        "every cut.frame is Simplified Chinese. cut.characters must contain only the canonical character IDs "
        "provided by seedScenes (for example C01), never display names; cut.props must contain only canonical "
        "prop IDs (for example P01), never display names. sceneIndex and beats must follow the supplied seedScenes "
        "and script exactly. Do not invent or substitute IDs."
    )
    system = build_system(
        sk, PASSES,
        extra=(language_contract + "\n\n## Selected Ref2V skill (rules for h3Prompt field only)\n" + ref2v_skill
               + "\n\n## Outer response contract\nThe selected skill output-format instructions apply only to the h3Prompt field. "
               "Return the complete requested JSON object {\"ep\": N, \"segments\": [...]}; never return a standalone prompt outside JSON."),
    )

    for group in batches(todo, BATCH):
        seed = ctx.node_json(sk, ["seed", script_path, "--eps", f"{group[0]}-{group[-1]}", "--aspect", aspect])
        seed_eps = {e["ep"]: e for e in seed["episodes"]}
        batch_doc: Dict[str, Any] = {"source": ctx.title, "aspectRatio": aspect,
                                     "promptLang": prompt_lang, "videoModel": video_model, "episodes": []}
        if _params_override(h3_gates):
            batch_doc["params"] = _params_override(h3_gates)
        batch_path = work / "storyboard-batch.json"
        items: List[Item] = []
        for ep in group:
            seed_ep = seed_eps[ep]
            scene_ids = {s.get("sceneId") for s in seed_ep.get("seedScenes", [])}
            char_ids = {c for s in seed_ep.get("seedScenes", []) for c in s.get("characters", [])}
            prop_ids = {p for s in seed_ep.get("seedScenes", []) for p in s.get("props", [])}
            _scene_rows = seed_ep.get("seedScenes", [])
            _by_id: Dict[Any, List[int]] = {}
            for _row in _scene_rows:
                _by_id.setdefault(_row.get("sceneId"), []).append(_row.get("sceneIndex"))
            scene_rule = (
                "## 场次与节拍编号（硬规则）\n"
                f"本集共 {len(_scene_rows)} 场，按 sceneIndex 顺序依次切分，不得跳场、合并或调换顺序；段不跨场，段的 sceneIndex 必须等于它所属场的 sceneIndex。"
                "每个分镜的 beats 是该场自己的节拍编号 n（每场都从 1 重新开始，不接上一场），且 [起, 止] 不得超过该场的拍数。\n"
            )
            for _sid, _idx in _by_id.items():
                if len(_idx) > 1:
                    scene_rule += (f"注意：sceneId {_sid} 在本集出现了 {len(_idx)} 次（sceneIndex {'、'.join(str(i) for i in _idx)}），"
                                   "它们是不同的场（中间隔着别的场），必须分别成段，不能合并成一场。\n")
            scene_rule += "\n"
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
                    "每镜明确动作衔接、双方空间位置、攻防反应和动势承接；仍遵守时长、台词、画幅和所选 Ref2V Skill 的约束。\n"
                    + (
                        "模式=auto：判断打戏对剧情的作用。只有 high 重要程度（决定胜负、揭示主角实力、改变剧情走向或构成高潮）的打戏才写 fightPlan 并强化；普通推搡、短暂冲突、重点在对白/情绪的挥拳不强化，沿用普通分镜。"
                        if fight_enhancement == "auto" else
                        "模式=enhanced：所有真实打戏都写 fightPlan 并强化；非打戏场景完全沿用普通分镜，不得强行增加打斗。"
                    )
                    + "\n"
                )
            prompt = (
                f"## 任务\n给第 {ep} 集切分镜：先按剧情单元分段（每段 9–20 秒、不跨场），段内切 4–8 秒的分镜。\n\n"
                + f"## 画幅与语言\naspectRatio = {aspect}；promptLang = {prompt_lang}（仅控制 frame、shot 等分镜字段语言；最终 Ref2V 视频提示词遵循所选 Skill，使用英文并保留对白原文）。\n\n"
                + f"## 视觉风格（frame、shot 和最终 Ref2V 视频提示词必须统一遵守）\n{style_hint}\n保持同一风格贯穿所有镜头，不要在漫剧插画与真人摄影之间混用。\n\n"
                + "## 多人同框说明\n单个分镜 characters 超过 3 人时，该分镜必须写非空 note：说明前景主体是谁、其余人物作为背景群像如何站位，以及哪些人物不单独特写。不超过 3 人可不写。\n\n"
                + "## 连续性状态表（必须落实到 JSON）\n每个 segment 增加 continuityMode：continuous 或 hard_cut；每个 cut 增加 continuity：{mode, stateBefore, stateAfter, referenceFrame}。mode 只允许 inherit 或 cut。stateBefore/stateAfter 用简短中文记录所有可见角色的位置（画面左右/前后、朝向、姿势）、服装、手持道具、伤势，以及关键物件位置；状态要具体可画，不能写“保持一致”。\n同场连续动作默认 inherit：下一切 stateBefore 必须承接上一切 stateAfter，动作按“起始姿态→动作中→结果姿态”推进；referenceFrame 填上一切的帧标识（如 E01-01/f1.png），供下游把前一帧作为构图/姿态参考。只有明确的时间跳跃、换地点或剧情要求重置构图时才用 cut，并写明原因；不要为了继承而强行复制上一镜构图。跨 segment 若同场且动作连续，也要在下一段首切的 stateBefore 承接前段末切 stateAfter，并把 referenceFrame 指向前段末帧。\nframe 只负责当前瞬间的构图、人物位置/姿态和状态；shot 负责几秒内动作过程、镜头运动与动作衔接。角色脸、服装、场景材质以设定图为准。必须在输出 JSON 中提供上述字段，不得只写在说明文字里。\n\n"
                + scene_rule
                + f"## 工作底稿 seedScenes（节拍编号、台词、每拍秒数已算好，不要重估；镜头必须认领这些节拍）\n{json.dumps(seed_ep.get('seedScenes', []), ensure_ascii=False, indent=1)}\n\n"
                + f"## 场景卡\n{json.dumps(scene_cards, ensure_ascii=False, indent=1)}\n\n"
                + f"## 角色卡（frame 里直呼角色名；视频正文用通用身份，不写名字）\n{json.dumps(char_cards, ensure_ascii=False, indent=1)}\n\n"
                + f"## 叙事道具\n{json.dumps(prop_cards, ensure_ascii=False, indent=1)}\n\n"
                + "## 资产绑定硬规则\n每个 cut.characters 只能填写角色卡中的准确角色名，cut.props 只能填写叙事道具卡中的准确名称；不得改名、缩写、合并或用代称。每切必须完整列出该画面实际可见或正在使用的角色与道具，空镜使用空数组。sceneIndex 必须指向实际场景卡。程序会根据这些字段生成唯一 assetRefs（角色→场景→道具），不要自行生成、复制或改写 assetRefs。资产绑定不得遗漏提示词中明确出现的角色、场景或道具，也不得把未出现在该切的资产加入绑定。\n\n"
                + (f"## 每场第一段的开场走位句（blocking 请照抄对应那一场的这一句）\n" + "\n".join(first_blocking) + "\n\n"
                   if first_blocking else "")
                + "## 输出\n只输出这一集的 JSON 对象：{\"ep\": N, \"segments\": [...]}，不要 seedScenes。"
                  "视频提示词是最终 Ref2V 提示词，不得生成 I2V/T2V 中间格式；时长变化时同步检查提示词动作节奏，但不要添加旧式对齐行或镜头标记。"
            )
            prompt += fight_instruction
            existing_cache = next((p for p in _episode_cache_candidates(work, ep) if p.is_file()), None)
            cache_path = existing_cache or (work / f"ep{ep:03d}.json")
            items.append(Item(
                str(ep), system, prompt, tokens=ep_tokens(ep), cache=cache_path,
                purpose=f"storyboard-generation:E{ep:02d}",
            ))

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

            # Bind every cut to deterministic, unique asset IDs. The model writes the
            # semantic names; the program resolves those names to canonical IDs so
            # downstream image/video stages can audit the exact asset-to-prompt mapping.
            _script_ep = next(
                (row for row in script.get("episodes", []) if int(row.get("ep", 0)) == ep),
                {},
            )
            _scene_id_by_index = {
                index: str(row.get("sceneId"))
                for index, row in enumerate(_script_ep.get("scenes", []), start=1)
                if row.get("sceneId")
            }
            _char_id_by_name = {}
            for _cid, _card in outline_chars.items():
                _name = str(_card.get("name") or "").strip()
                if _name:
                    _char_id_by_name.setdefault(_name, str(_cid))
            for _card in cast_by_id.values():
                _name = str(_card.get("name") or "").strip()
                if _name and _card.get("id"):
                    _char_id_by_name.setdefault(_name, str(_card["id"]))
            _prop_id_by_name = {}
            for _pid, _card in outline_props.items():
                _name = str(_card.get("name") or "").strip()
                if _name:
                    _prop_id_by_name.setdefault(_name, str(_pid))

            # The task key is authoritative; models sometimes copy segment IDs from examples.
            for segment_index, segment in enumerate(episode["segments"], start=1):
                if not isinstance(segment, dict):
                    raise ValueError(f"第 {segment_index} 段必须是 JSON 对象")
                segment["id"] = f"E{ep:02d}-{segment_index:02d}"
                scene_id = _scene_id_by_index.get(int(segment.get("sceneIndex") or 0))
                for cut_index, cut in enumerate(segment.get("cuts") or [], start=1):
                    if not isinstance(cut, dict):
                        raise ValueError(f"{segment['id']} 第 {cut_index} 切必须是 JSON 对象")
                    refs = []
                    seen_refs = set()

                    def bind_asset(kind, asset_id, name):
                        name = str(name or "").strip()
                        if not name:
                            return
                        stable_id = f"{kind}:{asset_id or name}"
                        if stable_id in seen_refs:
                            return
                        seen_refs.add(stable_id)
                        refs.append({"id": stable_id, "kind": kind, "name": name})

                    _character_names = list(cut.get("characters") or [])
                    _prop_names = list(cut.get("props") or [])
                    # The validator and downstream stages require canonical C/P IDs in the JSON arrays;
                    # preserve display names separately in assetRefs for reference-image lookup.
                    cut["characters"] = [
                        _char_id_by_name.get(str(name).strip(), str(name).strip())
                        for name in _character_names
                    ]
                    cut["props"] = [
                        _prop_id_by_name.get(str(name).strip(), str(name).strip())
                        for name in _prop_names
                    ]
                    for name in _character_names:
                        bind_asset("character", _char_id_by_name.get(str(name).strip(), str(name).strip()), name)
                    if scene_id:
                        bind_asset("scene", scene_id, scene_id)
                    for name in _prop_names:
                        bind_asset("prop", _prop_id_by_name.get(str(name).strip(), str(name).strip()), name)
                    cut["assetRefs"] = refs
                if video_model == "comfyui_h3":
                    _apply_ref_order(ctx, segment)
            _doc["episodes"] = sorted(
                [e for e in _doc["episodes"] if e["ep"] != ep] + [episode], key=lambda e: e["ep"])
            write_json(_path, _doc)

        def validate_batch(_p=batch_path, _doc=batch_doc) -> Result:
            # Deterministic repairs: missing size terms, stale H3 timing, tiny cut overruns,
            # and episode overruns that can be trimmed from action-only cuts without touching dialogue.
            basic_fixes = repair_deterministic_storyboard(_doc, prompt_lang, script)
            timing_fixes = repair_minor_cut_durations(_doc, prompt_lang)
            fit_fixes = repair_dialogue_fit(_doc, script, prompt_lang)
            duration_fixes = balance_episode_durations(_doc, script)
            for fix in [*basic_fixes, *timing_fixes, *fit_fixes, *duration_fixes]:
                ctx.log(f"[storyboard] 自动修正：{fix}")
            if basic_fixes or timing_fixes or fit_fixes or duration_fixes:
                write_json(_p, _doc)

            result = ctx.run(sk, ["validate", _p, *_refs(ctx)], cwd=sb_dir)
            if not result.ok and _repair_ref2v_prompts(ctx, _doc, _p, result, video_model):
                result = ctx.run(sk, ["validate", _p, *_refs(ctx)], cwd=sb_dir)
            # One targeted prompt-only repair at most. Never regenerate an episode or rewrite cuts.
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

    # h3Prompt is authored directly as the selected model's final Ref2V prompt.
    # Keep videoPrompt as the downstream stable field; avoid a second LLM rewrite call.
    main["videoPromptFormat"] = "ref2v-v1"
    for episode in main.get("episodes", []):
        for segment in episode.get("segments", []):
            if not isinstance(segment, dict):
                continue
            prompt = str(segment.get("h3Prompt") or "").strip()
            if not prompt:
                raise ValueError(f"{segment.get('id', '?')} 缺少最终 Ref2V 视频提示词")
            source_hash = hashlib.sha1(prompt.encode("utf-8")).hexdigest()
            segment["videoPrompt"] = prompt
            segment["videoModel"] = video_model
            segment["videoPromptFormat"] = "ref2v-v1"
            segment["videoPromptSourceHash"] = source_hash
    write_json(path, main)

    refs = _refs(ctx)
    ctx.render_to(sk, ["render", path, "--md", *refs], sb_dir / f"{path.stem}.md", cwd=sb_dir)
    ctx.render_to(sk, ["render", path, "--html", *refs], sb_dir / "storyboard-report.html", cwd=sb_dir)
    export = ctx.run(sk, ["export", path, *refs, "--out", sb_dir], cwd=sb_dir)
    ctx.log("已导出投产包（每段一个文件夹 + manifest.json）" if export.ok
            else f"⚠ export 失败：{export.text[:200]}")
    return path
