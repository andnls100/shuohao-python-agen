"""ComfyUI 生视频提示词优化：把每个镜头的 h3Prompt 改写成英文，同时保证 <Picture N>、[Shot k]、<d> 台词块一个都不丢。

流程：
  python run_agent.py run --stages video --eps 1 --video-model comfyui_h3 --video-optimize        # 只优化，不提交
  python run_agent.py run --stages video --eps 1 --video-model comfyui_h3 --video-submit          # 有优化结果就直接用

结果存在 output/video/prompts-optimized/：每个镜头一个 <镜头ID>.txt，加一个 index.json 记录原提示词哈希。
提交时只有「哈希对得上」才使用优化版；分镜改过、原提示词变了，旧的优化结果自动作废并回退到原提示词。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

DIR_NAME = "prompts-optimized"
INDEX_NAME = "index.json"

_PICTURE = re.compile(r"<\s*Picture\s*(\d+)\s*>", re.I)
_SHOT = re.compile(r"\[\s*(?:Shot|镜头)\s*(\d+)\s*\]", re.I)
_DIALOGUE = re.compile(r"<d>.*?</d>", re.S | re.I)
_CJK = re.compile(r"[㐀-鿿]")

SYSTEM = (
    "You are a video-generation prompt editor. Rewrite ONE shot prompt into fluent, concrete, natural English "
    "that a text-to-video model can follow, keeping every fact, character, action, camera move, timing and sound cue.\n"
    "Hard rules (output is machine-checked and rejected if any is broken):\n"
    "1. Every <Picture N> tag is a reference-image index. Keep EVERY tag exactly as written (same N, same angle brackets, "
    "same count). Never renumber, merge, translate, rewrite or drop one. Do not add new ones.\n"
    "2. Every [Shot k] / [镜头 k] marker stays, in the same order; write them as [Shot k]. Keep the time stamps (e.g. 00:03.000) unchanged.\n"
    "3. Copy every <d>...</d> dialogue block character for character, including its [Language] tag. Do not translate or edit dialogue.\n"
    "4. Everything outside <d> blocks must be English (no Chinese characters).\n"
    "5. Do not invent plot, characters or objects. Do not add style labels. Keep the section structure "
    "(alignment line, shot paragraphs, overall soundscape, non-diegetic music) if present.\n"
    "6. No explanations, no Markdown, no surrounding quotes. Output only the final prompt."
)


def prompt_hash(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()


def _safe(value: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", str(value)).strip("_") or "shot"


def validate(original: str, optimized: str) -> List[str]:
    """返回问题列表；空列表表示优化结果合格。"""
    problems: List[str] = []
    if not optimized.strip():
        return ["优化结果为空"]
    want, got = Counter(_PICTURE.findall(original)), Counter(_PICTURE.findall(optimized))
    for n in sorted(want, key=int):
        if got[n] < want[n]:
            problems.append(f"<Picture {n}> 少了（原文 {want[n]} 处，优化后 {got[n]} 处）")
    for n in sorted(set(got) - set(want), key=int):
        problems.append(f"多出了原文没有的 <Picture {n}>")
    if _SHOT.findall(original) != _SHOT.findall(optimized):
        problems.append(f"[Shot k] 标记不一致：原文 {_SHOT.findall(original)}，优化后 {_SHOT.findall(optimized)}")
    if [d.strip() for d in _DIALOGUE.findall(original)] != [d.strip() for d in _DIALOGUE.findall(optimized)]:
        problems.append("<d> 台词块和原文不逐字一致")
    outside = _DIALOGUE.sub("", optimized)
    if _CJK.search(outside):
        sample = "".join(_CJK.findall(outside)[:12])
        problems.append(f"<d> 之外仍有中文：{sample}")
    return problems


def optimize_one(original: str, chat_fn: Optional[Callable[..., str]] = None, retries: int = 1,
                 rewrite_fn: Optional[Callable[[str], str]] = None) -> Tuple[str, List[str]]:
    """返回 (提示词, 问题)。问题为空 = 优化版合格；否则返回原提示词，问题说明为什么没用优化版。

    rewrite_fn(原提示词) -> 英文提示词：由调用方提供改写实现（视频阶段用带占位符保护的 _optimize_h3_prompt），
    出错就抛异常；这里再做一遍 validate() 兜底。没有 rewrite_fn 时用 chat_fn 直接改写，校验不过会带着问题重试。
    """
    if rewrite_fn is not None:
        try:
            text = rewrite_fn(original).strip()
        except Exception as exc:  # noqa: BLE001
            return original, [f"改写失败：{exc}"]
        problems = validate(original, text)
        return (text, []) if not problems else (original, problems)
    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": original}]
    problems: List[str] = []
    for _ in range(retries + 1):
        try:
            text = chat_fn(messages, kind="text", temperature=0).strip()
        except Exception as exc:  # noqa: BLE001 - 模型调用失败也回退到原文
            return original, [f"模型调用失败：{exc}"]
        problems = validate(original, text)
        if not problems:
            return text, []
        messages = messages[:2] + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "Your rewrite broke these rules: " + "; ".join(problems)
             + ". Rewrite again from the ORIGINAL prompt and fix every one of them. Output only the prompt."},
        ]
    return original, problems


def _index_path(root: Path) -> Path:
    return Path(root) / DIR_NAME / INDEX_NAME


def _read_index(root: Path) -> Dict[str, Any]:
    try:
        data = json.loads(_index_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_optimized(root: Path, item: Dict[str, Any]) -> Optional[str]:
    """提交时调用：有「与当前原提示词对得上」的优化结果就返回它，否则返回 None。"""
    entry = _read_index(root).get(str(item.get("id")))
    if not isinstance(entry, dict) or entry.get("source_hash") != prompt_hash(str(item.get("prompt") or "")):
        return None
    if not entry.get("ok"):
        return None
    try:
        text = (Path(root) / DIR_NAME / entry["file"]).read_text(encoding="utf-8").strip()
    except (OSError, KeyError):
        return None
    if not text or entry.get("optimized_hash") != prompt_hash(text):
        return None
    return text


def optimize_items(root: Path, items: List[Dict[str, Any]], *, force: bool = False,
                   chat_fn: Optional[Callable[..., str]] = None, rewrite_fn: Optional[Callable[[str], str]] = None,
                   log: Callable[[str], None] = print, workers: int = 3) -> Dict[str, str]:
    """优化 items 里每个镜头的提示词并落盘；返回 {镜头ID: "optimized" | "cached" | "fallback"}。"""
    if chat_fn is None and rewrite_fn is None:
        from .llm import chat as chat_fn  # noqa: PLC0415
    from .stages.base import parallel_map  # noqa: PLC0415

    root = Path(root)
    out_dir = root / DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    index = _read_index(root)
    status: Dict[str, str] = {}
    todo = []
    for item in items:
        sid, original = str(item["id"]), str(item.get("prompt") or "")
        if not original.strip():
            status[sid] = "fallback"
            log(f"[优化] {sid}：原提示词为空，跳过")
            continue
        if not force and load_optimized(root, item) is not None:
            status[sid] = "cached"
            log(f"[优化] {sid}：已有优化结果，跳过（想重做加 --video-optimize-force）")
            continue
        todo.append(item)

    def work(item: Dict[str, Any]) -> Tuple[str, str, List[str]]:
        text, problems = optimize_one(str(item["prompt"]), chat_fn, rewrite_fn=rewrite_fn)
        return str(item["id"]), text, problems

    for result, item in zip(parallel_map(work, todo, workers), todo):
        sid, original = str(item["id"]), str(item["prompt"])
        if isinstance(result, Exception):
            status[sid] = "fallback"
            index[sid] = {"source_hash": prompt_hash(original), "ok": False, "problems": [str(result)]}
            log(f"[优化] {sid}：失败，提交时仍用原提示词：{result}")
            continue
        _sid, text, problems = result
        if problems:
            status[sid] = "fallback"
            index[sid] = {"source_hash": prompt_hash(original), "ok": False, "problems": problems}
            log(f"[优化] {sid}：优化结果没通过检查，提交时仍用原提示词：{'；'.join(problems)}")
            continue
        name = _safe(sid) + ".txt"
        (out_dir / name).write_text(text + "\n", encoding="utf-8")
        index[sid] = {"source_hash": prompt_hash(original), "optimized_hash": prompt_hash(text), "file": name, "ok": True}
        status[sid] = "optimized"
        log(f"[优化] {sid}：完成（{len(original)} → {len(text)} 字，<Picture N> 全部保留）")
    _index_path(root).write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return status
