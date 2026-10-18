"""统一视频中间层：模型技能路由、提示词标准化和能力信息。

中间层不负责 HTTP 请求；具体平台请求仍由 video_adapters 处理。
技能包负责模型侧提示词规则，适配器只负责把标准化结果转换成 API payload。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .skill_loader import SkillError, load_pack


@dataclass(frozen=True)
class VideoSkill:
    adapter: str
    name: str
    prompt_limit: Optional[int] = None


SKILL_REGISTRY = {
    "cogvideox_zhipu": VideoSkill("cogvideox_zhipu", "video-cogvideox3", 512),
    "aliyun_wan": VideoSkill("aliyun_wan", "video-wan-r2v", None),
    "seedance": VideoSkill("seedance", "video-seedance", None),
}


def resolve_skill(adapter: str) -> VideoSkill | None:
    return SKILL_REGISTRY.get(str(adapter or "").strip().lower())


def skill_instructions(adapter: str) -> str:
    """返回模型技能包中的提示词规则，供上游 LLM 做二次优化。"""
    skill = resolve_skill(adapter)
    if skill is None:
        return ""
    try:
        pack = load_pack()
        return pack[skill.name].skill_md.strip()
    except (SkillError, OSError):
        return ""


def optimize_prompt(adapter: str, prompt: str) -> str:
    """执行不依赖 LLM 的模型侧提示词标准化。"""
    text = str(prompt or "").strip()
    skill = resolve_skill(adapter)
    if skill is None:
        return text

    if skill.name == "video-cogvideox3":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines and re.match(r"^(How the reference pictures align|参考图与目标视频的对齐|目标视频在)", lines[0]):
            lines = lines[1:]
        text = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if skill.prompt_limit and len(text) > skill.prompt_limit:
            cut = text[:skill.prompt_limit]
            marks = ("。", "！", "？", ".", "；", "，", ", ")
            boundary = max(cut.rfind(mark) for mark in marks)
            text = cut[:boundary + 1] if boundary >= skill.prompt_limit // 2 else cut
        return text

    if skill.name == "video-wan-r2v":
        return re.sub(r"\s+", " ", text).strip()

    if skill.name == "video-seedance":
        return text.strip()

    return text


def prepare_prompt(provider: dict, prompt: str) -> str:
    """统一入口：根据 provider.adapter 选择对应视频提示词技能。"""
    return optimize_prompt(str(provider.get("adapter") or "generic"), prompt)
