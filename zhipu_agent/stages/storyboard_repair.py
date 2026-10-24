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
        upper = float((doc.get("params") or {}).get("maxCutSeconds", 8))
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
        upper = float(params.get("maxCutSeconds", 8))
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
                if dialogue > seconds and _r1(dialogue) <= upper:
                    # 校验脚本是严格比较 dlg > seconds（浮点累加误差也算超），所以必须取到 >= dialogue 的值
                    target = _r1(dialogue)
                    if target < dialogue:
                        target = math.ceil(dialogue * 100) / 100
                    cut["seconds"] = target
                    fixes.append(f"{segment.get('id', '?')}#{index}: 台词 {dialogue:.2f}s，分镜 {seconds:.1f}s -> {cut['seconds']:.2f}s")
                    changed = True
            if changed:
                sync_segment_timing(segment, lang)
    return fixes


_SIZE_ALIASES = {
    "extreme-wide": ("extremewide", "extremewideshot", "大远景", "极远景", "远景大全景"),
    "wide": ("wide", "wideshot", "全景", "远景", "longshot"),
    "medium": ("medium", "mediumshot", "中景", "midshot", "mediumwide"),
    "close": ("close", "closeup", "closeupshot", "特写", "近景", "mediumclose", "mediumcloseup", "mediumcloseupshot", "中近景", "中特写"),
    "extreme-close": ("extremeclose", "extremecloseup", "大特写", "极特写"),
}


def _norm_size(value: Any) -> Optional[str]:
    """把模型写的景别别名（大小写、连字符、Shot 后缀、中文词）归到枚举；认不出来的返回 None，不猜。"""
    key = re.sub(r"[\s_\-]+", "", str(value or "")).lower()
    if not key:
        return None
    for canon, names in _SIZE_ALIASES.items():
        if key == canon.replace("-", "") or key in names:
            return canon
    return None


def _strip_wrappers(text: str, chars: str) -> str:
    return re.sub("[" + re.escape(chars) + "]", "", text).strip()


