"""Build a consistency-asset manifest from existing production JSON.

This stage does not generate images or videos. It links character/scene/prop assets
to storyboard cuts and records which reference assets should be created or reused.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..validator import Result
from .base import Context, read_json, write_json


def artifact(ctx: Context) -> Path:
    return ctx.out / "continuity" / "continuity-manifest.json"


def _image_items(ctx: Context) -> Dict[str, dict]:
    path = ctx.out / "images" / "image-manifest.json"
    if not path.is_file():
        return {}
    try:
        data = read_json(path)
    except (OSError, ValueError, TypeError):
        return {}
    return {
        str(item.get("id")): item
        for item in data.get("items", [])
        if isinstance(item, dict) and item.get("id")
    }


def _aliases(character: dict) -> List[str]:
    values = [character.get("name"), *(character.get("aliases") or [])]
    return list(dict.fromkeys(
        str(value).strip() for value in values
        if isinstance(value, str) and len(value.strip()) >= 2
    ))


def _scene_index(staging: dict) -> Dict[tuple, dict]:
    result = {}
    for scene in staging.get("scenes", []):
        if isinstance(scene, dict):
            result[(scene.get("ep"), scene.get("scene"))] = scene
    return result


def _mentioned_characters(text: str, characters: List[dict]) -> List[str]:
    found = []
    for character in characters:
        ident = str(character.get("id") or "").strip()
        if not ident:
            continue
        if any(alias in text for alias in _aliases(character)):
            found.append(ident)
    return found


def check(ctx: Context) -> Result:
    path = artifact(ctx)
    if not path.is_file():
        return Result(1, "一致性资产清单尚未生成")
    try:
        data = read_json(path)
    except (OSError, ValueError, TypeError) as exc:
        return Result(1, f"一致性资产清单不可读：{exc}")
    items = data.get("shots")
    if not isinstance(items, list) or not items:
        return Result(1, "一致性资产清单没有镜头条目")
    missing = [
        str(item.get("id", "?")) for item in items
        if not item.get("reference_image", {}).get("planned_path")
    ]
    if missing:
        return Result(1, "以下镜头缺少参考图目标路径：" + "、".join(missing[:10]))
    return Result(0, f"{len(items)} 个镜头的一致性资产清单已生成")


def run(ctx: Context) -> Path:
    cast_path = ctx.artifact("characters")
    art_path = ctx.artifact("art")
    storyboard_path = ctx.artifact("storyboard")
    for path in (cast_path, art_path, storyboard_path):
        if not path.is_file():
            raise FileNotFoundError(f"生成一致性资产清单需要已有文件：{path}")

    cast = read_json(cast_path)
    art = read_json(art_path)
    board = read_json(storyboard_path)
    staging_path = ctx.artifact("staging")
    staging = read_json(staging_path) if staging_path.is_file() else {}
    image_items = _image_items(ctx)

    characters = cast.get("characters", [])
    scenes = art.get("scenes", [])
    props = art.get("props", [])
    char_by_id = {str(c.get("id")): c for c in characters if c.get("id")}
    scene_by_id = {str(s.get("id")): s for s in scenes if s.get("id")}
    prop_by_id = {str(p.get("id")): p for p in props if p.get("id")}
    staged_scenes = _scene_index(staging)

    def existing_image(asset_id: str, kind: str) -> Optional[str]:
        row = image_items.get(f"{kind}:{asset_id}", {})
        relative = row.get("output_path")
        if row.get("state") == "succeeded" and relative:
            path = ctx.out / str(relative)
            if path.is_file():
                return str(path.relative_to(ctx.out)).replace("\\", "/")
        return None

    character_assets = []
    for character in characters:
        ident = str(character.get("id") or "")
        if not ident:
            continue
        character_assets.append({
            "id": ident,
            "name": character.get("name", ident),
            "aliases": character.get("aliases", []),
            "reference_image": existing_image(ident, "character"),
            "planned_reference_path": f"continuity/characters/{ident}/anchor.png",
            "image_status": "available" if existing_image(ident, "character") else "pending",
        })

    scene_assets = []
    for scene in scenes:
        ident = str(scene.get("id") or "")
        if not ident:
            continue
        scene_assets.append({
            "id": ident,
            "name": scene.get("name", ident),
            "anchors": scene.get("anchors", []),
            "reference_image": existing_image(ident, "scene"),
            "planned_reference_path": f"continuity/scenes/{ident}/master.png",
            "image_status": "available" if existing_image(ident, "scene") else "pending",
        })

    prop_assets = []
    for prop in props:
        ident = str(prop.get("id") or "")
        if not ident:
            continue
        prop_assets.append({
            "id": ident,
            "name": prop.get("name", ident),
            "reference_image": existing_image(ident, "prop"),
            "planned_reference_path": f"continuity/props/{ident}/anchor.png",
            "image_status": "available" if existing_image(ident, "prop") else "pending",
        })

    shots = []
    for episode in board.get("episodes", []):
        ep = int(episode.get("ep", 0))
        for seg_index, segment in enumerate(episode.get("segments", []), 1):
            segment_id = str(segment.get("id") or f"E{ep:02d}-{seg_index:02d}")
            scene_number = segment.get("sceneIndex")
            staging_scene = staged_scenes.get((ep, scene_number), {})
            scene_id = str(segment.get("sceneId") or staging_scene.get("sceneId") or "")
            scene = scene_by_id.get(scene_id, {})
            for cut_index, cut in enumerate(segment.get("cuts", []), 1):
                shot_id = f"{segment_id}-C{cut_index:02d}"
                shot_text = " ".join(str(cut.get(k) or "") for k in ("shot", "frame", "description"))
                context_text = shot_text + " " + str(segment.get("blocking") or "")
                character_ids = _mentioned_characters(context_text, characters)
                # Staging provides explicit presence data; use it to fill omissions in shot text.
                staging_places = staging_scene.get("places", []) if isinstance(staging_scene, dict) else []
                for place in staging_places:
                    name = str(place.get("name") or place.get("character") or "") if isinstance(place, dict) else ""
                    for character in characters:
                        ident = str(character.get("id") or "")
                        if ident and name and name in _aliases(character) and ident not in character_ids:
                            character_ids.append(ident)
                prop_ids = []
                for ident, prop in prop_by_id.items():
                    aliases = [
                        str(value).strip()
                        for value in [prop.get("name"), *(prop.get("aliases") or [])]
                        if value is not None and str(value).strip()
                    ]
                    if any(len(alias) >= 2 and alias in context_text for alias in aliases):
                        prop_ids.append(ident)
                char_refs = [
                    {"id": ident, "name": char_by_id[ident].get("name"),
                     "reference_image": existing_image(ident, "character"),
                     "planned_reference_path": f"continuity/characters/{ident}/anchor.png"}
                    for ident in character_ids if ident in char_by_id
                ]
                prop_refs = [
                    {"id": ident, "name": prop_by_id[ident].get("name"),
                     "reference_image": existing_image(ident, "prop"),
                     "planned_reference_path": f"continuity/props/{ident}/anchor.png"}
                    for ident in prop_ids if ident in prop_by_id
                ]
                shots.append({
                    "id": shot_id,
                    "episode": ep,
                    "segment_id": segment_id,
                    "cut_index": cut_index,
                    "scene_id": scene_id or None,
                    "scene_name": scene.get("name") if scene else None,
                    "character_ids": character_ids,
                    "prop_ids": prop_ids,
                    "association_method": "text_match_plus_staging",
                    "shot_description": shot_text,
                    "video_prompt_source": "cut fields + segment context",
                    "reference_assets": {
                        "scene": ({
                            "id": scene_id,
                            "name": scene.get("name"),
                            "reference_image": existing_image(scene_id, "scene"),
                            "planned_reference_path": f"continuity/scenes/{scene_id}/master.png",
                        } if scene_id and scene else None),
                        "characters": char_refs,
                        "props": prop_refs,
                    },
                    "reference_image": {
                        "status": "pending",
                        "planned_path": f"continuity/shots/{shot_id}-reference.png",
                        "generation_inputs": {
                            "scene_id": scene_id or None,
                            "character_ids": character_ids,
                            "prop_ids": prop_ids,
                            "composition": cut.get("composition"),
                            "camera_position": cut.get("cameraPosition"),
                            "shot_size": cut.get("size"),
                            "lighting": cut.get("lighting"),
                            "shot_description": shot_text,
                        },
                    },
                })

    manifest = {
        "version": 1,
        "source": ctx.title,
        "status": "asset_plan_only",
        "note": "This stage only builds the consistency-asset plan. It does not generate images or videos. Review inferred associations before using them for production.",
        "source_files": {
            "characters": str(cast_path.relative_to(ctx.out)).replace("\\", "/"),
            "art": str(art_path.relative_to(ctx.out)).replace("\\", "/"),
            "storyboard": str(storyboard_path.relative_to(ctx.out)).replace("\\", "/"),
            "staging": str(staging_path.relative_to(ctx.out)).replace("\\", "/") if staging_path.is_file() else None,
            "image_manifest": "images/image-manifest.json" if (ctx.out / "images" / "image-manifest.json").is_file() else None,
        },
        "assets": {
            "characters": character_assets,
            "scenes": scene_assets,
            "props": prop_assets,
        },
        "shots": shots,
        "summary": {
            "characters": len(character_assets),
            "scenes": len(scene_assets),
            "props": len(prop_assets),
            "shots": len(shots),
            "shots_with_scene": sum(bool(s.get("scene_id")) for s in shots),
            "shots_without_scene": sum(not s.get("scene_id") for s in shots),
            "shots_without_character_match": sum(not s.get("character_ids") for s in shots),
        },
    }
    path = artifact(ctx)
    write_json(path, manifest)
    ctx.log(f"一致性资产清单已生成：{path}")
    ctx.log(
        f"角色 {len(character_assets)} · 场景 {len(scene_assets)} · 道具 {len(prop_assets)} · 镜头 {len(shots)}；"
        f"未关联场景 {manifest['summary']['shots_without_scene']}，未匹配角色 {manifest['summary']['shots_without_character_match']}"
    )
    ctx.log("本阶段只生成资产清单，不会调用生图或视频 API。")
    return path
