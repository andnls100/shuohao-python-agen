"""统一视频中间层：模型技能路由、单镜头提示词优化和能力信息。

中间层不负责 HTTP 请求；具体平台请求仍由 video_adapters 处理。
技能包负责模型侧提示词规则，适配器只负责把标准化结果转换成 API payload。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .llm import chat
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
    """返回模型技能包中的提示词规则。"""
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


def _apply_style_constraint(prompt: str, style_instruction: str) -> str:
    """Append an authoritative style constraint after optimization."""
    text = str(prompt or "").strip()
    style = str(style_instruction or "").strip()
    if not style:
        return text
    marker = "视觉风格硬约束（最高优先级，必须遵守）"
    text = re.sub(r"视觉风格硬约束（最高优先级，必须遵守）：.*$", "", text, flags=re.S).strip()
    return f"{text}\\n\\n{marker}：{style}" if text else f"{marker}：{style}"


def optimize_prompt_with_skill(adapter: str, prompt: str, style_instruction: str = "") -> str:
    """Optimize one shot while treating the selected/user-defined visual style as immutable."""
    text = str(prompt or "").strip()
    style = str(style_instruction or "").strip()
    rules = skill_instructions(adapter)
    if not rules:
        return _apply_style_constraint(optimize_prompt(adapter, text), style)

    if style:
        style_rules = (
            "\\n\\n视觉风格硬约束：\\n"
            + style
            + "\\n这是最高优先级要求。删除或改写与该风格冲突的描述，不得反转风格。"
            "不得额外添加国漫、动漫、插画、卡通、写实摄影等未被指定的风格标签。"
        )
    else:
        style_rules = (
            "\\n\\n视觉风格硬约束：必须保留原提示词中已有的视觉风格及限制，"
            "不得自行新增、替换或反转风格；原提示词未指定风格时，不得创造新风格。"
        )
    system = (
        "你是视频生成提示词优化器。只优化当前一个镜头，不处理其他镜头。\\n"
        "严格遵守下面的模型技能包规则。保留原提示词中的事实、人物、动作、镜头和时序意图，"
        "不要虚构剧情；删除冗余说明；输出只允许是一段可直接提交给视频模型的最终 prompt，"
        "不要解释、不要 Markdown、不要前后引号。\\n"
        "视觉风格是不可擅自修改的硬约束。"
        + style_rules
        + "\\n\\n模型技能包规则：\\n"
        + rules
    )
    try:
        optimized = chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            kind="text",
            temperature=0,
        ).strip()
        print(
            f"\\n===== 视频技能包优化器返回内容（{adapter}，{len(optimized)} 字）=====\\n"
            f"{optimized}\\n"
            "===== 优化器返回内容结束 =====\\n",
            flush=True,
        )
    except Exception:
        optimized = text
    return _apply_style_constraint(optimize_prompt(adapter, optimized), style)


def prepare_prompt(provider: dict, prompt: str) -> str:
    """统一入口：根据 provider.adapter 选择对应视频提示词技能。"""
    return optimize_prompt(str(provider.get("adapter") or "generic"), prompt)
