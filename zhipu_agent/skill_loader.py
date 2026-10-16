"""技能包加载。

技能包（skills/<name>/）由三部分组成：
  SKILL.md           流程说明
  references/*.md    给模型的任务指令（*-pass.md）和数据结构（schema.md）
  scripts/<name>.mjs 确定性 Node 工具（seed / validate / render ...）

本模块只负责读取这些文件、把指令拼成发给模型的系统提示。
不改动技能包里的任何文件。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .config import Settings, load_settings


class SkillError(RuntimeError):
    """技能包缺失或结构不对。"""


def read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def split_frontmatter(text: str):
    """切出 SKILL.md 开头的 --- 块。返回 (meta 字典, 正文)。只解析 name / version。"""
    meta: Dict[str, str] = {}
    if not text.startswith("---"):
        return meta, text
    end = text.find("\n---", 3)
    if end == -1:
        return meta, text
    head = text[3:end]
    body = text[end + 4:].lstrip("\n")
    for line in head.splitlines():
        if line and not line.startswith((" ", "\t", "-")) and ":" in line:
            key, _, value = line.partition(":")
            value = value.strip()
            if key.strip() in ("name", "version") and value and value != "|":
                meta[key.strip()] = value
    return meta, body


@dataclass
class Skill:
    name: str
    root: Path
    meta: Dict[str, str] = field(default_factory=dict)

    @property
    def script(self) -> Path:
        return self.root / "scripts" / f"{self.name}.mjs"

    @property
    def skill_md(self) -> str:
        return split_frontmatter(read_text(self.root / "SKILL.md"))[1]

    def has_ref(self, filename: str) -> bool:
        return (self.root / "references" / filename).is_file()

    def ref(self, filename: str) -> str:
        """读 references/ 下的文档；{baseDir} 换成技能目录的绝对路径。"""
        path = self.root / "references" / filename
        if not path.is_file():
            raise SkillError(f"技能 {self.name} 里没有 references/{filename}")
        return read_text(path).replace("{baseDir}", self.root.as_posix())

    def example(self, filename: str) -> Path:
        return self.root / "examples" / filename


class SkillPack:
    def __init__(self, skills_dir: Path):
        self.dir = Path(skills_dir)
        if not self.dir.is_dir():
            raise SkillError(f"找不到技能包目录：{self.dir}（可用环境变量 SKILLS_DIR 指定）")
        self._skills: Dict[str, Skill] = {}
        for sub in sorted(self.dir.iterdir()):
            md = sub / "SKILL.md"
            if sub.is_dir() and md.is_file():
                meta, _ = split_frontmatter(read_text(md))
                self._skills[sub.name] = Skill(sub.name, sub, meta)

    @property
    def names(self) -> List[str]:
        return list(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def __getitem__(self, name: str) -> Skill:
        if name not in self._skills:
            raise SkillError(f"技能包里没有 {name}。现有：{', '.join(self.names) or '（空）'}")
        return self._skills[name]


def load_pack(settings: Optional[Settings] = None) -> SkillPack:
    return SkillPack((settings or load_settings()).skills_dir)


PREAMBLE = """[PASS] {skill}/{passes}
你是 AI 短剧制作流水线里的一个工作模型，正在执行技能包「{skill}」的一个步骤。
- 只做下面《任务指令》要求的这一步，按它的规则写。
- 文档里出现的 node 命令（seed / validate / render 等）由外部程序负责执行，你无法运行，也不需要运行；
  校验不通过时，报错会原样发给你，请据此逐条修改。
- 输出规则：只输出 JSON，不要解释，不要 markdown 围栏；字段名与结构以《数据结构》为准。
- 不要编造素材里没有的内容；需要逐字引用的地方必须逐字复制。"""


def build_system(
    skill: Skill,
    passes: Iterable[str],
    *,
    schema: bool = True,
    extra: str = "",
    output_json: bool = True,
) -> str:
    """把任务指令（+ 数据结构 + 额外规范）拼成系统提示。

    首行的 [PASS] 标记同时用于日志和离线测试时的分派。
    """
    passes = list(passes)
    preamble = PREAMBLE.format(skill=skill.name, passes="+".join(passes))
    if not output_json:
        preamble = preamble.replace(
            "- 输出规则：只输出 JSON，不要解释，不要 markdown 围栏；字段名与结构以《数据结构》为准。",
            "- 输出规则：按任务要求直接输出结果，不要多余的解释。",
        )
    sections = [preamble]
    for name in passes:
        sections.append(f"# 任务指令（{name}）\n\n{skill.ref(name)}")
    if schema and skill.has_ref("schema.md"):
        sections.append(f"# 数据结构（schema.md）\n\n{skill.ref('schema.md')}")
    if extra:
        sections.append(extra)
    return "\n\n".join(sections)
