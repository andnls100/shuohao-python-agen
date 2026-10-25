"""不同视频模型用不同的提示词优化策略。

策略名决定三件事：用哪套改写规则、怎么校验、结果存在哪（output/video/prompts-optimized/<镜头ID>.<策略名>.txt）。

| 模型（models.json video[].adapter） | 策略名 | 改写规则                                         | 校验                                        |
|-----------------------------------|--------|--------------------------------------------------|---------------------------------------------|
| comfyui（MiniMax H3 / Ref2VA）      | minimax_h3 | skills/video-minimax-h3（学习自 minimax-h3-video-prompt） | <Picture N>、[Shot k]、<d> 台词、7000 字符、时间轴 |
| aliyun_wan                          | aliyun_wan | skills/video-wan-r2v                             | 非空                                        |
| cogvideox_zhipu / seedance / 其他    | 适配器名   | 对应技能包（没有就不改写）                        | 非空                                        |
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from .video_prompt_optimizer import DEFAULT_STRATEGY


def strategy_for(provider: Dict[str, Any]) -> str:
    adapter = str(provider.get("adapter") or "generic").strip().lower()
    return DEFAULT_STRATEGY if adapter == "comfyui" else adapter


def rewrite_fn_for(provider: Dict[str, Any], settings: Any, manifest: Dict[str, Any]) -> Callable[[str], str]:
    """返回 原提示词 -> 优化后提示词。出错抛异常（调用方负责回退）。"""
    strategy = strategy_for(provider)
    if strategy == DEFAULT_STRATEGY:
        from .stages.video import _optimize_h3_prompt  # noqa: PLC0415 - 避免循环导入
        return lambda prompt: _optimize_h3_prompt(prompt, settings)

    from .video_middleware import optimize_prompt_with_skill  # noqa: PLC0415
    style = str((manifest or {}).get("visual_style_instruction") or "").strip()
    return lambda prompt: optimize_prompt_with_skill(strategy, prompt, style_instruction=style) if style else optimize_prompt_with_skill(strategy, prompt)


def next_command(provider: Dict[str, Any], eps: Any = None) -> str:
    key = str(provider.get("key") or provider.get("name") or "").strip()
    eps_arg = f" --eps {','.join(str(e) for e in eps)}" if eps else " --eps 1"
    return f"python run_agent.py run --stages video{eps_arg} --video-model {key} --video-submit"
