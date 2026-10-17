"""Cheap, deterministic storyboard repairs that avoid regenerating whole episodes."""
from __future__ import annotations

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
    starts: List[float] = []
    total = 0.0
    for cut in cuts:
        starts.append(round(total, 1))
        try:
            total += float(cut.get("seconds", 0))
        except (TypeError, ValueError):
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
    if not lines:
        return False
    lines[0] = _alignment(cuts, lang)
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
            if 5.0 < seconds <= 5.0 + max_overrun:
                cut["seconds"] = 5.0
                repaired.append(f"{segment.get('id', '?')}#{index}: {seconds:.1f}s -> 5.0s")
                changed = True
        if changed:
            sync_segment_timing(segment, lang)
    return repaired


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
    marker = rf"\\[镜头\\s*{index}\\]" if lang == "zh" else rf"\\[Shot\\s*{index}\\]"
    next_marker = rf"\\[镜头\\s*{index + 1}\\]" if lang == "zh" else rf"\\[Shot\\s*{index + 1}\\]"
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
        return False
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
