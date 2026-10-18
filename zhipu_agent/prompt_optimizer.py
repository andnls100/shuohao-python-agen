"""独立优化单集分镜提示词；只调用文本模型，不生成图片或视频。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Optional

from .config import load_settings
from .llm import Conversation
from .skill_loader import build_system, load_pack
from .stages.base import read_json, safe_name, write_json


PROMPT_FIELDS = ("frame", "shot")
SEGMENT_PROMPT_FIELDS = ("h3Prompt", "blocking", "soundscape", "music")


def find_storyboard(out_dir: Path) -> Path:
    candidates = sorted((Path(out_dir) / "storyboard").glob("*-storyboard.json"))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FileNotFoundError(
            f"未找到分镜文件：{Path(out_dir) / 'storyboard'}/*-storyboard.json；"
            "请先生成分镜，或用 --input 指定 JSON 文件。"
        )
    raise ValueError(
        "找到多份分镜文件，请用 --input 明确指定："
        + ", ".join(str(p) for p in candidates)
    )


def optimize_episode(
    *,
    episode: int,
    out_dir: Path,
    input_file: Optional[Path] = None,
    output_file: Optional[Path] = None,
) -> Path:
    settings = load_settings()
    settings.require()
    pack = load_pack(settings)
    skill = pack["novel-storyboard"]

    source_path = Path(input_file) if input_file else find_storyboard(Path(out_dir))
    if not source_path.is_file():
        raise FileNotFoundError(f"分镜文件不存在：{source_path}")
    source = read_json(source_path)
    episodes = source.get("episodes")
    if not isinstance(episodes, list):
        raise ValueError(f"分镜 JSON 缺少 episodes 数组：{source_path}")
    selected = next((ep for ep in episodes if isinstance(ep, dict) and ep.get("ep") == episode), None)
    if selected is None:
        available = [str(ep.get("ep")) for ep in episodes if isinstance(ep, dict) and "ep" in ep]
        raise ValueError(f"文件中没有第 {episode} 集；现有集号：{', '.join(available) or '无'}")
    if not isinstance(selected.get("segments"), list) or not selected["segments"]:
        raise ValueError(f"第 {episode} 集没有 segments 分镜数据")

    target = Path(output_file) if output_file else (
        Path(out_dir) / "storyboard" /
        f"{safe_name(str(source.get('source') or source_path.stem))}-E{episode:02d}-optimized-prompts.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    # 使用技能包原有规范作为优化依据，不覆盖源文件。
    system = build_system(
        skill,
        ["shot-writing.md", "h3-prompt.md", "frame.md", "fight-enhancement.md"],
        schema=True,
        extra=(
            "## 本次任务：优化现有分镜提示词，不重新创作分镜\n"
            "输入是一段已经存在的 segment。只优化文字提示词，让动作更可执行、画面构图更明确、"
            "角色/场景/道具连续性更强，声音与画面动作一致。\n"
            "硬性不变量：段 id、sceneIndex、cuts 数量和顺序、每个 cut 的 beats/seconds/size/camera/"
            "characters/props 及所有非提示词字段不得改变；不得增加或删除角色、道具、动作、对白或剧情事实。"
            "cut.frame 仍用中文并直呼角色名；cut.shot 按技能规则使用通用身份、不写角色名。"
            "所有台词必须逐字保留。H3 提示词必须与原 cuts 数量、顺序、时长、切点、运镜和台词严格一致，"
            "不能改时间戳或协议骨架。\n"
            "只返回 JSON 对象，至少包含 h3Prompt、cuts；cuts 中每项只需返回 frame 和 shot。"
            "可以返回 blocking、soundscape、music；不得返回解释或 Markdown。"
        ),
    )

    result_doc: Dict[str, Any] = {
        key: copy.deepcopy(value)
        for key, value in source.items()
        if key != "episodes"
    }
    result_doc["episodes"] = [copy.deepcopy(selected)]
    # 先写原始提示词副本，单段成功后即保存；失败时源文件不受影响，结果文件可续查。
    write_json(target, result_doc)

    optimized_episode = result_doc["episodes"][0]
    segments = optimized_episode["segments"]
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError(f"第 {episode} 集第 {index + 1} 段结构无效")
        cuts = segment.get("cuts")
        if not isinstance(cuts, list) or not cuts:
            raise ValueError(f"{segment.get('id', index + 1)} 没有 cuts")
        prompt = {
            "task": "优化这个 segment 的提示词文本，严格保留结构化分镜信息",
            "promptLang": source.get("promptLang", "en"),
            "aspectRatio": source.get("aspectRatio", "16:9"),
            "segment": segment,
        }
        response = Conversation(system, settings).ask_json(
            json.dumps(prompt, ensure_ascii=False, indent=2)
        )
        if not isinstance(response, dict):
            raise ValueError(f"{segment.get('id')} 返回结果不是 JSON 对象")
        returned_cuts = response.get("cuts")
        if not isinstance(returned_cuts, list) or len(returned_cuts) != len(cuts):
            raise ValueError(
                f"{segment.get('id')} 返回 cuts 数量不一致："
                f"期望 {len(cuts)}，实际 {len(returned_cuts) if isinstance(returned_cuts, list) else '无'}"
            )

        # 只接收允许优化的文字字段，结构化时间/镜头信息始终以源文件为准。
        for field in SEGMENT_PROMPT_FIELDS:
            value = response.get(field)
            if isinstance(value, str) and value.strip():
                segment[field] = value
        for cut_index, (original_cut, returned_cut) in enumerate(zip(cuts, returned_cuts), start=1):
            if not isinstance(returned_cut, dict):
                raise ValueError(f"{segment.get('id')} 第 {cut_index} 切返回结构无效")
            for field in PROMPT_FIELDS:
                value = returned_cut.get(field)
                if isinstance(value, str) and value.strip():
                    original_cut[field] = value
        write_json(target, result_doc)
        print(f"[prompt-optimizer] 已优化 {segment.get('id')}（{index + 1}/{len(segments)}）")

    print(f"[prompt-optimizer] 输出：{target}")
    print("[prompt-optimizer] 未生成图片或视频；原分镜文件未修改。")
    return target
