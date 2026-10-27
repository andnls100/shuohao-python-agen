"""H3 Ref2VA 参考图编号：素材 ID 占位符 → 最终图片编号。

问题背景：分镜阶段模型写提示词时，并不知道视频阶段最终会上传哪几张图、按什么顺序。
旧做法让模型直接写 <Picture N>，校验器还要求 N 恰好等于分镜数，于是单镜头的段全是 <Picture 1>，
而视频阶段上传的是「角色 → 场景 → 道具」的素材图，两套编号对不上。

现在的做法：
  1. 模型只写素材 ID：<Picture C02>（角色）、<Picture S01>（场景）、<Picture P03>（道具）；
  2. 程序绑定 cut.assetRefs 之后，用 build_ref_order() 算出这一段的参考图顺序（refOrder），
     再用 resolve_placeholders() 把 ID 换成最终编号；
  3. 视频阶段按同一份 refOrder 上传图片，编号天然一致；
  4. 参考图超过 max_refs 被拼成拼图时，用 remap_picture_tags() 把被合并的编号同步改掉。

这个模块只做字符串和列表运算，不读文件、不调模型。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

KIND_ORDER = ("character", "scene", "prop")  # 与 frames._references 的上传顺序一致：角色 → 场景 → 道具

# 数字编号：<Picture 3>；占位符：不以数字开头的任意写法（素材 ID C02、素材名 燕溪山），避免和数字编号混淆
NUMBERED = re.compile(r"<\s*Picture\s*(\d+)\s*>", re.I)
PLACEHOLDER = re.compile(r"<\s*Picture\s+([^\s>\d][^>]*?)\s*>", re.I)


def asset_tag(ref: Mapping[str, Any]) -> str:
    """cut.assetRefs 条目的短 ID：id 形如 "character:C02" 取冒号后面，没有冒号就用 name。"""
    raw = str(ref.get("id") or "").strip()
    if ":" in raw:
        raw = raw.split(":", 1)[1].strip()
    return raw or str(ref.get("name") or "").strip()


def build_ref_order(segment: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """一段的参考图顺序：段内所有分镜 assetRefs 的并集，角色 → 场景 → 道具，同一素材只算一次，编号从 1 开始。

    每个条目：{"no", "id", "kind", "name", "tag"}。id 沿用 assetRefs 里的稳定 ID（如 "character:C02"），
    tag 是占位符里写的短 ID（如 "C02"）。
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k in KIND_ORDER}
    seen = set()
    for cut in segment.get("cuts") or []:
        if not isinstance(cut, Mapping):
            continue
        for ref in cut.get("assetRefs") or []:
            if not isinstance(ref, Mapping):
                continue
            kind = str(ref.get("kind") or "")
            if kind not in buckets:
                continue
            key = (kind, str(ref.get("id") or ref.get("name") or ""))
            if key in seen or not key[1]:
                continue
            seen.add(key)
            buckets[kind].append({
                "id": str(ref.get("id") or f"{kind}:{ref.get('name')}"),
                "kind": kind,
                "name": str(ref.get("name") or "").strip(),
                "tag": asset_tag(ref),
            })
    order: List[Dict[str, Any]] = []
    for kind in KIND_ORDER:
        order.extend(buckets[kind])
    for no, entry in enumerate(order, 1):
        entry["no"] = no
    return order


def tag_lookup(order: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """占位符 → 编号。同时认短 ID（C02）和素材名，大小写不敏感；短 ID 优先。"""
    table: Dict[str, int] = {}
    for entry in order:
        for key in (entry.get("name"), entry.get("tag")):
            if key:
                table[str(key).strip().upper()] = int(entry["no"])
    for entry in order:  # 短 ID 覆盖同名的素材名，避免两个素材撞名时串号
        if entry.get("tag"):
            table[str(entry["tag"]).strip().upper()] = int(entry["no"])
    return table


def resolve_placeholders(prompt: str, order: Sequence[Mapping[str, Any]]) -> Tuple[str, List[str]]:
    """把 <Picture C02> 换成 <Picture N>。返回（新提示词，没能解析的占位符列表）。无法解析的原样保留，交给校验器报错。"""
    table = tag_lookup(order)
    unresolved: List[str] = []

    def sub(match: "re.Match[str]") -> str:
        key = match.group(1).strip().upper()
        if key in table:
            return f"<Picture {table[key]}>"
        if match.group(1) not in unresolved:
            unresolved.append(match.group(1))
        return match.group(0)

    return PLACEHOLDER.sub(sub, str(prompt)), unresolved


def picture_numbers(prompt: str) -> List[int]:
    """提示词里出现过的数字编号（去重、升序）。"""
    return sorted({int(n) for n in NUMBERED.findall(str(prompt))})


def remap_picture_tags(prompt: str, mapping: Mapping[int, int]) -> str:
    """一次性把 <Picture old> 换成 <Picture new>。单遍替换，所以 1→2、2→3 这类映射不会连锁。"""
    return NUMBERED.sub(lambda m: f"<Picture {mapping.get(int(m.group(1)), int(m.group(1)))}>", str(prompt))


def collage_mapping(count: int, max_refs: int, front: int = 0) -> Dict[int, int]:
    """参考图合并后的编号映射。

    count：原始参考图张数（不含前置的上一镜帧）；max_refs：最多上传几张；
    front：最前面额外插入几张（上一镜最后一帧），原有编号整体后移。
    超过 max_refs 时，最后几张拼成一张放在第 max_refs 位，被拼进去的编号都指向这一位。
    返回 {原编号: 新编号}，原编号从 1 开始。
    """
    total = count + front
    mapping: Dict[int, int] = {}
    for old in range(1, count + 1):
        position = old + front
        if total <= max_refs or max_refs < 1:
            mapping[old] = position
        elif max_refs == 1:
            mapping[old] = 1
        else:
            mapping[old] = position if position <= max_refs - 1 else max_refs
    return mapping


def describe_order(order: Iterable[Mapping[str, Any]]) -> str:
    """给日志和修复提示用的一行行说明：`1 = character C02 燕溪山`。"""
    return "\n".join(f"{e['no']} = {e['kind']} {e.get('tag')} {e.get('name')}".rstrip() for e in order)
