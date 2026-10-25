"""命令行入口。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ConfigError, REPO_ROOT, load_settings
from .llm import LLMError, ping_all
from .validator import NodeError, StageFailed, check_node
from . import workflow


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zhipu_agent", description="用 Python + AI API 驱动 shuohao-skills 技能包")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="检查运行环境")
    d.add_argument("--ping", action="store_true", help="真的调用一次模型，确认密钥、地址和模型名可用")
    for name, help_text in (("run", "运行流水线"), ("status", "查看各阶段产物状态")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--book", default=str(REPO_ROOT / "input" / "novel.txt"), help="小说文本文件")
        s.add_argument("--out", default=str(REPO_ROOT / "output"), help="输出目录")
        s.add_argument("--title", help="书名")
        s.add_argument("--lang", default="zh", choices=["zh", "en", "ja"], help="角色报告语言")
        if name == "run":
            s.add_argument("--stages", default="all", help=f"阶段，逗号分隔；all = {','.join(workflow.DEFAULT_STAGES)}；可选：{','.join(workflow.STAGES)}")
            s.add_argument("--episodes", type=int, help="总集数")
            s.add_argument("--minutes", type=float, help="单集分钟数")
            s.add_argument("--genre", help="题材")
            s.add_argument("--adapt-mode", default="抽核", help="改编幅度")
            s.add_argument("--preferences", default="", help="偏好")
            s.add_argument("--eps", help="只处理这些集，如 1-3 或 1,3,5")
            s.add_argument("--aspect", default="16:9", choices=["16:9", "9:16"], help="分镜画幅")
            s.add_argument("--prompt-lang", default="en", choices=["en", "zh"], help="H3 提示词语言")
            s.add_argument("--fight-enhancement", default="auto", choices=["off", "auto", "enhanced"], help="打戏增强")
            s.add_argument("--visual-style", default="live-action", choices=["manju", "live-action"], help="视觉提示词风格")
            s.add_argument("--visual-style-prompt", default="", help="自定义视觉风格描述；优先于 --visual-style，并作为视频提示词硬约束")
            s.add_argument("--max-chars", type=int, default=30, help="最多角色数")
            s.add_argument("--images", help="场景设定图目录")
            s.add_argument("--refs-names", help="refs 阶段只处理这些角色，逗号分隔")
            s.add_argument("--refs-look", help="refs 阶段画风")
            s.add_argument("--refs-gen", action="store_true", help="refs 阶段真的出图")
            s.add_argument("--optimize-mode", default="flux", choices=["flux", "wan", "both"])
            s.add_argument("--optimize-kinds", default="characters")
            s.add_argument("--optimize-only")
            s.add_argument("--images-gen", action="store_true", help="批量出角色、场景和道具图")
            s.add_argument("--images-only", help="只出指定图片")
            s.add_argument("--frames-gen", action="store_true", help="真的生成静态分镜图")
            s.add_argument("--frames-only", help="只出指定分镜图")
            s.add_argument("--video-protocol", default="h3", choices=["h3", "seedance"], help="视频提示词协议；视频输入由连续性规则决定")
            s.add_argument("--video-submit", action="store_true", help="提交云端视频任务；不加只生成清单")
            s.add_argument("--video-model", help="选择 models.json 中 video[].key")
            s.add_argument("--video-optimize", action="store_true", help="先把生视频提示词优化成英文（保留全部 <Picture N>），结果存 output/video/prompts-optimized/；之后 --video-submit 自动使用")
            s.add_argument("--video-optimize-force", action="store_true", help="重新优化，忽略已有的优化结果")
            s.add_argument("--video-size", help="ComfyUI 视频清晰度，例如 480p；不指定时沿用 models.json 的 size_preset（默认 480p；720p = 360p 生成 + 2 倍放大）")
            s.add_argument("--video-merge-seconds", type=float, default=10, help="同段内连续镜头合并为一次视频调用，总时长上限秒数（默认 10；0 = 不合并，逐镜头调用）")
            s.add_argument("--segments", help="只处理指定分段 ID，逗号分隔，例如 E01-01,E01-02；seedance/i2v 下会展开为该段全部镜头")
            s.add_argument("--shots", help="只处理指定分镜头 ID，逗号分隔，例如 E01-01-C01,E01-01-C02")
            s.add_argument("--audio-submit", action="store_true", help="调用 TTS API")
            s.add_argument("--force", action="store_true", help="忽略已有产物和缓存")
            s.add_argument("--yes", action="store_true", help="不暂停")
            s.add_argument("--concurrency", type=int, help="并发数")
            s.add_argument("--fix-rounds", type=int, help="回炉轮数")
    op = sub.add_parser("optimize-prompts", help="优化一集所有提示词")
    op.add_argument("--out", default=str(REPO_ROOT / "output"))
    op.add_argument("--episode", "--eps", dest="episode", type=int, required=True)
    op.add_argument("--input")
    op.add_argument("--output-file")
    return p


def _mask(value: str) -> str:
    return "（未设置）" if not value else (value[:4] + "…" + value[-2:] if len(value) > 8 else "已设置")


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = load_settings()
    problems = 0
    def line(ok: Optional[bool], text: str) -> None:
        nonlocal problems
        if ok is False:
            problems += 1
        mark = {True: "✓", False: "✗", None: "·"}[ok]
        print(f"  {mark} {text}")
    print(f"Python {sys.version.split()[0]}")
    try:
        import openai
        line(True, f"openai 包 {openai.__version__}")
    except ImportError:
        line(False, "缺少 openai 包：pip install -r requirements.txt")
    try:
        line(True, f"Node {check_node(settings)}")
    except NodeError as exc:
        line(False, str(exc))
    try:
        pack = workflow.load_pack(settings)
        missing = [n for n in ("novel-outline", "novel-characters", "novel-art", "novel-script", "novel-staging", "novel-storyboard", "character-refs") if n not in pack]
        line(not missing, f"技能包 {settings.skills_dir}（{len(pack.names)} 个技能）" + (f"，缺：{missing}" if missing else ""))
    except Exception as exc:
        line(False, str(exc))
    if settings.models_file:
        line(True, f"模型配置：{settings.models_file}（策略 {settings.strategy}）")
    else:
        line(None, "模型配置：环境变量 / .env")
    for kind, title in (("text", "文本模型"), ("vision", "视觉模型")):
        eps = settings.endpoints(kind)
        if not eps:
            line(False if kind == "text" else None, f"{title}：未配置")
            continue
        for ep in eps:
            line(True, f"{title} {ep.name}：{ep.model} · {ep.base_url} · Key {_mask(ep.api_key)}")
    line(None, f"并发 {settings.concurrency} · 回炉 {settings.fix_rounds} 轮")
    if args.ping and settings.text_endpoints():
        for kind, label, ok, note, secs in ping_all(settings):
            line(ok, f"{'文本' if kind == 'text' else '视觉'} {label}：" + (f"可用（{secs:.1f}s）{note!r}" if ok else f"调用失败 {note}"))
    print("环境正常。" if not problems else f"有 {problems} 项需要处理。")
    return 1 if problems else 0


def _ctx_from(args: argparse.Namespace, settings, *, skip_source: bool = False) -> "workflow.Context":
    return workflow.build_context(settings, Path(args.book), Path(args.out), title=args.title, lang=args.lang,
                                  interactive=False if args.command == "status" else (not args.yes and sys.stdin.isatty()),
                                  force=getattr(args, "force", False),
                                  images_dir=Path(args.images) if getattr(args, "images", None) else None,
                                  skip_source=skip_source)


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings()
    ctx = _ctx_from(args, settings)
    for name in [n for n in workflow.STAGES if n != "refs"]:
        module = workflow.MODULES[name]
        path = module.artifact(ctx)
        if not path.is_file():
            print(f"  · {name:<11} 未生成")
            continue
        result = module.check(ctx)
        print(f"  {'✓' if result.ok else '✗'} {name:<11} {path.relative_to(ctx.out)}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    overrides: Dict[str, Any] = {}
    if args.concurrency:
        overrides["concurrency"] = max(1, args.concurrency)
    if args.fix_rounds is not None:
        overrides["fix_rounds"] = max(0, args.fix_rounds)
    settings = load_settings(**overrides)
    stages = workflow.parse_stages(args.stages)
    if any(stage not in ("images", "frames", "video", "audio") for stage in stages):
        settings.require()
    params: Dict[str, Any] = {}
    if "outline" in stages:
        if not (args.episodes and args.minutes and args.genre):
            raise ValueError("outline 阶段必须给出 --episodes、--minutes、--genre")
        params = {"episodes": args.episodes, "minutesPerEpisode": args.minutes, "genre": args.genre,
                  "adaptMode": args.adapt_mode, "preferences": args.preferences}
    opts: Dict[str, Any] = {
        "params": params, "aspect": args.aspect, "prompt_lang": args.prompt_lang,
        "fight_enhancement": args.fight_enhancement, "visual_style": args.visual_style,
        "visual_style_prompt": args.visual_style_prompt.strip(),
        "max_chars": args.max_chars, "refs_look": args.refs_look or ("写实" if args.visual_style == "live-action" else "动漫"),
        "refs_gen": args.refs_gen, "optimize_mode": args.optimize_mode,
        "optimize_kinds": [k.strip() for k in args.optimize_kinds.split(",") if k.strip()],
        "optimize_only": [n.strip() for n in args.optimize_only.split(",") if n.strip()] if args.optimize_only else None,
        "images_gen": args.images_gen,
        "images_only": [n.strip() for n in args.images_only.split(",") if n.strip()] if args.images_only else None,
        "video_protocol": args.video_protocol, "video_submit": args.video_submit, "video_model": args.video_model,
        "frames_gen": args.frames_gen,
        "frames_only": [n.strip() for n in args.frames_only.split(",") if n.strip()] if args.frames_only else None,
        "video_merge_seconds": args.video_merge_seconds, "video_size": args.video_size,
        "video_optimize": args.video_optimize, "video_optimize_force": args.video_optimize_force,
        "video_segments": [n.strip() for n in args.segments.split(",") if n.strip()] if args.segments else None,
        "video_shots": [n.strip() for n in args.shots.split(",") if n.strip()] if args.shots else None,
        "audio_submit": args.audio_submit,
        "refs_names": [n.strip() for n in args.refs_names.split(",") if n.strip()] if args.refs_names else None,
    }
    if args.eps:
        from .stages.base import parse_eps
        opts["eps"] = parse_eps(args.eps)
    ctx = _ctx_from(args, settings, skip_source=all(stage in ("optimize", "images", "frames", "continuity", "video") for stage in stages))
    results = workflow.run_pipeline(ctx, stages, opts)
    ctx.log("全部完成：")
    for name, path in results.items():
        ctx.log(f"  {name:<11} {path}")
    return 0


def cmd_optimize_prompts(args: argparse.Namespace) -> int:
    from .prompt_optimizer import optimize_episode
    settings = load_settings()
    settings.require()
    optimize_episode(episode=args.episode, out_dir=Path(args.out),
                      input_file=Path(args.input) if args.input else None,
                      output_file=Path(args.output_file) if args.output_file else None)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = _build_parser().parse_args(argv)
    try:
        return {"doctor": cmd_doctor, "run": cmd_run, "status": cmd_status, "optimize-prompts": cmd_optimize_prompts}[args.command](args)
    except KeyboardInterrupt:
        print("\n已中断。已完成的条目保留在 output/，重新运行可继续。")
        return 130
    except StageFailed as exc:
        print(f"\n✗ 阶段回炉后仍未通过校验：\n{exc.result.text}")
        return 1
    except (ConfigError, NodeError, LLMError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"\n✗ {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