def _clean_shot(text: str) -> str:
    """Seedance 镜头正文里不许有 H3 标记／台词块／协议符号：台词由程序从剧本补，所以整块删；<> 和（）只去符号留文字。"""
    out = re.sub(r"<d>.*?</d>", "", text, flags=re.S | re.I)
    out = re.sub(r"\{[^{}]*\}", "", out)
    out = re.sub(r"\[Shot\s*\d+\]|\[镜头\s*\d+\]|<\s*Picture[^>]*>|@\s*\[?图片\s*\d*\]?", "", out, flags=re.I)
    out = re.sub(r"[<>＜＞（）()\{\}]", "", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _beat_numbers(value: Any) -> List[int]:
    """从 beats 的各种写法里取出整数：[9]、9、"9"、"9-12"、[9, None]、[9, "12"]、[9,10,11]。"""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [int(value)]
    if isinstance(value, str):
        return [int(x) for x in re.findall(r"\d+", value)]
    if isinstance(value, (list, tuple)):
        out: List[int] = []
        for item in value:
            out.extend(_beat_numbers(item))
        return out
    return []


def repair_beats_and_symbols(doc: Dict[str, Any]) -> List[str]:
    """确定性修复：beats 缺终点、景别别名、soundscape/music 自带的 <> 与括号。不改剧情内容。"""
    repaired: List[str] = []
    for episode in doc.get("episodes", []) or []:
        flat = [(seg, idx, cut) for seg in episode.get("segments", []) or [] if isinstance(seg, dict)
                for idx, cut in enumerate(seg.get("cuts", []) or [], 1) if isinstance(cut, dict)]
        for pos, (seg, idx, cut) in enumerate(flat):
            nums = _beat_numbers(cut.get("beats"))
            start = None
            if len(nums) >= 2 and not (isinstance(cut.get("beats"), list) and len(cut["beats"]) == 2
                                       and all(isinstance(x, int) and not isinstance(x, bool) for x in cut["beats"])):
                cut["beats"] = [min(nums), max(nums)]
                repaired.append(f"{seg.get('id', '?')}#{idx}: beats 规整为 [{min(nums)}, {max(nums)}]")
            elif len(nums) == 1:
                start = nums[0]
            if start is not None:
                end = start
                for nxt_seg, _i, nxt in flat[pos + 1:]:
                    if nxt_seg.get("sceneIndex") != seg.get("sceneIndex"):
                        break
                    nn = _beat_numbers(nxt.get("beats"))
                    if nn and min(nn) > start:
                        end = min(nn) - 1
                    break
                cut["beats"] = [start, end]
                repaired.append(f"{seg.get('id', '?')}#{idx}: beats 补齐为 [{start}, {end}]")
            canon = _norm_size(cut.get("size"))
            if canon and cut.get("size") != canon:
                repaired.append(f"{seg.get('id', '?')}#{idx}: 景别「{cut.get('size')}」归一为「{canon}」")
                cut["size"] = canon
        for seg in episode.get("segments", []) or []:
            if not isinstance(seg, dict):
                continue
            for idx, cut in enumerate(seg.get("cuts", []) or [], 1):
                if isinstance(cut, dict) and isinstance(cut.get("shot"), str):
                    cleaned = _clean_shot(cut["shot"])
                    if cleaned != cut["shot"]:
                        cut["shot"] = cleaned
                        repaired.append(f"{seg.get('id', '?')}#{idx}: 去掉 shot 里的协议符号")
            for key, chars in (("soundscape", "<>＜＞"), ("music", "（）()")):
                value = seg.get(key)
                if isinstance(value, str):
                    cleaned = _strip_wrappers(value, chars)
                    if cleaned != value:
                        seg[key] = cleaned
                        repaired.append(f"{seg.get('id', '?')}: 去掉 {key} 自带的符号")
    return repaired


def _int_pair(value: Any) -> Optional[Tuple[int, int]]:
    if (isinstance(value, list) and len(value) == 2
            and all(isinstance(x, int) and not isinstance(x, bool) for x in value)):
        return value[0], value[1]
    return None


def repair_beat_overlaps(doc: Dict[str, Any]) -> List[str]:
    """同场相邻镜头的节拍区间重叠时消除：重复的那一拍归前一镜（后一镜起点后移）；后一镜会变空时改缩前一镜终点。"""
    repaired: List[str] = []
    for episode in doc.get("episodes", []) or []:
        flat = [(seg, idx, cut) for seg in episode.get("segments", []) or [] if isinstance(seg, dict)
                for idx, cut in enumerate(seg.get("cuts", []) or [], 1) if isinstance(cut, dict)]
        for pos in range(1, len(flat)):
            pseg, pidx, prev = flat[pos - 1]
            seg, idx, cur = flat[pos]
            if pseg.get("sceneIndex") != seg.get("sceneIndex"):
                continue
            a, b = _int_pair(prev.get("beats")) or (None, None)
            c, d = _int_pair(cur.get("beats")) or (None, None)
            if a is None or c is None or c > b:
                continue
            if d > b:
                cur["beats"] = [b + 1, d]
                repaired.append(f"{seg.get('id', '?')}#{idx}: 与上一镜重叠，起点 {c} -> {b + 1}")
            elif b > a and c > a:
                prev["beats"] = [a, c - 1]
                repaired.append(f"{pseg.get('id', '?')}#{pidx}: 与下一镜重叠，终点 {b} -> {c - 1}")
    return repaired


def repair_short_cuts(doc: Dict[str, Any], lang: str = "en") -> List[str]:
    """低于 minCutSeconds 的镜头补到下限（段总时长不超过 maxSegmentSeconds 才补），并重建 H3 时间。"""
    params = doc.get("params") or {}
    try:
        lower = float(params.get("minCutSeconds", 4))
        cap = float(params.get("maxSegmentSeconds", 20))
    except (TypeError, ValueError):
        return []
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
            if 0 < seconds < lower:
                total = sum(float(c.get("seconds") or 0) for c in cuts if isinstance(c, dict))
                if total - seconds + lower <= cap + 1e-9:
                    cut["seconds"] = lower
                    repaired.append(f"{segment.get('id', '?')}#{index}: {seconds:.1f}s 低于下限，补到 {lower:.1f}s")
                    changed = True
        if changed:
            sync_segment_timing(segment, lang)
    return repaired



def _camera_for_cut(cut: Dict[str, Any]) -> Optional[str]:
    """Normalize paired camera labels emitted by LLMs to one canonical H3 enum."""
    camera = str(cut.get("camera") or "").strip()
    valid = {
        "Static Shot", "Push In", "Pull Out", "Zoom In", "Zoom Out",
        "Pan Left", "Pan Right", "Truck Left", "Truck Right",
        "Tilt Up", "Tilt Down", "Pedestal Up", "Pedestal Down",
        "Arc Shot", "Tracking Shot", "Shake Slightly", "Shake Strongly",
        "POV", "Roll Clockwise", "Roll Counterclockwise",
    }
    if camera in valid:
        return camera
    aliases = {
        "Pan Left/Right": ("Pan Left", "Pan Right"),
        "Truck Left/Right": ("Truck Left", "Truck Right"),
        "Tilt Up/Down": ("Tilt Up", "Tilt Down"),
        "Pedestal Up/Down": ("Pedestal Up", "Pedestal Down"),
        "Zoom In/Out": ("Zoom In", "Zoom Out"),
        "Roll Clockwise/Counterclockwise": ("Roll Clockwise", "Roll Counterclockwise"),
        "Roll CW/CCW": ("Roll Clockwise", "Roll Counterclockwise"),
        "Shake Slightly/Strongly": ("Shake Slightly", "Shake Strongly"),
    }
    choices = aliases.get(camera)
    if not choices:
        return None
    text = " ".join(str(cut.get(key) or "") for key in ("shot", "frame", "note")).lower()
    directional = {
        "Pan Left/Right": (("left", "向左", "左摇", "左移"), ("right", "向右", "右摇", "右移")),
        "Truck Left/Right": (("left", "向左", "左移"), ("right", "向右", "右移")),
        "Tilt Up/Down": (("up", "向上", "上摇", "仰"), ("down", "向下", "下摇", "俯")),
        "Pedestal Up/Down": (("up", "向上", "上升", "升起"), ("down", "向下", "下降")),
        "Zoom In/Out": (("in", "推进", "拉近", "放大"), ("out", "拉远", "缩小")),
        "Roll Clockwise/Counterclockwise": (("clockwise", "顺时针", "顺旋"), ("counterclockwise", "逆时针", "逆旋")),
        "Roll CW/CCW": (("clockwise", "顺时针", "顺旋"), ("counterclockwise", "逆时针", "逆旋")),
        "Shake Slightly/Strongly": (("slight", "轻微", "微晃"), ("strong", "强烈", "剧烈")),
    }.get(camera, ((), ()))
    for choice, words in zip(choices, directional):
        if any(word in text for word in words):
            return choice
    # Ambiguous paired labels have no single official enum. Use the first listed
    # direction as a stable fallback and log the repair; never leave an invalid enum.
    return choices[0]


def _script_scene_for_segment(segment: Dict[str, Any], script: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(script, dict):
        return None
    try:
        ep_number = int(str(segment.get("id", "")).split("-")[0].lstrip("E"))
        scene_index = int(segment.get("sceneIndex", 0))
    except (TypeError, ValueError):
        return None
    episode = next((e for e in script.get("episodes", []) if int(e.get("ep", -1)) == ep_number), None)
    scenes = episode.get("scenes", []) if isinstance(episode, dict) else []
    return scenes[scene_index - 1] if 1 <= scene_index <= len(scenes) else None


def _repair_h3_protocol(segment: Dict[str, Any], scene: Optional[Dict[str, Any]], lang: str) -> List[str]:
    """Deterministically restore H3 skeleton, shot markers, camera terms and claimed dialogue."""
    cuts = segment.get("cuts")
    prompt = segment.get("h3Prompt")
    if not isinstance(cuts, list) or not cuts or not isinstance(prompt, str) or not prompt.strip():
        return []
    lang = "zh" if lang == "zh" else "en"
    zh = lang == "zh"
    fields = (
        ("整体视听描述：", "整体音景：", "非叙事配乐：")
        if zh else
        ("integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:")
    )
    marker_re = re.compile(r"\[镜头\s*(\d+)\]" if zh else r"\[Shot\s*(\d+)\]")
    sound_label, music_label = fields[1], fields[2]
    lines = prompt.splitlines()
    body_start = next((i for i, line in enumerate(lines) if line.strip().startswith(fields[0])), None)
    sound_start = next((i for i, line in enumerate(lines) if line.strip().startswith(sound_label)), None)
    music_start = next((i for i, line in enumerate(lines) if line.strip().startswith(music_label)), None)
    field_starts = [
        (body_start, fields[0]), (sound_start, sound_label), (music_start, music_label)
    ]
    field_starts = sorted((pos, label) for pos, label in field_starts if pos is not None)
    field_content: Dict[str, List[str]] = {}
    for field_index, (pos, label) in enumerate(field_starts):
        end = field_starts[field_index + 1][0] if field_index + 1 < len(field_starts) else len(lines)
        values = lines[pos + 1:end]
        inline = lines[pos].split(":", 1)[1].strip() if ":" in lines[pos] else ""
        if inline:
            values.insert(0, inline)
        field_content[label] = values
    body_lines = field_content.get(fields[0], [])
    sound_lines = field_content.get(sound_label, [])
    music_lines = field_content.get(music_label, [])
    body = "\n".join(body_lines).strip()
    matches = list(marker_re.finditer(body))
    blocks: Dict[int, str] = {}
    if matches:
        # Keep any unmarked leading prose with shot 1 rather than discarding it.
        if matches[0].start() > 0:
            blocks[1] = body[:matches[0].start()].strip()
        for idx, match in enumerate(matches):
            number = int(match.group(1))
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
            blocks[number] = body[match.start():end].strip()
    elif body:
        blocks[1] = body

    starts = _starts(cuts)
    repaired: List[str] = []
    shot_blocks: List[str] = []
    for index, cut in enumerate(cuts, 1):
        camera = str(cut.get("camera") or "").strip()
        block = blocks.get(index, "")
        marker = f"[镜头 {index}]" if zh else f"[Shot {index}]"
        if not block:
            block = marker
            fallback = "镜头保持对参考构图的稳定呈现。" if zh else "The camera holds on the referenced composition as the action continues."
            block += " " + fallback
            repaired.append(f"{segment.get('id', '?')}#{index}: 补建缺失的镜头段落")
        elif not block.startswith(marker):
            block = marker + " " + block
        timestamp = _timecode(starts[index - 1])
        if index > 1:
            prefix = f"{marker} 于 {timestamp}，" if zh else f"{marker} At {timestamp},"
            block = re.sub(rf"^\[镜头\s*{index}\]\s*于\s*\d{{2}}:\d{{2}}\.\d{{3}}，?" if zh else rf"^\[Shot\s*{index}\]\s*At\s*\d{{2}}:\d{{2}}\.\d{{3}},?", marker, block)
            block = re.sub(rf"^\[镜头\s*{index}\]\s*" if zh else rf"^\[Shot\s*{index}\]\s*", "", block)
            block = prefix + " " + block.lstrip()
        term = camera if not zh else {
            "Static Shot": "固定", "Push In": "推", "Pull Out": "拉",
            "Zoom In": "变焦推", "Zoom Out": "变焦拉",
            "Pan Left": "左摇", "Pan Right": "右摇",
            "Truck Left": "左移", "Truck Right": "右移",
            "Tilt Up": "仰摇", "Tilt Down": "俯摇",
            "Pedestal Up": "升", "Pedestal Down": "降",
            "Arc Shot": "环绕", "Tracking Shot": "跟拍",
            "Shake Slightly": "轻微晃动", "Shake Strongly": "强烈晃动",
            "POV": "主观视角", "Roll Clockwise": "顺旋",
            "Roll Counterclockwise": "逆旋",
        }.get(camera, camera)
        if term and term.lower() not in block.lower():
            phrase = f"镜头采用{term}。" if zh else f"The camera uses a {term.lower()}."
            block = block.rstrip() + " " + phrase
            repaired.append(f"{segment.get('id', '?')}#{index}: 补齐运镜词「{term}」")
        if scene:
            bounds = cut.get("beats", [])
            if isinstance(bounds, list) and len(bounds) == 2:
                try:
                    claimed = scene.get("flow", [])[int(bounds[0]) - 1:int(bounds[1])]
                except (TypeError, ValueError, IndexError):
                    claimed = []
                for beat in claimed:
                    if not isinstance(beat, dict) or not isinstance(beat.get("line"), str):
                        continue
                    dialogue = beat["line"]
                    exact = f"<d>[Chinese] {dialogue}</d>"
                    if exact not in block and f"<d>[Chinese]{dialogue}</d>" not in block:
                        block = block.rstrip() + " " + exact
                        repaired.append(f"{segment.get('id', '?')}#{index}: 从剧本补入台词 <d> 块")
        shot_blocks.append(block.strip())

    alignment = _alignment(cuts, lang)
    sound = "\n".join(line.strip() for line in sound_lines if line.strip())
    music = "\n".join(line.strip() for line in music_lines if line.strip())
    if not sound:
        sound = "环境声与动作声自然呈现。" if zh else "Natural ambient and action sounds accompany the scene."
        repaired.append(f"{segment.get('id', '?')}: 补齐音景字段")
    if not music:
        music = "无" if zh else "N/A"
        repaired.append(f"{segment.get('id', '?')}: 补齐配乐字段")
    rebuilt = "\n".join([
        alignment, "", fields[0], "\n".join(shot_blocks), "",
        sound_label, sound, "", music_label, music,
    ])
    if rebuilt != prompt.strip():
        segment["h3Prompt"] = rebuilt
        if not repaired:
            repaired.append(f"{segment.get('id', '?')}: 规范化 H3 骨架")
    return repaired


def repair_deterministic_storyboard(doc: Dict[str, Any], lang: str = "en", script: Optional[Dict[str, Any]] = None) -> List[str]:
    """Apply safe, deterministic repairs that do not invent story content."""
    shot_size_terms = {
        "extreme-wide": "大远景",
        "wide": "全景",
        "medium": "中景",
        "close": "特写",
        "extreme-close": "大特写",
    }

    repaired: List[str] = repair_beats_and_symbols(doc)
    repaired += repair_beat_overlaps(doc)
    repaired += repair_short_cuts(doc, lang)
    for segment in _segments(doc):
        for index, cut in enumerate(segment.get("cuts", []), 1):
            size = cut.get("size")
            term = shot_size_terms.get(size)
            frame = str(cut.get("frame") or "")
            if term and frame.strip() and term not in frame:
                cut["frame"] = f"{term}，{frame.lstrip()}"
                repaired.append(f"{segment.get('id', '?')}#{index}: 补齐景别词「{term}」")
            canonical = _camera_for_cut(cut)
            if canonical and canonical != cut.get("camera"):
                repaired.append(f"{segment.get('id', '?')}#{index}: 运镜「{cut.get('camera')}」规范化为「{canonical}」")
                cut["camera"] = canonical
        scene = _script_scene_for_segment(segment, script)
        repaired.extend(_repair_h3_protocol(segment, scene, lang))
        if sync_segment_timing(segment, lang):
            repaired.append(f"{segment.get('id', '?')}: 重建 H3 对齐行与切点时间")
    return repaired


def balance_episode_durations(doc: Dict[str, Any], script: Dict[str, Any],
                              tolerance: float = 1.0, min_cut_seconds: float = 4.0) -> List[str]:
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
