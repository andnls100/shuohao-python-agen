#!/usr/bin/env python3
"""把流水线产物导出成可训练小模型的 JSONL（只用标准库）。

两种任务：
  prompt      一段分镜 → 最终 H3 Ref2VA 视频提示词（默认；格式固定、有校验器，最适合先训小模型）
  storyboard  一集剧本 → 这一集的分镜（段/镜头 JSON，不含视频提示词）

两种格式：
  messages    {"messages":[{"role":"system"},{"role":"user"},{"role":"assistant"}]}  （LLaMA-Factory / ms-swift / TRL 通用）
  alpaca      {"instruction":..., "input":..., "output":...}

质量过滤（只保留通过的样本）：
  1. 调用技能包的 validate，报错里点名的段（E01-03）整段不要；storyboard 任务整集不要
  2. 本地检查：提示词非空、≤7000 字符、六个章节齐全、没有未替换的 <Picture C02> 占位符、
     <Picture N> 的 N 都在 refOrder 范围内
  3. 输入完全相同的样本去重

用法：
  python scripts/export_training_data.py --root output --out training
  python scripts/export_training_data.py --root novelA/output --root novelB/output --task prompt --format alpaca --valid-ratio 0.05
  python scripts/export_training_data.py --root output --task storyboard --no-validate
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from zhipu_agent.ref_tags import build_ref_order  # noqa: E402
from zhipu_agent.stages.base import STAGE_FILES  # noqa: E402

SECTIONS = ("subject_definitions", "summary", "retention_analysis", "detailed_description",
            "overall_soundscape", "non_diegetic_music")
SEG_ID = re.compile(r"\bE\d{2}-\d{2}\b")
EP_ID = re.compile(r"\bE(\d{2})\b(?!-)")
CUT_DROP = {"assetRefs", "beats", "stability"}

PROMPT_SYSTEM = (
    "You write the final MiniMax H3 Ref2VA video prompt for ONE storyboard segment. "
    "Write exactly six sections in this order, each starting with its name and a colon: "
    "subject_definitions, summary, retention_analysis, detailed_description, overall_soundscape, non_diegetic_music. "
    "Use English except for dialogue, which goes inside <d>...</d> verbatim in its original language. "
    "Reference images are numbered by refOrder: write <Picture N> exactly as numbered there, define each one once in "
    "subject_definitions, and never invent or renumber. Follow the given cuts and timing in order; do not add events. "
    "Keep the prompt under 7000 characters. Return only the prompt."
)
STORYBOARD_SYSTEM = (
    "You turn ONE episode of a short-drama script into storyboard JSON. Return {\"segments\": [...]}; each segment has "
    "id, sceneIndex, cuts, blocking, soundscape, music. Each cut has seconds (4-8), size, camera, lens, cameraPosition, "
    "composition, eyeline, focus, characters, props, frame, shot (and sfx when needed). Keep script order and dialogue; "
    "use only the character and prop ids that appear in the script. Return only JSON."
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- 找文件
def find_storyboards(root: Path) -> List[Path]:
    sub, pattern = STAGE_FILES["storyboard"]
    suffix = pattern.format(t="")
    folder = root / sub
    return sorted(p for p in folder.glob("*" + suffix) if p.is_file()) if folder.is_dir() else []


def sibling(root: Path, stage: str, storyboard: Path) -> Optional[Path]:
    """同一部小说的其他产物（剧本、角色等），标题取自分镜文件名。"""
    sub, pattern = STAGE_FILES["storyboard"]
    suffix = pattern.format(t="")
    title = storyboard.name[: -len(suffix)]
    sub2, pattern2 = STAGE_FILES[stage]
    p = root / sub2 / pattern2.format(t=title)
    return p if p.is_file() else None


# ---------------------------------------------------------------- 校验过滤
def node_validate(storyboard: Path, root: Path) -> Tuple[bool, str]:
    script = REPO / "skills" / "novel-storyboard" / "scripts" / "novel-storyboard.mjs"
    args = [os.environ.get("NODE_BIN", "node"), str(script), "validate", str(storyboard)]
    for flag, stage in (("--script", "script"), ("--outline", "outline"), ("--cast", "characters"), ("--art", "art")):
        p = sibling(root, stage, storyboard)
        if p:
            args += [flag, str(p)]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, "NO_COLOR": "1"}, timeout=600)
    return proc.returncode == 0, (proc.stdout + "\n" + proc.stderr).strip()


def flagged_ids(text: str) -> Tuple[set, set]:
    segs = set(SEG_ID.findall(text))
    eps = {int(m) for m in EP_ID.findall(text)}
    return segs, eps


# ---------------------------------------------------------------- 样本构造
def clean_cut(cut: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in cut.items() if k not in CUT_DROP and v not in (None, "", [])}


def ref_table(segment: Dict[str, Any]) -> List[Dict[str, Any]]:
    order = segment.get("refOrder") or build_ref_order(segment)
    return [{"no": r.get("no"), "kind": r.get("kind"), "name": r.get("name"), "id": r.get("tag") or r.get("id")} for r in order]


def local_problems(prompt: str, refs: Sequence[Dict[str, Any]]) -> List[str]:
    out = []
    if not prompt.strip():
        return ["提示词为空"]
    if len(prompt) > 7000:
        out.append("超过 7000 字符")
    missing = [name for name in SECTIONS if name not in prompt]
    if missing:
        out.append("缺少章节：" + ",".join(missing))
    if re.search(r"<Picture\s+(?!\d+>)", prompt):
        out.append("有未替换的 <Picture 素材ID> 占位符")
    numbers = {int(n) for n in re.findall(r"<Picture\s+(\d+)>", prompt)}
    valid = {int(r["no"]) for r in refs if r.get("no")}
    if numbers and not numbers <= valid:
        out.append(f"引用了 refOrder 之外的编号：{sorted(numbers - valid)}")
    return out


def prompt_sample(segment: Dict[str, Any], system: str) -> Tuple[Dict[str, Any], str, List[str]]:
    refs = ref_table(segment)
    user = {
        "segment": segment.get("id"),
        "refOrder": refs,
        "blocking": segment.get("blocking"),
        "soundscape": segment.get("soundscape"),
        "music": segment.get("music"),
        "cuts": [clean_cut(c) for c in segment.get("cuts") or [] if isinstance(c, dict)],
    }
    user = {k: v for k, v in user.items() if v not in (None, "", [])}
    answer = str(segment.get("videoPrompt") or segment.get("h3Prompt") or "").strip()
    return {"system": system, "input": compact(user), "output": answer}, answer, local_problems(answer, refs)


def storyboard_sample(episode: Dict[str, Any], script_ep: Dict[str, Any], system: str) -> Dict[str, Any]:
    segments = []
    for seg in episode.get("segments") or []:
        segments.append({
            "id": seg.get("id"), "sceneIndex": seg.get("sceneIndex"),
            "cuts": [clean_cut(c) for c in seg.get("cuts") or [] if isinstance(c, dict)],
            "blocking": seg.get("blocking"), "soundscape": seg.get("soundscape"), "music": seg.get("music"),
        })
    segments = [{k: v for k, v in s.items() if v not in (None, "", [])} for s in segments]
    return {"system": system, "input": compact(script_ep), "output": compact({"segments": segments})}


def render(sample: Dict[str, Any], fmt: str) -> Dict[str, Any]:
    if fmt == "alpaca":
        return {"instruction": sample["system"], "input": sample["input"], "output": sample["output"]}
    return {"messages": [{"role": "system", "content": sample["system"]},
                         {"role": "user", "content": sample["input"]},
                         {"role": "assistant", "content": sample["output"]}]}


# ---------------------------------------------------------------- 主流程
def collect(roots: Iterable[Path], task: str, system: str, validate: bool, log) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    stats = {"storyboards": 0, "segments": 0, "kept": 0, "dropped_validator": 0, "dropped_local": 0, "dropped_dup": 0, "dropped_missing_script": 0}
    samples: List[Dict[str, Any]] = []
    seen = set()
    for root in roots:
        for sb in find_storyboards(root):
            stats["storyboards"] += 1
            bad_segs: set = set()
            bad_eps: set = set()
            if validate:
                ok, text = node_validate(sb, root)
                if not ok:
                    bad_segs, bad_eps = flagged_ids(text)
                    log(f"[{sb.name}] 校验未全过，点名的段 {len(bad_segs)} 个、集 {len(bad_eps)} 个将被排除")
            doc = read_json(sb)
            script_doc = None
            if task == "storyboard":
                sp = sibling(root, "script", sb)
                script_doc = read_json(sp) if sp else None
            for episode in doc.get("episodes") or []:
                ep = int(episode.get("ep") or 0)
                if task == "storyboard":
                    script_ep = next((e for e in (script_doc or {}).get("episodes", []) if int(e.get("ep") or 0) == ep), None)
                    if script_ep is None:
                        stats["dropped_missing_script"] += 1
                        continue
                    ep_bad = ep in bad_eps or any(s.get("id") in bad_segs for s in episode.get("segments") or [])
                    if ep_bad:
                        stats["dropped_validator"] += 1
                        continue
                    sample = storyboard_sample(episode, script_ep, system)
                    key = hashlib.sha1(sample["input"].encode("utf-8")).hexdigest()
                    if key in seen:
                        stats["dropped_dup"] += 1
                        continue
                    seen.add(key)
                    samples.append(sample)
                    stats["kept"] += 1
                    continue
                for segment in episode.get("segments") or []:
                    if not isinstance(segment, dict):
                        continue
                    stats["segments"] += 1
                    if segment.get("id") in bad_segs or ep in bad_eps:
                        stats["dropped_validator"] += 1
                        continue
                    sample, _answer, problems = prompt_sample(segment, system)
                    if problems:
                        stats["dropped_local"] += 1
                        log(f"  跳过 {segment.get('id')}：{'；'.join(problems)}")
                        continue
                    key = hashlib.sha1(sample["input"].encode("utf-8")).hexdigest()
                    if key in seen:
                        stats["dropped_dup"] += 1
                        continue
                    seen.add(key)
                    samples.append(sample)
                    stats["kept"] += 1
    return samples, stats


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="把分镜产物导出为小模型训练用 JSONL")
    ap.add_argument("--root", action="append", required=True, help="output 目录（可重复，多部小说各一个）")
    ap.add_argument("--out", default="training", help="输出目录（默认 training）")
    ap.add_argument("--task", choices=("prompt", "storyboard"), default="prompt")
    ap.add_argument("--format", choices=("messages", "alpaca"), default="messages")
    ap.add_argument("--valid-ratio", type=float, default=0.0, help="验证集比例，如 0.05；默认不切分")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-validate", action="store_true", help="不调用技能包校验（只做本地检查）")
    ap.add_argument("--system-file", help="用这个文件的内容当系统提示（例如 skills/h3-prompt-writing/SKILL.md）；默认用内置的简短指令")
    args = ap.parse_args(argv)

    system = Path(args.system_file).read_text(encoding="utf-8") if args.system_file else (
        PROMPT_SYSTEM if args.task == "prompt" else STORYBOARD_SYSTEM)
    roots = [Path(r) for r in args.root]
    samples, stats = collect(roots, args.task, system, not args.no_validate, print)
    if not samples:
        print("没有可导出的样本：" + json.dumps(stats, ensure_ascii=False))
        return 1
    random.Random(args.seed).shuffle(samples)
    n_valid = int(len(samples) * args.valid_ratio) if args.valid_ratio > 0 else 0
    parts = {"train": samples[n_valid:]}
    if n_valid:
        parts["valid"] = samples[:n_valid]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in parts.items():
        path = out / f"{args.task}.{args.format}.{name}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(render(row, args.format), ensure_ascii=False) + "\n")
        print(f"已写出 {len(rows)} 条 → {path}")
    print("统计：" + json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
