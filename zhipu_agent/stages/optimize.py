"""独立命令：批量优化已保存的出图提示词（角色 / 场景 / 道具），结果写到单独的文件，不改 cast.json / art.json。

    python run_agent.py run --stages optimize --optimize-mode flux|wan|both [--optimize-kinds characters,scenes,props] [--optimize-only 角色名或编号,...]

读 output/characters/*-cast.json（和 output/art/*-art.json），每条记录的 image.prompt / image.sheet 交给 models.json 的 text 模型，
按 flux（skills/flux-image-best-practices）或 wan（skills/video-wan-r2v）的写法重写。输出：
    output/prompts/<书名>-characters-flux.json   （wan 则是 -wan.json；场景 -scenes-、道具 -props-）
每个文件只有重写后的提示词和原提示词对照，想用的话再自己合并进 cast.json。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ..image_prompt_optimizer import MODES, optimize_image
from .base import Context, parallel_map, read_json, write_json, safe_name

KINDS = ("characters", "scenes", "props")
KIND_OF = {"characters": "character", "scenes": "scene", "props": "prop"}
PROP_SCALES = {"手持级": "handheld scale", "桌面级": "tabletop scale", "家具级": "furniture scale"}


def artifact(ctx: Context) -> Path:  # 兼容 workflow 的统一接口；本阶段没有单一产物
    return ctx.out / "prompts"


def check(ctx: Context):  # pragma: no cover - 本阶段永远重新跑
    raise NotImplementedError


def output_path(ctx: Context, kind: str, mode: str) -> Path:
    return ctx.dir("prompts") / f"{safe_name(ctx.title)}-{kind}-{mode}.json"


def _records(ctx: Context, kind: str) -> List[Dict[str, Any]]:
    if kind == "characters":
        path = ctx.artifact("characters")
        key = "characters"
    else:
        path = ctx.artifact("art")
        key = "scenes" if kind == "scenes" else "props"
    if not path.is_file():
        raise FileNotFoundError(f"找不到 {path}：先运行 {'characters' if kind == 'characters' else 'art'} 阶段")
    return list(read_json(path).get(key, []))


def _matches(record: Dict[str, Any], only: Optional[List[str]]) -> bool:
    if not only:
        return True
    wanted = {o.strip().lower() for o in only}
    return any(str(v).strip().lower() in wanted for v in (record.get("id"), record.get("name")))


def run(ctx: Context, mode: str = "flux", kinds: Optional[List[str]] = None,
        only: Optional[List[str]] = None) -> Path:
    modes = list(MODES) if mode == "both" else [mode]
    for m in modes:
        if m not in MODES:
            raise ValueError(f"--optimize-mode 只能是 flux / wan / both，收到 {m!r}")
    kinds = kinds or ["characters"]
    for k in kinds:
        if k not in KINDS:
            raise ValueError(f"--optimize-kinds 只能是 {'/'.join(KINDS)}，收到 {k!r}")

    cast_names: List[str] = []
    cast_path = ctx.artifact("characters")
    if cast_path.is_file():
        for c in read_json(cast_path).get("characters", []):
            cast_names += [c.get("name", ""), *[a for a in c.get("aliases", []) if isinstance(a, str)]]

    last = artifact(ctx)
    for kind in kinds:
        records = [r for r in _records(ctx, kind) if _matches(r, only)]
        if not records:
            ctx.log(f"[optimize] {kind}：没有匹配的记录")
            continue
        for m in modes:
            ctx.log(f"[optimize] {kind} × {m}：{len(records)} 条")

            def one(record: Dict[str, Any]) -> Dict[str, Any]:
                image = record.get("image") or {}
                label = str(record.get("name") or record.get("id"))
                if kind == "characters":
                    forbid = [record.get("name", ""), *[a for a in record.get("aliases", []) if isinstance(a, str)]]
                    keep: List[str] = []
                else:
                    forbid = cast_names
                    keep = [PROP_SCALES.get(record.get("scale"), ""), "white background"] if kind == "props" else []
                new = optimize_image(ctx, image, kind=KIND_OF[kind], label=label, forbid=forbid,
                                     must_keep=keep, mode=m, always=True)
                out = {"id": record.get("id"), "name": record.get("name"), "optimized": new is not image}
                for f in ("prompt", "sheet"):
                    if f in image:
                        out[f] = new.get(f, image[f])
                        out[f"original_{f}"] = image[f]
                return out

            results = parallel_map(one, records, ctx.settings.concurrency)
            items: List[Dict[str, Any]] = []
            for record, res in zip(records, results):
                if isinstance(res, Exception):
                    ctx.log(f"[optimize] {record.get('name')} 失败：{res}")
                    continue
                items.append(res)
            path = output_path(ctx, kind, m)
            write_json(path, {"mode": m, "kind": kind, "title": ctx.title, "items": items})
            done = sum(1 for i in items if i["optimized"])
            ctx.log(f"[optimize] 已写入 {path}（优化 {done}/{len(items)} 条，其余保留原提示词）")
            last = path
    return last
