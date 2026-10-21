"""可通过 CLI 选择的视觉提示词风格。"""

VISUAL_STYLES = {
    "manju": (
        "Visual style: polished Chinese manhua short-drama animation, stylized 2D/2.5D illustration, "
        "coherent character design, expressive cinematic composition, clean intentional linework, "
        "illustrative shading and controlled color design. Keep the result clearly illustrated, "
        "not a photograph or live-action footage."
    ),
    "live-action": (
        "Visual style: photorealistic live-action Chinese short-drama cinematography, real human actors, "
        "natural skin texture and age-appropriate pores, realistic hair strands and fabric fibers, "
        "physically plausible lighting, believable locations and tactile props, cinematic lens rendering. "
        "Avoid illustration, anime, cartoon outlines, plastic skin and game-render aesthetics."
    ),
}


def visual_style_instruction(style: str, custom_style: str = "") -> str:
    """Return custom user style when provided; otherwise use the selected preset."""
    custom = str(custom_style or "").strip()
    if custom:
        return custom
    try:
        return VISUAL_STYLES[style]
    except KeyError as exc:
        raise ValueError(
            f"不支持的视觉风格 {style!r}；可选：{', '.join(VISUAL_STYLES)}"
        ) from exc
