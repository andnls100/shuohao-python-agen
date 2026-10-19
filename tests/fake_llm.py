"""离线测试用的模拟模型。

不联网、不花额度：按提示词里的标记（[PASS] 技能/指令文档、「角色名」、第 N 集……）
从技能包自带的《渡口》样例里取出对应的标准答案返回。
样例本身通过了全部质量门，所以整条流水线应当全部校验通过——
这验证的是代码的接线（合并、校验、回炉、缓存），不是真实模型的写作质量。
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

SKILLS = Path(__file__).resolve().parent.parent / "skills"
FIX_MARK = "外部校验程序对你这一部分输出报了以下问题"


def _load(rel: str) -> Any:
    return json.loads((SKILLS / rel).read_text(encoding="utf-8"))


class FakeModel:
    def __init__(self, *, break_script_ep: Optional[int] = None, break_cast_name: Optional[str] = None):
        self.outline = _load("novel-outline/examples/渡口-outline.json")
        self.cast = _load("novel-characters/examples/渡口-cast.json")
        self.art = _load("novel-art/examples/渡口-art.json")
        self.script = _load("novel-script/examples/渡口-script.json")
        self.staging = _load("novel-staging/examples/渡口-staging.json")
        self.storyboard = _load("novel-storyboard/examples/渡口-storyboard.json")
        self.calls: Counter = Counter()      # (pass, 条目) -> 调用次数
        self.fix_calls: List[str] = []
        self.break_script_ep = break_script_ep
        self.break_cast_name = break_cast_name

    # ---------------------------------------------------------- 分派
    def __call__(self, messages: List[Dict[str, Any]], *, model: str = "") -> str:
        system = messages[0]["content"]

        def text_of(content: Any) -> str:
            if isinstance(content, str):
                return content
            return next(p["text"] for p in content if p.get("type") == "text")  # 多模态：取文字部分

        # 回炉轮的最后一条只有报错；条目是谁要从这段对话的第一条 user 消息（原始提示词）里认
        user = text_of(messages[1]["content"])
        is_fix = FIX_MARK in text_of(messages[-1]["content"])
        first = system.splitlines()[0]
        passname = first.replace("[PASS]", "").strip() if first.startswith("[PASS]") else system[:12]
        if is_fix:
            self.fix_calls.append(passname)

        handler = {
            "novel-outline/volume-pass.md": self._volume,
            "character-refs/intake.md": self._intake,
            "novel-outline/outline-pass.md": self._skeleton,
            "novel-outline/episode-pass.md": self._outline_eps,
            "novel-characters/roster-pass.md": self._roster,
            "novel-characters/profile-pass.md": self._card,
            "novel-art/scene-pass.md": self._scene,
            "novel-art/prop-pass.md": self._prop,
            "flux-image-best-practices/optimize-pass.md": self._flux,
            "novel-script/script-pass.md": self._script_ep,
            "novel-staging/read-pass.md": self._location,
            "novel-staging/staging-pass.md": self._staging_scenes,
        }.get(passname)
        if passname.startswith("novel-storyboard/"):
            handler = self._storyboard_ep
        if handler is None:
            if system.startswith("你是编辑"):
                return self.cast["summary"]
            if system.startswith("你在复核角色归并候选"):
                return json.dumps({"merges": []}, ensure_ascii=False)
            raise AssertionError(f"模拟模型不认识这个请求：{system[:80]!r}")
        return json.dumps(handler(user, is_fix, passname), ensure_ascii=False)

    def _count(self, passname: str, key: Any) -> None:
        self.calls[(passname, str(key))] += 1

    # ---------------------------------------------------------- outline
    def _volume(self, user, is_fix, passname):
        self._count(passname, re.search(r"这是第 (\d+) 卷", user).group(1))
        return {"plotlines": [{"name": "主线", "events": "按顺序写具体", "hookPotential": "开箱"}],
                "characters": [{"name": "沈知微", "aliases": [], "doing": "抱箱上船"}],
                "beatMaterial": [{"moment": "开箱", "type": "反转", "evidence": "原文片段"}],
                "settings": ["渡船船舱"]}

    def _intake(self, user, is_fix, passname):
        self._count(passname, "intake")
        return _load("character-refs/examples/阿禾-intake.json")

    def _skeleton(self, user, is_fix, passname):
        self._count(passname, "skeleton")
        self.last_skeleton_prompt = user
        return {k: self.outline[k] for k in ("adaptation", "characters", "scenes", "props", "beats")}

    def _outline_eps(self, user, is_fix, passname):
        lo, hi = map(int, re.search(r"写第 (\d+)–(\d+) 集", user).groups())
        self._count(passname, f"{lo}-{hi}")
        return [e for e in self.outline["episodes"] if lo <= e["ep"] <= hi]

    # ---------------------------------------------------------- characters
    def _roster(self, user, is_fix, passname):
        self._count(passname, "roster")
        return {"characters": [
            {"name": c["name"], "aliases": c.get("aliases", []),
             "note": f"{c.get('oneLiner', '')} {c['persona'].get('appearance', '')}",
             "quotes": c["persona"].get("evidence", [])}
            for c in self.cast["characters"]
        ]}

    def _card(self, user, is_fix, passname):
        name = re.search(r"为角色「(.+?)」产出", user).group(1)
        self._count(passname, name)
        by_name = {c["name"]: c for c in self.cast["characters"]}
        if name in by_name:
            card = copy.deepcopy(by_name[name])
        else:  # 大纲里有、样例 cast 里没有的角色（功能性角色）：借用次要角色的卡，改名，并换掉两段提示词
            card = copy.deepcopy(self.cast["characters"][-1])  # 否则会被"同批角色提示词雷同"门拦住
            card["name"] = name
            card["aliases"] = []
            card["importance"] = "minor"
            card["image"]["prompt"] = (
                "A wiry elderly Chinese night watchman, thin stooped frame, deeply lined weathered face, "
                "wearing a patched dark cotton jacket and a tattered straw hat, holding a hooded paper lantern "
                "and a small wooden clapper, standing on a damp riverbank at dawn, plain neutral background"
            )
            card["voice"]["prompt"] = (
                "Late-60s male, dry hoarse baritone, low pitch, slow flat delivery, "
                "short clipped phrases with long pauses, weary and unhurried, rural southern accent"
            )
        if self.break_cast_name == name and not is_fix:
            card["image"]["prompt"] = f"{name} standing in the fog"  # 出图提示词里写了人名 → 应被拦
        return card

    # ---------------------------------------------------------- flux 提示词优化
    FLUX_TAIL = " Soft window light from the left."

    def _flux(self, user, is_fix, passname):
        body = user.split("## 原提示词\n", 1)[1].split("\n\n## ", 1)[0]
        self._count(passname, "flux")
        return {k: v + self.FLUX_TAIL for k, v in json.loads(body).items()}

    # ---------------------------------------------------------- art
    def _scene(self, user, is_fix, passname):
        sid = re.search(r"场景「.+?」（(S\d+)）", user).group(1)
        self._count(passname, sid)
        return next(s for s in self.art["scenes"] if s["id"] == sid)

    def _prop(self, user, is_fix, passname):
        pid = re.search(r"叙事道具「.+?」（(P\d+)）", user).group(1)
        self._count(passname, pid)
        return next(p for p in self.art["props"] if p["id"] == pid)

    # ---------------------------------------------------------- script
    def _script_ep(self, user, is_fix, passname):
        ep = int(re.search(r"写第 (\d+) 集的完整剧本", user).group(1))
        self._count(passname, ep)
        data = copy.deepcopy(next(e for e in self.script["episodes"] if e["ep"] == ep))
        if self.break_script_ep == ep and not is_fix:
            data["scenes"] = []  # 第一次故意交空剧本 → 应被时长门拦住并回炉
        return data

    # ---------------------------------------------------------- staging
    def _location(self, user, is_fix, passname):
        sid = re.search(r"（(S\d+)）里人能站", user).group(1)
        self._count(passname, sid)
        return next(l for l in self.staging["locations"] if l["sceneId"] == sid)

    def _staging_scenes(self, user, is_fix, passname):
        pairs = [(int(a), int(b)) for a, b in re.findall(r'"ep": (\d+),\s+"scene": (\d+)', user)]
        self._count(passname, pairs)
        return [s for s in self.staging["scenes"] if (s["ep"], s["scene"]) in pairs]

    # ---------------------------------------------------------- storyboard
    def _storyboard_ep(self, user, is_fix, passname):
        ep = int(re.search(r"给第 (\d+) 集切分镜", user).group(1))
        self._count("novel-storyboard", ep)
        episode = next(e for e in self.storyboard["episodes"] if e["ep"] == ep)
        return copy.deepcopy(episode)
