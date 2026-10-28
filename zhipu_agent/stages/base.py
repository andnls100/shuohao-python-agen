"""阶段公共引擎。

所有阶段共用同一个循环（run_items）：
  1. 并发地让模型生成每个条目（一集 / 一个场景 / 一张角色卡……）
  2. 把结果写进目标文件，调用技能包的 validate
  3. 校验不过 → 按报错里的集号 / 场景名 / 角色名把报错归给对应条目，
     只让这些条目带着报错回炉，其余条目不动
  4. 最多回炉 settings.fix_rounds 轮，仍不过就抛出 StageFailed，文件保留在磁盘上供人工修改
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from ..config import Settings
from ..llm import Conversation
from ..skill_loader import Skill, SkillPack
from ..validator import (
    Result,
    StageFailed,
    node_json,
    ok_result,
    route_violations,
    run_node,
)

# 各阶段的产物位置：(子目录, 文件名模板)，{t} 是书名
STAGE_FILES = {
    "outline": ("outline", "{t}-outline.json"),
    "characters": ("characters", "{t}-cast.json"),
    "art": ("art", "{t}-art.json"),
    "script": ("script", "{t}-script.json"),
    "staging": ("staging", "staging.json"),
    "storyboard": ("storyboard", "{t}-storyboard.json"),
    "continuity": ("continuity", "continuity-manifest.json"),
}


# ---------------------------------------------------------------- 小工具

def safe_name(name: str) -> str:
    """文件名安全化：去掉路径分隔符和 Windows 不允许的字符。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" ._")
    return cleaned or "untitled"


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    """原子写入：先写临时文件再替换，中途被打断不会留下半个文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_text_auto(path: Path) -> str:
    """读文本，自动识别 UTF-8 / GB18030（中文网文常见）。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def guess_title(text: str, fallback: str) -> str:
    match = re.search(r"《([^》\n]{1,40})》", text[:400])
    return match.group(1).strip() if match else fallback


def parse_eps(spec: str) -> List[int]:
    """'1-6' / '1,3,5-7' -> [1,2,...]"""
    eps: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            eps.extend(range(int(lo), int(hi) + 1))
        else:
            eps.append(int(part))
    return sorted(set(eps))


def batches(seq: Sequence[Any], size: int) -> List[List[Any]]:
    return [list(seq[i:i + size]) for i in range(0, len(seq), size)]


def ep_tokens(ep: int) -> List[str]:
    """能在报错里认出第 ep 集的写法。"""
    return [f"第 {ep} 集", f"第{ep}集", f"E{ep:02d}-", f"E{ep:02d}#"]


def scene_tokens(ep: int, scene: int) -> List[str]:
    return [f"第 {ep} 集第 {scene} 场", f"第{ep}集第{scene}场"]


def short(obj: Any, limit: int = 4000) -> str:
    """给提示词用的紧凑 JSON；过长时截断并注明。"""
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    if len(text) > limit:
        text = text[:limit] + "\n…（已截断）"
    return text


def compact(obj: Any, maxlen: int = 160, drop: Sequence[str] = ()) -> Any:
    """递归缩短长字符串、去掉指定键，给提示词里的参考资料瘦身（不改原数据）。"""
    if isinstance(obj, dict):
        return {k: compact(v, maxlen, drop) for k, v in obj.items() if k not in drop}
    if isinstance(obj, list):
        return [compact(v, maxlen, drop) for v in obj]
    if isinstance(obj, str) and len(obj) > maxlen:
        return obj[:maxlen] + "…"
    return obj


def parallel_map(fn: Callable[[Any], Any], items: Iterable[Any], workers: int) -> List[Any]:
    """并发执行；异常作为返回值交还，由调用方决定怎么处理（保证已完成的结果不丢）。"""
    def safe(item):
        try:
            return fn(item)
        except Exception as exc:  # noqa: BLE001
            return exc

    items = list(items)
    if workers <= 1 or len(items) <= 1:
        return [safe(i) for i in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(safe, items))


# ---------------------------------------------------------------- 上下文

