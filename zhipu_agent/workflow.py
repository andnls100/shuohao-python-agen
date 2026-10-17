"""流水线编排：按技能包的接力顺序依次执行各阶段。

    小说 → outline → characters → art → images（可选，复用提示词批量出图） → script → staging → storyboard
                                  ↘ refs（可选，角色锚点与派生参考图）

每个阶段是 stages/ 下的一个模块，暴露 run / artifact / check。
已经完成且通过校验的阶段会被跳过（--force 重做）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Settings
from .skill_loader import load_pack
from .stages import art, audio, characters, continuity, images, outline, refs, script, staging, storyboard, video
from .stages.base import (
    Context, guess_title, read_text_auto, safe_name,
)
from .validator import check_node

STAGES = ["outline", "characters", "art", "images", "script", "audio", "staging", "storyboard", "refs", "continuity", "video"]
# 生图、角色参考图和视频调用都可能产生额外费用，默认流水线不自动触发。
DEFAULT_STAGES = [name for name in STAGES if name not in ("images", "refs", "video")]
MODULES = {
    "outline": outline, "characters": characters, "art": art, "images": images, "script": script, "audio": audio,
    "staging": staging, "storyboard": storyboard, "refs": refs, "continuity": continuity, "video": video,
}
# 这些阶段内部按集续跑，指定了 --eps 时不能整体跳过
PARTIAL_OK = ("script", "audio", "staging", "storyboard", "video")
REQUIRES = {
    "characters": [],            # 没有大纲也能跑
    "art": ["outline"],
    "images": ["characters", "art"],
    "script": ["outline"],
    "audio": ["characters", "script"],
    "staging": ["script", "art"],
    "storyboard": ["script"],
    "refs": ["characters"],
    "continuity": ["characters", "art", "storyboard"],
    "video": ["storyboard"],
}


def parse_stages(spec: str) -> List[str]:
    if spec in ("all", "全部"):
        return list(DEFAULT_STAGES)
    names = [s.strip() for s in spec.split(",") if s.strip()]
    unknown = [n for n in names if n not in STAGES]
    if unknown:
        raise ValueError(f"未知阶段 {unknown}。可选：{', '.join(STAGES)}")
    return sorted(set(names), key=STAGES.index)


def prepare_source(book: Path, out: Path, title: Optional[str]) -> Tuple[Path, str]:
    """把原文统一转成 UTF-8（中文网文常是 GB18030），存到 <out>/source/ 下。"""
    book = Path(book)
    if not book.is_file():
        raise FileNotFoundError(f"找不到小说文件：{book}")
    text = read_text_auto(book)
    final_title = title or guess_title(text, book.stem)
    target = Path(out) / "source" / f"{safe_name(final_title)}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, final_title


def build_context(
    settings: Settings,
    book: Path,
    out: Path,
    *,
    title: Optional[str] = None,
    lang: str = "zh",
    interactive: bool = True,
    force: bool = False,
    images_dir: Optional[Path] = None,
    skip_source: bool = False,
) -> Context:
    version = check_node(settings)
    pack = load_pack(settings)
    if skip_source:
        out = Path(out)
        final_title = title
        if not final_title:
            candidates = set()
            for folder, suffix in ((out / "characters", "-cast.json"), (out / "art", "-art.json")):
                if folder.is_dir():
                    for candidate in folder.glob("*" + suffix):
                        candidates.add(candidate.name[:-len(suffix)])
            if len(candidates) == 1:
                final_title = next(iter(candidates))
        if not final_title:
            raise ValueError("只运行 images 阶段且不读取小说时，请用 --title 指定书名，或确保 output 中只有一套 cast/art JSON")
        # The images stage only consumes cast.json and art.json. Do not read or copy the novel.
        book_file = out / "source" / f"{safe_name(final_title)}.txt"
        source_chars = None
    else:
        book_file, final_title = prepare_source(book, out, title)
        source_chars = len(book_file.read_text(encoding="utf-8"))
    ctx = Context(
        settings=settings, pack=pack, out=Path(out), book_file=book_file, title=final_title,
        lang=lang, interactive=interactive, force=force,
        images_dir=Path(images_dir) if images_dir else None,
    )
    if source_chars is None:
        ctx.log(f"《{final_title}》 · 使用已有 JSON 资产（跳过小说读取） · Node {version} · 输出到 {ctx.out}")
    else:
        ctx.log(f"《{final_title}》 · 原文 {source_chars} 字 · Node {version} · 输出到 {ctx.out}")
    return ctx


def run_stage(ctx: Context, name: str, opts: Dict[str, Any]) -> Optional[Path]:
    module = MODULES[name]
    eps = opts.get("eps")

    for need in REQUIRES.get(name, []):
        if not ctx.artifact(need).is_file() and need not in opts["stages"]:
            raise FileNotFoundError(
                f"阶段 {name} 需要先有 {need} 的产物：{ctx.artifact(need)}。"
                f"请把 {need} 加进 --stages，或先单独运行它。"
            )

    if name not in ("refs", "continuity") and not ctx.force and module.artifact(ctx).is_file() \
            and not (eps and name in PARTIAL_OK):
        result = module.check(ctx)
        if result.ok:
            ctx.log(f"▶ {name}：已有产物且通过校验，跳过（--force 重做）")
            return module.artifact(ctx)
        ctx.log(f"▶ {name}：已有产物但未通过校验，继续补全")

    ctx.log(f"▶ {name}")
    if name == "outline":
        return module.run(ctx, opts["params"])
    if name == "characters":
        return module.run(ctx, max_chars=opts.get("max_chars", 30),
                          visual_style=opts.get("visual_style", "manju"))
    if name == "art":
        return module.run(ctx, visual_style=opts.get("visual_style", "manju"))
    if name in ("script", "staging"):
        return module.run(ctx, eps=eps)
    if name == "audio":
        return module.run(ctx, eps=eps, submit=opts.get("audio_submit", False))
    if name == "storyboard":
        return module.run(ctx, eps=eps, aspect=opts.get("aspect", "16:9"),
                          prompt_lang=opts.get("prompt_lang", "en"),
                          visual_style=opts.get("visual_style", "manju"))
    if name == "images":
        return module.run(ctx, generate=opts.get("images_gen", False), only=opts.get("images_only"))
    if name == "refs":
        return module.run(ctx, names=opts.get("refs_names"), look=opts.get("refs_look", "动漫"),
                          gen=opts.get("refs_gen", False))
    if name == "continuity":
        return module.run(ctx)
    if name == "video":
        return module.run(ctx, eps=eps, protocol=opts.get("video_protocol", "h3"),
                          submit=opts.get("video_submit", False),
                          visual_style=opts.get("visual_style", "manju"),
                          shots=opts.get("video_shots"),
                          video_model=opts.get("video_model"))
    raise ValueError(name)


def run_pipeline(ctx: Context, stages: List[str], opts: Dict[str, Any]) -> Dict[str, Path]:
    opts = {**opts, "stages": stages}
    results: Dict[str, Path] = {}
    for name in stages:
        path = run_stage(ctx, name, opts)
        if path:
            results[name] = path
    return results
