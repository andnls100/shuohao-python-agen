"""调用技能包的 Node 工具，并解读校验结果。

技能包的所有 validate / seed / render 都是零依赖的 Node 脚本：
通过 → 退出码 0；不通过 → 退出码 1，并逐行打印"质量门未过：……"。
本模块把这套约定封装成 Result，并提供把报错行定位到具体条目的路由函数。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Settings
from .skill_loader import Skill


class NodeError(RuntimeError):
    """Node 不可用，或命令本身出错（不是校验不通过）。"""


class StageFailed(RuntimeError):
    """多轮回炉后校验仍未通过。"""

    def __init__(self, label: str, result: "Result"):
        self.label = label
        self.result = result
        super().__init__(f"[{label}] 校验仍未通过：\n{result.text}")


@dataclass
class Result:
    code: int
    stdout: str = ""
    stderr: str = ""
    args: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return "\n".join(part for part in (self.stdout.strip(), self.stderr.strip()) if part)

    @property
    def violations(self) -> List[str]:
        """逐条违规。跳过 ✓ / ✗ / ⚠ 开头的汇总行和提示行。"""
        lines = []
        for raw in self.text.splitlines():
            line = raw.strip()
            if not line or line.startswith(("✓", "✗", "⚠")):
                continue
            lines.append(line)
        return lines


def check_node(settings: Settings) -> str:
    """确认 Node 可用且 >= 18，返回版本号。"""
    try:
        proc = subprocess.run(
            [settings.node_bin, "--version"], capture_output=True, text=True, timeout=20
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise NodeError(
            f"找不到 Node（{settings.node_bin}）。技能包的校验和渲染脚本需要 Node >= 18，"
            "请先安装：https://nodejs.org/ ，或用环境变量 NODE_BIN 指定路径。"
        ) from exc
    version = proc.stdout.strip()
    match = re.match(r"v?(\d+)", version)
    if proc.returncode != 0 or not match:
        raise NodeError(f"无法读取 Node 版本：{proc.stdout} {proc.stderr}")
    if int(match.group(1)) < 18:
        raise NodeError(f"Node 版本过低（{version}），需要 >= 18")
    return version


def run_node(
    settings: Settings,
    skill: Skill,
    args: Sequence[Any],
    *,
    cwd: Optional[Path] = None,
    timeout: int = 600,
) -> Result:
    """运行 skills/<name>/scripts/<name>.mjs。参数以列表传递，不经过 shell。"""
    if not skill.script.is_file():
        raise NodeError(f"找不到脚本：{skill.script}")
    cmd = [settings.node_bin, str(skill.script), *[str(a) for a in args]]
    env = {**os.environ, "NO_COLOR": "1"}
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise NodeError(f"找不到 Node（{settings.node_bin}）") from exc
    except subprocess.TimeoutExpired as exc:
        raise NodeError(f"命令超时（{timeout}s）：{' '.join(cmd)}") from exc
    return Result(proc.returncode, proc.stdout, proc.stderr, [str(a) for a in args])


def node_json(
    settings: Settings,
    skill: Skill,
    args: Sequence[Any],
    *,
    cwd: Optional[Path] = None,
) -> Any:
    """运行命令并把 stdout 当 JSON 解析。命令失败按 NodeError 抛出。"""
    result = run_node(settings, skill, args, cwd=cwd)
    if not result.ok:
        raise NodeError(f"{skill.name} {' '.join(map(str, args))} 失败：\n{result.text}")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise NodeError(
            f"{skill.name} {args[0]} 的输出不是 JSON：{result.stdout[:200]!r}"
        ) from exc


def route_violations(
    lines: Sequence[str], tokens: Dict[str, Sequence[str]]
) -> Tuple[Dict[str, List[str]], List[str]]:
    """把报错行归给对应的条目。

    tokens: 条目 key -> 能在报错行里认出它的字符串（"第 3 集"、"E03-"、角色名……）。
    返回 (key -> 属于它的报错行, 无法归属的报错行)。一行报错可以同时归给多个条目。
    """
    routed: Dict[str, List[str]] = {}
    unrouted: List[str] = []
    for line in lines:
        hit = False
        for key, toks in tokens.items():
            if any(tok and tok in line for tok in toks):
                routed.setdefault(key, []).append(line)
                hit = True
        if not hit:
            unrouted.append(line)
    return routed, unrouted


def ok_result() -> Result:
    return Result(0)