@dataclass
class Context:
    settings: Settings
    pack: SkillPack
    out: Path
    book_file: Path            # UTF-8 的原文
    title: str
    lang: str = "zh"
    interactive: bool = True
    force: bool = False
    images_dir: Optional[Path] = None
    convs: Dict[str, Dict[str, Conversation]] = field(default_factory=dict)
    _log_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.out = Path(self.out).resolve()
        self.book_file = Path(self.book_file).resolve()

    # -- 路径
    def dir(self, *parts: str) -> Path:
        path = self.out.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def work(self, name: str) -> Path:
        return self.dir("work", name)

    def artifact(self, stage: str) -> Path:
        sub, pattern = STAGE_FILES[stage]
        return self.out / sub / pattern.format(t=safe_name(self.title))

    def skill(self, name: str) -> Skill:
        return self.pack[name]

    # -- 日志
    def log(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        with self._log_lock:
            print(line, flush=True)
            try:
                self.out.mkdir(parents=True, exist_ok=True)
                with open(self.out / "agent.log", "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    # -- 交互
    def ask(self, prompt: str) -> str:
        """交互模式下向用户提问；非交互模式返回空串（视为接受默认）。"""
        if not self.interactive:
            return ""
        try:
            return input(prompt).strip()
        except EOFError:
            return ""

    # -- Node 工具
    def run(self, skill: Skill, args: Sequence[Any], *, cwd: Optional[Path] = None) -> Result:
        return run_node(self.settings, skill, args, cwd=cwd)

    def node_json(self, skill: Skill, args: Sequence[Any], *, cwd: Optional[Path] = None) -> Any:
        return node_json(self.settings, skill, args, cwd=cwd)

    def render_to(self, skill: Skill, args: Sequence[Any], target: Path, *, cwd: Optional[Path] = None) -> None:
        """运行 render 并把 stdout 写成文件。失败只告警，不中断流水线。"""
        result = self.run(skill, args, cwd=cwd)
        if result.ok and result.stdout.strip():
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text(result.stdout, encoding="utf-8")
            self.log(f"已生成 {Path(target).relative_to(self.out) if self.out in Path(target).parents else target}")
        else:
            self.log(f"⚠ 渲染 {Path(target).name} 失败：{result.text[:200]}")


# ---------------------------------------------------------------- 引擎

@dataclass
class Item:
    """一个让模型生成的条目。"""
    key: str
    system: str
    prompt: str
    tokens: List[str] = field(default_factory=list)   # 用来在报错行里认出它
    images: List[Path] = field(default_factory=list)
    kind: str = "text"                                 # "vision" = 用视觉模型池（带图的条目）
    cache: Optional[Path] = None                       # 已有缓存就不重新调用模型
    purpose: str = "unspecified"                       # 写入 LLM 用量日志
    max_tokens: Optional[int] = None                   # 本条目的输出 Token 上限


def fix_prompt(lines: Sequence[str], hint: str = "") -> str:
    body = "\n".join(f"- {line}" for line in lines)
    return (
        "外部校验程序对你这一部分输出报了以下问题：\n"
        f"{body}\n\n"
        "请逐条修正，其余内容保持不变，重新输出**完整**的 JSON（结构与上一次相同，不要只给片段）。"
        + (f"\n{hint}" if hint else "")
    )


def no_validate() -> Result:
    return ok_result()


def run_items(
    ctx: Context,
    items: Sequence[Item],
    *,
    apply: Callable[[str, Any], None],
    validate: Callable[[], Result],
    label: str,
    rounds: Optional[int] = None,
    hint: str = "",
) -> Result:
    """生成 → apply 写入 → validate → 报错回炉。通过时返回最后一次校验结果。"""
    settings = ctx.settings
    rounds = settings.fix_rounds if rounds is None else rounds
    convs = ctx.convs.setdefault(label, {})
    by_key = {it.key: it for it in items}
    last_output: Dict[str, Any] = {}
    pending: Dict[str, Optional[List[str]]] = {it.key: None for it in items}
    result = ok_result()

    for rnd in range(rounds + 1):
        keys = list(pending)
        ctx.log(f"[{label}] 第 {rnd + 1} 轮：{len(keys)} 项")

        def work(key: str):
            item = by_key[key]
            feedback = pending[key]
            if (feedback is None and item.cache and item.cache.is_file()
                    and not ctx.force and key not in convs):
                return read_json(item.cache), True
            conv = convs.get(key)
            if conv is None:
                purpose = item.purpose if item.purpose != "unspecified" else f"{label}:{key}"
                conv = convs[key] = Conversation(
                    item.system, settings, purpose=purpose, max_tokens=item.max_tokens,
                )
            if feedback is None:
                user, images = item.prompt, item.images
            elif not conv.has_history:
                previous = json.dumps(last_output.get(key), ensure_ascii=False, indent=1)
                user = f"{item.prompt}\n\n## 你上一次的输出\n{previous}\n\n{fix_prompt(feedback, hint)}"
                images = item.images
            else:
                user, images = fix_prompt(feedback, hint), []
            return conv.ask_json(user, images=images, kind=item.kind), False

        outputs = parallel_map(work, keys, settings.concurrency)
        structural: Dict[str, List[str]] = {}
        first_error: Optional[Exception] = None
        for key, out in zip(keys, outputs):
            if isinstance(out, Exception):
                first_error = first_error or out
                ctx.log(f"[{label}] {key} 调用失败：{out}")
                continue
            data, cached = out
            last_output[key] = data
            item = by_key[key]
            # 先写缓存再 apply：缓存存模型的原始输出；如果 apply 要把规范化后的结果写到同一个文件，
            # 以 apply 的为准（顺序反过来，规范化过的 id / 分档 / 名字会被原始输出覆盖）
            if item.cache and not cached:
                write_json(item.cache, data)
            try:
                apply(key, data)
            except Exception as exc:  # noqa: BLE001 - 结构不对当作违规回炉
                structural[key] = [f"输出结构不符合要求：{exc}"]
        if first_error is not None:
            raise first_error  # 已成功的条目已经写进磁盘，重跑时可续上

        result = validate()
        if result.ok and not structural:
            ctx.log(f"[{label}] 校验通过")
            return result

        lines = result.violations if not result.ok else []
        for line in lines[:6]:
            ctx.log(f"[{label}]   {line[:160]}")
        if len(lines) > 6:
            ctx.log(f"[{label}]   …另有 {len(lines) - 6} 条")

        routed, unrouted = route_violations(lines, {k: by_key[k].tokens for k in by_key})
        pending = {k: ls + unrouted for k, ls in routed.items()}
        for key, ls in structural.items():
            pending[key] = pending.get(key, []) + ls
        if not pending:
            # 报错无法归属到具体条目：全部带着报错回炉
            pending = {k: list(lines) for k in by_key}
        if rnd == rounds:
            if structural and result.ok:
                # 校验本身通过，但有条目 apply 失败（结构不对、没写入）：把原因带进异常，别吞掉
                text = "\n".join(f"{k}: {'; '.join(v)}" for k, v in structural.items())
                result = Result(1, stdout="条目未能写入：\n" + text)
            break

    raise StageFailed(label, result)
