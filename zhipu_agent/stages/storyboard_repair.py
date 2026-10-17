"""Cheap, deterministic storyboard repairs that avoid regenerating whole episodes."""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


EN_I2VA = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced."
)
ZH_I2VA = "目标视频在 0.00 秒处完全参照图 1（来自镜头 1）。"


def _segments(doc: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for episode in doc.get("episodes", []):
        yield from episode.get("segments", [])


def _starts(cuts: List[Dict[str, Any]]) -> List[float]:
    """Match the validator's JavaScript Math.round(n * 10) / 10 exactly."""
    starts: List[float] = []
    total = 0.0
    for cut in cuts:
        starts.append(_r1(total))
        try:
            total += float(cut.get("seconds", 0))
        except (TypeError, ValueError, AttributeError):
            pass
    return starts


def _timecode(seconds: float) -> str:
    minutes = int(seconds // 60)
    whole = int(seconds % 60)
    millis = round((seconds - int(seconds)) * 1000)
    if millis == 1000:
        whole += 1
        millis = 0
    if whole == 60:
        minutes += 1
        whole = 0
    return f"{minutes:02d}:{whole:02d}.{millis:03d}"


def _alignment(cuts: List[Dict[str, Any]], lang: str) -> str:
    if len(cuts) <= 1:
        return ZH_I2VA if lang == "zh" else EN_I2VA
    starts = _starts(cuts)
    if lang == "zh":
        items = [
            f"图 {i}（来自镜头 {i}）对齐目标视频 {starts[i - 1]:.2f} 秒处"
            for i in range(1, len(cuts) + 1)
        ]
        return "参考图与目标视频的对齐——" + "；".join(items) + "。"
    items = [
        f"Picture {i} (from Shot {i}) aligns with the {starts[i - 1]:.2f}-second mark of the target video"
        for i in range(1, len(cuts) + 1)
    ]
    return "How the reference pictures align with the target video — " + "; ".join(items) + "."


def sync_segment_timing(segment: Dict[str, Any], lang: str = "en") -> bool:
    """Rebuild only deterministic H3 alignment/cut-time text after seconds change."""
    cuts = segment.get("cuts")
    prompt = segment.get("h3Prompt")
    if not isinstance(cuts, list) or not cuts or not isinstance(prompt, str) or not prompt.strip():
        return False

    lines = prompt.splitlines()
    # Models occasionally emit a leading blank line. Replace the first actual
    # content line, not the blank line, or the validator still sees a stale header.
    first_content = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_content is None:
        return False
    lines[first_content] = _alignment(cuts, lang)
    if first_content:
        lines = lines[first_content:]
    prompt = "\n".join(lines)
    starts = _starts(cuts)

    for index in range(2, len(cuts) + 1):
        timestamp = _timecode(starts[index - 1])
        if lang == "zh":
            pattern = re.compile(rf"(\[镜头\s*{index}\]\s*于\s*)\d{{2}}:\d{{2}}\.\d{{3}}(，)")
        else:
            pattern = re.compile(rf"(\[Shot\s*{index}\]\s*At\s*)\d{{2}}:\d{{2}}\.\d{{3}}(,)")
        prompt, count = pattern.subn(rf"\g<1>{timestamp}\g<2>", prompt, count=1)
        # Don't silently invent a missing shot paragraph; validation will report it.
    changed = prompt != segment.get("h3Prompt")
    segment["h3Prompt"] = prompt
    return changed


def repair_minor_cut_durations(doc: Dict[str, Any], lang: str = "en",
                               max_overrun: float = 0.2) -> List[str]:
    """Snap tiny upper-bound overruns (e.g. 5.1s) to 5.0s, then resync H3 timing."""
    repaired: List[str] = []
    try:
        upper = float((doc.get("params") or {}).get("maxCutSeconds", 10))
    except (TypeError, ValueError):
        upper = 10.0
    for segment in _segments(doc):
        cuts = segment.get("cuts")
        if not isinstance(cuts, list):
            continue
        changed = False
        for index, cut in enumerate(cuts, 1):
            try:
                seconds = float(cut.get("seconds"))
            except (TypeError, ValueError, AttributeError):
                continue
            if upper < seconds <= upper + max_overrun:
                cut["seconds"] = upper
                repaired.append(f"{segment.get('id', '?')}#{index}: {seconds:.1f}s -> {upper:.1f}s")
                changed = True
        if changed:
            sync_segment_timing(segment, lang)
    return repaired


def _r1(x: float) -> float:
    """与 Node 的 Math.round(n * 10) / 10 一致（四舍五入，非银行家舍入）。"""
    return math.floor(x * 10 + 0.5) / 10


def repair_dialogue_fit(doc: Dict[str, Any], script: Dict[str, Any], lang: str = "zh") -> List[str]:
    """认领节拍的台词秒数 > 分镜秒数时，把分镜加长到台词秒数（不超过 maxCutSeconds），再重建 H3 时间。

    台词秒数的算法和校验脚本一致：去空白字数 / charsPerSecond（默认 4.5），每句先四舍五入到 0.1 再求和。
    台词本身超过上限的不动，留给模型回炉（要拆切或改剧本）。
    """
    params = doc.get("params") or {}
    try:
        upper = float(params.get("maxCutSeconds", 10))
    except (TypeError, ValueError):
        upper = 10.0
    try:
        cps = float((script.get("params") or {}).get("charsPerSecond", 4.5))
    except (TypeError, ValueError):
        cps = 4.5
    if cps <= 0:
        cps = 4.5
    script_eps = {ep.get("ep"): ep for ep in script.get("episodes", [])}
    fixes: List[str] = []
    for episode in doc.get("episodes", []):
        scenes = (script_eps.get(episode.get("ep")) or {}).get("scenes", [])
        for segment in episode.get("segments", []):
            try:
                scene_index = int(segment.get("sceneIndex", 0) or 0)
            except (TypeError, ValueError):
                continue
            scene = scenes[scene_index - 1] if 1 <= scene_index <= len(scenes) else {}
            flow = scene.get("flow", []) if isinstance(scene, dict) else []
            changed = False
            for index, cut in enumerate(segment.get("cuts", []), 1):
                bounds = cut.get("beats", [])
                if not isinstance(bounds, list) or len(bounds) != 2:
                    continue
                try:
                    start, end = int(bounds[0]), int(bounds[1])
                    seconds = float(cut.get("seconds"))
                except (TypeError, ValueError):
                    continue
                if not 1 <= start <= end <= len(flow):
                    continue
                dialogue = 0.0
                for beat in flow[start - 1:end]:
                    if isinstance(beat, dict) and isinstance(beat.get("line"), str):
                        dialogue += _r1(len(re.sub(r"\s+", "", beat["line"])) / cps)
                if dialogue > seconds + 1e-9 and _r1(dialogue) <= upper:
                    cut["seconds"] = _r1(dialogue)
                    fixes.append(f"{segment.get('id', '?')}#{index}: 台词 {_r1(dialogue):.1f}s，分镜 {seconds:.1f}s -> {cut['seconds']:.1f}s")
                    changed = True
            if changed:
                sync_segment_timing(segment, lang)
    return fixes


def repair_deterministic_storyboard(doc: Dict[str, Any], lang: str = "en") -> List[str]:
    """Apply safe, deterministic repairs that do not invent story content."""
    shot_size_terms = {
        "extreme-wide": "大远景",
        "wide": "全景",
        "medium": "中景",
        "close": "特写",
        "extreme-close": "大特写",
    }

    repaired: List[str] = []
    for segment in _segments(doc):
        for index, cut in enumerate(segment.get("cuts", []), 1):
            size = cut.get("size")
            term = shot_size_terms.get(size)
            frame = str(cut.get("frame") or "")
            if term and frame.strip() and term not in frame:
                cut["frame"] = f"{term}，{frame.lstrip()}"
                repaired.append(f"{segment.get('id', '?')}#{index}: 补齐景别词「{term}」")
        if sync_segment_timing(segment, lang):
            repaired.append(f"{segment.get('id', '?')}: 重建 H3 对齐行与切点时间")
    return repaired


def balance_episode_durations(doc: Dict[str, Any], script: Dict[str, Any],
                              tolerance: float = 1.0, min_cut_seconds: float = 2.0) -> List[str]:
    """Trim only action-only cuts, in 0.1s steps, when an episode exceeds its target window."""
    params = doc.get("params") or {}
    try:
        tolerance = float(params.get("tolerance", tolerance))
        min_cut_seconds = float(params.get("minCutSeconds", min_cut_seconds))
    except (TypeError, ValueError):
        pass
    script_eps = {ep.get("ep"): ep for ep in script.get("episodes", [])}
    fixes: List[str] = []
    for episode in doc.get("episodes", []):
        source_ep = script_eps.get(episode.get("ep"))
        try:
            target = float(source_ep.get("targetSeconds", 0)) if source_ep else 0.0
        except (TypeError, ValueError):
            target = 0.0
        if target <= 0:
            continue
        segments = episode.get("segments", [])
        total = sum(float(c.get("seconds", 0) or 0) for seg in segments for c in seg.get("cuts", []))
        excess_ticks = int(round(max(0.0, total - target * (1 + tolerance)) * 10))
        if excess_ticks <= 0:
            continue

        eligible: List[Tuple[Dict[str, Any], Dict[str, Any], int, float]] = []
        scenes = source_ep.get("scenes", []) if source_ep else []
        for segment in segments:
            scene_index = int(segment.get("sceneIndex", 0) or 0)
            scene = scenes[scene_index - 1] if 1 <= scene_index <= len(scenes) else {}
            beats = scene.get("flow", []) if isinstance(scene, dict) else []
            for cut_index, cut in enumerate(segment.get("cuts", []), 1):
                bounds = cut.get("beats", [])
                if not isinstance(bounds, list) or len(bounds) != 2:
                    continue
                try:
                    start, end = int(bounds[0]), int(bounds[1])
                    seconds = float(cut.get("seconds", 0) or 0)
                except (TypeError, ValueError):
                    continue
                claimed = beats[start - 1:end] if 1 <= start <= end <= len(beats) else []
                if not claimed or any(isinstance(beat, dict) and isinstance(beat.get("line"), str) for beat in claimed):
                    continue
                capacity = max(0, int(round((seconds - min_cut_seconds) * 10)))
                if capacity:
                    eligible.append((segment, cut, cut_index, capacity))

        original_excess = excess_ticks
        while excess_ticks > 0:
            changed = False
            # Trim the longest action-only cuts first; never touch dialogue-bearing cuts.
            eligible.sort(key=lambda item: float(item[1].get("seconds", 0) or 0), reverse=True)
            for item_index, (segment, cut, cut_index, capacity) in enumerate(eligible):
                seconds = float(cut.get("seconds", 0) or 0)
                if capacity <= 0 or seconds <= min_cut_seconds + 0.001:
                    continue
                cut["seconds"] = round(seconds - 0.1, 1)
                excess_ticks -= 1
                eligible[item_index] = (segment, cut, cut_index, capacity - 1)
                fixes.append(f"{segment.get('id', '?')}#{cut_index}: 动作镜头时长 -0.1s")
                changed = True
                if excess_ticks <= 0:
                    break
            if not changed:
                break

        if excess_ticks < original_excess:
            for segment in segments:
                sync_segment_timing(segment, doc.get("promptLang", "zh"))
            fixes.append(
                f"E{int(episode.get('ep', 0)):02d}: 总时长减少 {(original_excess - excess_ticks) / 10:.1f}s"
            )
    return fixes


def segment_h3_violation_refs(lines: Iterable[str]) -> Dict[str, List[str]]:
    """Collect segment-level H3 language/structure errors for a prompt-only repair."""
    found: Dict[str, List[str]] = {}
    markers = (
        "首行对齐指令", "核心字段", "描述正文缺", "缺「[镜头", "缺「[Shot",
        "h3Prompt", "promptLang=", "整体视听描述", "integrated_multimodal_description",
        "整体音景", "overall_soundscape", "非叙事配乐", "non_diegetic_music",
    )
    for line in lines:
        match = re.search(r"\b(E\d{2}-\d{2})\b", line)
        if match and any(marker in line for marker in markers):
            found.setdefault(match.group(1), []).append(line)
    return found


def cut_violation_refs(lines: Iterable[str]) -> Dict[Tuple[str, int], List[str]]:
    """Group only errors with an explicit segment/cut id; episode-wide errors are not routed."""
    found: Dict[Tuple[str, int], List[str]] = {}
    for line in lines:
        match = re.search(r"\b(E\d{2}-\d{2})#(\d+)\b", line)
        if match:
            key = (match.group(1), int(match.group(2)))
            found.setdefault(key, []).append(line)
    return found



def extract_h3_cut_text(segment: Dict[str, Any], index: int, lang: str = "en") -> str:
    """Return one H3 shot block, excluding the next shot and soundscape fields."""
    prompt = segment.get("h3Prompt")
    if not isinstance(prompt, str):
        return ""
    marker = rf"\[镜头\s*{index}\]" if lang == "zh" else rf"\[Shot\s*{index}\]"
    next_marker = rf"\[镜头\s*{index + 1}\]" if lang == "zh" else rf"\[Shot\s*{index + 1}\]"
    field_marker = "整体音景：" if lang == "zh" else "overall_soundscape:"
    start = re.search(marker, prompt)
    if not start:
        return ""
    ends = []
    nxt = re.search(next_marker, prompt[start.end():])
    if nxt:
        ends.append(start.end() + nxt.start())
    field = prompt.find(field_marker, start.end())
    if field >= 0:
        ends.append(field)
    end = min(ends) if ends else len(prompt)
    return prompt[start.start():end].strip()

def replace_h3_cut_text(segment: Dict[str, Any], index: int, text: str, lang: str = "en") -> bool:
    """Replace just one [Shot k]/[镜头 k] block, leaving other shots untouched."""
    prompt = segment.get("h3Prompt")
    if not isinstance(prompt, str) or not text.strip():
        return False
    if lang == "zh":
        marker = rf"\[镜头\s*{index}\]"
        next_marker = rf"\[镜头\s*{index + 1}\]"
        field_marker = "整体音景："
    else:
        marker = rf"\[Shot\s*{index}\]"
        next_marker = rf"\[Shot\s*{index + 1}\]"
        field_marker = "overall_soundscape:"

    start_match = re.search(marker, prompt)
    if not start_match:
        # Missing [Shot k]/[镜头 k] marker: insert this block before the next shot,
        # or before the soundscape field for the final shot. Do not discard neighbours.
        next_match = re.search(next_marker, prompt)
        field_pos = prompt.find(field_marker)
        candidates = [m.start() for m in (next_match,) if m]
        if field_pos >= 0:
            candidates.append(field_pos)
        if not candidates:
            return False
        insert_at = min(candidates)
        prefix = prompt[:insert_at].rstrip()
        suffix = prompt[insert_at:].lstrip("\n")
        segment["h3Prompt"] = prefix + "\n" + text.strip() + "\n" + suffix
        return True
    end_candidates = []
    next_match = re.search(next_marker, prompt[start_match.end():])
    if next_match:
        end_candidates.append(start_match.end() + next_match.start())
    field_pos = prompt.find(field_marker, start_match.end())
    if field_pos >= 0:
        end_candidates.append(field_pos)
    end = min(end_candidates) if end_candidates else len(prompt)
    replacement = text.strip()
    segment["h3Prompt"] = prompt[:start_match.start()] + replacement + "\n" + prompt[end:].lstrip("\n")
    return True
