"""流水线编排：按技能包的接力顺序依次执行各阶段。

    小说 → outline → characters → art → script → staging → storyboard
                                  ↘ refs（可选，真出角色参考图）

每个阶段是 stages/ 下的一个模块，暴露 run / artifact / check。
已经完成且通过校验的阶段会被跳过（--force 重做）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Settings
from .skill_loader import load_pack
from .stages import art, characters, outline, refs, script, staging, storyboard
from .stages.base import (
    Context, guess_title, read_text_auto, safe_name,
)
from .validator import check_node

STAGES = ["outline", "characters", "art", "script", "staging", "storyboard", "refs"]
DEFAULT_STAGES = STAGES[:-1]  # refs 要出图，默认不跑
MODULES = {
    "outline": outline, "characters": characters, "art": art, "script": script,
    "staging": staging, "storyboard": storyboard, "refs": refs,
}
# 这些阶段内部按集续跑，指定了 --eps 时不能整体跳过
PARTIAL_OK = ("script", "staging", "storyboard")
REQUIRES = {
    "characters": [],            # 没有大纲也能跑
    "art": ["outline"],
    "script": ["outline"],
    "staging": ["script", "art"],
    "storyboard": ["script"],
    "refs": ["characters"],
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
) -> Context:
    version = check_node(settings)
    pack = load_pack(settings)
    book_file, final_title = prepare_source(book, out, title)
    ctx = Context(
        settings=settings, pack=pack, out=Path(out), book_file=book_file, title=final_title,
        lang=lang, interactive=interactive, force=force,
        images_dir=Path(images_dir) if images_dir else None,
    )
    ctx.log(f"《{final_title}》 · 原文 {len(book_file.read_text(encoding='utf-8'))} 字 · Node {version} · 输出到 {ctx.out}")
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

    if name != "refs" and not ctx.force and module.artifact(ctx).is_file() \
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
        return module.run(ctx, max_chars=opts.get("max_chars", 30))
    if name == "art":
        return module.run(ctx)
    if name in ("script", "staging"):
        return module.run(ctx, eps=eps)
    if name == "storyboard":
        return module.run(ctx, eps=eps, aspect=opts.get("aspect", "16:9"),
                          prompt_lang=opts.get("prompt_lang", "en"))
    if name == "refs":
        return module.run(ctx, names=opts.get("refs_names"), look=opts.get("refs_look", "动漫"),
                          gen=opts.get("refs_gen", False))
    raise ValueError(name)


def run_pipeline(ctx: Context, stages: List[str], opts: Dict[str, Any]) -> Dict[str, Path]:
    opts = {**opts, "stages": stages}
    results: Dict[str, Path] = {}
    for name in stages:
        path = run_stage(ctx, name, opts)
        if path:
            results[name] = path
    return results
