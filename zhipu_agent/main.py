"""命令行入口。

    python -m zhipu_agent.main doctor [--ping]          检查环境（Python 依赖、Node、技能包、模型配置）
    python -m zhipu_agent.main run  --book input/novel.txt --episodes 6 --minutes 2 --genre 都市
    python -m zhipu_agent.main status                   各阶段产物是否存在、是否通过校验
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ConfigError, REPO_ROOT, load_settings
from .llm import LLMError, ping_all
from .validator import NodeError, StageFailed, check_node
from . import workflow


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zhipu_agent", description="用 Python + AI API 驱动 shuohao-skills 技能包（不需要 Codex）")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="检查运行环境")
    d.add_argument("--ping", action="store_true", help="真的调用一次模型，确认密钥、地址和模型名可用")

    for name, help_text in (("run", "运行流水线"), ("status", "查看各阶段产物状态")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--book", default=str(REPO_ROOT / "input" / "novel.txt"), help="小说文本文件（UTF-8 或 GBK 均可）")
        s.add_argument("--out", default=str(REPO_ROOT / "output"), help="输出目录")
        s.add_argument("--title", help="书名；不给就从正文里的《……》或文件名推断")
        s.add_argument("--lang", default="zh", choices=["zh", "en", "ja"], help="角色报告语言")
        if name == "run":
            s.add_argument("--stages", default="all",
                           help=f"要运行的阶段，逗号分隔；all = {','.join(workflow.DEFAULT_STAGES)}；可选：{','.join(workflow.STAGES)}")
            s.add_argument("--episodes", type=int, help="总集数（outline 阶段必填）")
            s.add_argument("--minutes", type=float, help="单集分钟数（outline 阶段必填）")
            s.add_argument("--genre", help="题材（outline 阶段必填，自由文本，决定爽点类型）")
            s.add_argument("--adapt-mode", default="抽核", help="改编幅度：忠实 / 抽核 / 借壳（默认 抽核）")
            s.add_argument("--preferences", default="", help="想保留的角色或场景等偏好")
            s.add_argument("--eps", help="只处理这些集，如 1-3 或 1,3,5（script / staging / storyboard 阶段）")
            s.add_argument("--aspect", default="16:9", choices=["16:9", "9:16"], help="分镜画幅")
            s.add_argument("--prompt-lang", default="en", choices=["en", "zh"], help="H3 提示词语言（官方口径默认 en）")
            s.add_argument("--max-chars", type=int, default=30, help="没有大纲时，最多给几个角色出角色卡")
            s.add_argument("--images", help="场景设定图目录（staging 认图用，需要配置视觉模型）")
            s.add_argument("--refs-names", help="refs 阶段只处理这些角色，逗号分隔；默认主角和重要配角")
            s.add_argument("--refs-look", default="动漫", help="refs 阶段的画风（默认 动漫）")
            s.add_argument("--refs-gen", action="store_true", help="refs 阶段真的出图（需先配置 character-refs 的出图模型）")
            s.add_argument("--images-gen", action="store_true", help="从已保存的 cast.json / art.json 提示词批量出角色、场景和道具图；需配置 models.json 的 image 号池")
            s.add_argument("--video-protocol", default="h3", choices=["h3", "seedance"], help="视频提示词协议（默认 h3）")
            s.add_argument("--video-submit", action="store_true", help="明确提交云端视频生成任务；不加此参数只生成提示词和清单")
            s.add_argument("--force", action="store_true", help="忽略已有产物和缓存，全部重做")
            s.add_argument("--yes", action="store_true", help="不暂停：骨架拍板点自动确认")
            s.add_argument("--concurrency", type=int, help="同时进行的模型调用数（默认 3）")
            s.add_argument("--fix-rounds", type=int, help="校验不过时最多回炉几轮（默认 3）")
    return p


def _mask(value: str) -> str:
    return "（未设置）" if not value else (value[:4] + "…" + value[-2:] if len(value) > 8 else "已设置")


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = load_settings()
    problems = 0

    def line(ok: Optional[bool], text: str) -> None:
        nonlocal problems
        mark = {True: "✓", False: "✗", None: "·"}[ok]
        if ok is False:
            problems += 1
        print(f"  {mark} {text}")

    print(f"Python {sys.version.split()[0]}")
    try:
        import openai  # noqa: F401
        line(True, f"openai 包 {openai.__version__}")
    except ImportError:
        line(False, "缺少 openai 包：pip install -r requirements.txt")
    try:
        line(True, f"Node {check_node(settings)}")
    except NodeError as exc:
        line(False, str(exc))
    try:
        pack = workflow.load_pack(settings)
        missing = [n for n in ("novel-outline", "novel-characters", "novel-art", "novel-script",
                               "novel-staging", "novel-storyboard", "character-refs") if n not in pack]
        line(not missing, f"技能包 {settings.skills_dir}（{len(pack.names)} 个技能）" + (f"，缺：{missing}" if missing else ""))
    except Exception as exc:  # noqa: BLE001
        line(False, str(exc))
    if settings.models_file:
        line(True, f"模型配置：{settings.models_file}（策略 {settings.strategy}，出错平台冷却 {settings.cooldown:.0f}s）")
    else:
        line(None, "模型配置：环境变量 / .env（单平台）。要配置多个模型或平台，见 models.example.json")
    for kind, title in (("text", "文本模型"), ("vision", "视觉模型")):
        eps = settings.endpoints(kind)
        if not eps:
            if kind == "text":
                line(False, "文本模型：未配置（设置 ZHIPU_API_KEY 和 ZHIPU_MODEL，或创建 models.json）")
            else:
                line(None, "视觉模型：未配置（staging 将只能按提示词推算站位）")
            continue
        for ep in eps:
            line(True, f"{title} {ep.name}：{ep.model} · {ep.base_url} · Key {_mask(ep.api_key)}")
    line(None, f"并发 {settings.concurrency} · 回炉 {settings.fix_rounds} 轮 · 整本直读阈值 {settings.direct_chars} 字")
    if args.ping and settings.text_endpoints():
        for kind, label, ok, note, secs in ping_all(settings):
            line(ok, f"{'文本' if kind == 'text' else '视觉'} {label}：" + (f"可用（{secs:.1f}s）{note!r}" if ok else f"调用失败 {note}"))
    print("环境正常。" if not problems else f"有 {problems} 项需要处理。")
    return 1 if problems else 0


def _ctx_from(args: argparse.Namespace, settings, *, skip_source: bool = False) -> "workflow.Context":
    return workflow.build_context(
        settings, Path(args.book), Path(args.out), title=args.title, lang=args.lang,
        interactive=False if args.command == "status" else (not args.yes and sys.stdin.isatty()),
        force=getattr(args, "force", False),
        images_dir=Path(args.images) if getattr(args, "images", None) else None,
        skip_source=skip_source,
    )


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings()
    ctx = _ctx_from(args, settings)
    for name in [n for n in workflow.STAGES if n not in ("refs",)]:
        module = workflow.MODULES[name]
        path = module.artifact(ctx)
        if not path.is_file():
            print(f"  · {name:<11} 未生成")
            continue
        result = module.check(ctx)
        print(f"  {'✓' if result.ok else '✗'} {name:<11} {path.relative_to(ctx.out)}"
              + ("" if result.ok else f"  （{len(result.violations)} 处违规）"))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    overrides: Dict[str, Any] = {}
    if args.concurrency:
        overrides["concurrency"] = max(1, args.concurrency)
    if args.fix_rounds is not None:
        overrides["fix_rounds"] = max(0, args.fix_rounds)
    settings = load_settings(**overrides)
    stages = workflow.parse_stages(args.stages)
    # Images/video consume saved production JSON and do not need a text-model credential.
    if any(stage not in ("images", "video") for stage in stages):
        settings.require()

    interactive = not args.yes and sys.stdin.isatty()
    params: Dict[str, Any] = {}
    if "outline" in stages:
        episodes, minutes, genre = args.episodes, args.minutes, args.genre
        if interactive:
            if episodes is None:
                episodes = int(input("总集数：").strip())
            if minutes is None:
                minutes = float(input("单集时长（分钟）：").strip())
            if not genre:
                genre = input("题材（决定爽点类型，如 都市 / 悬疑 / 女频打脸）：").strip()
        if not (episodes and minutes and genre):
            raise ValueError("outline 阶段必须给出 --episodes（总集数）、--minutes（单集分钟数）、--genre（题材）")
        params = {
            "episodes": episodes, "minutesPerEpisode": minutes, "genre": genre,
            "adaptMode": args.adapt_mode, "preferences": args.preferences,
        }

    opts: Dict[str, Any] = {
        "params": params, "aspect": args.aspect, "prompt_lang": args.prompt_lang,
        "max_chars": args.max_chars, "refs_look": args.refs_look, "refs_gen": args.refs_gen,
        "images_gen": args.images_gen,
        "video_protocol": args.video_protocol, "video_submit": args.video_submit,
        "refs_names": [n.strip() for n in args.refs_names.split(",")] if args.refs_names else None,
    }
    if args.eps:
        from .stages.base import parse_eps
        opts["eps"] = parse_eps(args.eps)

    ctx = _ctx_from(args, settings, skip_source=(stages == ["images"]))
    results = workflow.run_pipeline(ctx, stages, opts)
    ctx.log("全部完成：")
    for name, path in results.items():
        ctx.log(f"  {name:<11} {path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # Windows 控制台中文不乱码
        except (AttributeError, ValueError):
            pass
    args = _build_parser().parse_args(argv)
    try:
        return {"doctor": cmd_doctor, "run": cmd_run, "status": cmd_status}[args.command](args)
    except KeyboardInterrupt:
        print("\n已中断。已完成的条目都缓存在 output/work/ 里，重新运行会接着做。")
        return 130
    except StageFailed as exc:
        print(f"\n✗ 阶段回炉 {load_settings().fix_rounds} 轮后仍未通过校验，文件已保留，可手工修改后重跑：\n{exc.result.text}")
        return 1
    except (ConfigError, NodeError, LLMError, FileNotFoundError, ValueError) as exc:
        print(f"\n✗ {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
