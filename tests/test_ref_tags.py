"""H3 参考图编号：素材 ID 占位符 → 最终 <Picture N>，以及视频阶段按同一份 refOrder 上传。

复现的原问题：单镜头的段，提示词里全是 <Picture 1>，而视频阶段上传的是 角色 / 场景 设定图。
"""
import json
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from zhipu_agent import ref_tags as rt
from tests.test_video_comfyui import REF_WF, _Handler
from zhipu_agent.stages import video, video_comfyui as vc

# 与用户 manifest 里那个镜头同构：2 个主角 + 1 个村民群像 + 1 个场景，全在同一个分镜里
ASSET_REFS = [
    {"id": "character:C09", "kind": "character", "name": "江边村民"},
    {"id": "character:C02", "kind": "character", "name": "燕溪山"},
    {"id": "character:C03", "kind": "character", "name": "徐今朝"},
    {"id": "scene:S01", "kind": "scene", "name": "S01"},
]


def _segment(**extra):
    seg = {"id": "E01-01", "sceneIndex": 1, "cuts": [{"seconds": 6, "assetRefs": [dict(r) for r in ASSET_REFS]}]}
    seg.update(extra)
    return seg


class RefOrderTest(unittest.TestCase):
    def test_order_is_character_scene_prop_with_stable_numbers(self):
        seg = _segment()
        seg["cuts"][0]["assetRefs"].append({"id": "prop:P03", "kind": "prop", "name": "长剑"})
        order = rt.build_ref_order(seg)
        self.assertEqual([(e["no"], e["kind"], e["tag"]) for e in order],
                         [(1, "character", "C09"), (2, "character", "C02"), (3, "character", "C03"), (4, "scene", "S01"), (5, "prop", "P03")])

    def test_same_asset_in_several_cuts_counts_once_and_scene_goes_after_characters(self):
        seg = {"id": "E01-02", "cuts": [
            {"assetRefs": [{"id": "character:C02", "kind": "character", "name": "燕溪山"}, {"id": "scene:S01", "kind": "scene", "name": "S01"}]},
            {"assetRefs": [{"id": "character:C03", "kind": "character", "name": "徐今朝"}, {"id": "scene:S01", "kind": "scene", "name": "S01"},
                           {"id": "character:C02", "kind": "character", "name": "燕溪山"}]},
        ]}
        order = rt.build_ref_order(seg)
        self.assertEqual([(e["no"], e["tag"]) for e in order], [(1, "C02"), (2, "C03"), (3, "S01")])

    def test_four_assets_do_not_all_become_picture_1(self):
        """原 bug：四个素材的提示词里全是 <Picture 1>。"""
        prompt = ("subject_definitions:\n<Picture C02>: swordsman\n<Picture C03>: sister\n<Picture C09>: villagers\n<Picture S01>: altar\n"
                  "detailed_description:\n[Shot 1] <Picture S01> at night, <Picture C02> and <Picture C03> stand, <Picture C09> push.")
        out, unresolved = rt.resolve_placeholders(prompt, rt.build_ref_order(_segment()))
        self.assertEqual(unresolved, [])
        self.assertEqual(rt.picture_numbers(out), [1, 2, 3, 4])
        self.assertIn("<Picture 2>: swordsman", out)   # 燕溪山 = 第 2 张
        self.assertIn("<Picture 3>: sister", out)      # 徐今朝 = 第 3 张
        self.assertIn("<Picture 4>: altar", out)       # 场景 = 第 4 张

    def test_placeholder_by_name_case_insensitive_and_unknown_kept(self):
        order = rt.build_ref_order(_segment())
        out, unresolved = rt.resolve_placeholders("<Picture 燕溪山> <picture c03> <Picture C99>", order)
        self.assertEqual(out, "<Picture 2> <Picture 3> <Picture C99>")
        self.assertEqual(unresolved, ["C99"])

    def test_digit_tags_are_left_alone_by_resolver(self):
        out, unresolved = rt.resolve_placeholders("<Picture 1> <Picture 2>", rt.build_ref_order(_segment()))
        self.assertEqual((out, unresolved), ("<Picture 1> <Picture 2>", []))


class CollageTest(unittest.TestCase):
    def test_remap_is_single_pass_not_chained(self):
        self.assertEqual(rt.remap_picture_tags("<Picture 1> <Picture 2> <Picture 3>", {1: 2, 2: 3, 3: 4}), "<Picture 2> <Picture 3> <Picture 4>")

    def test_mapping_without_overflow(self):
        self.assertEqual(rt.collage_mapping(4, 6), {1: 1, 2: 2, 3: 3, 4: 4})
        self.assertEqual(rt.collage_mapping(4, 6, front=1), {1: 2, 2: 3, 3: 4, 4: 5})

    def test_mapping_overflow_merges_tail_into_last_slot(self):
        # 8 张、最多 6 张：前 5 张原样，第 6~8 张拼成第 6 位
        self.assertEqual(rt.collage_mapping(8, 6), {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 6, 8: 6})
        # 前面多了上一镜帧：原 1~4 后移，第 5~8 张全进拼图
        self.assertEqual(rt.collage_mapping(7, 6, front=1), {1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 6, 7: 6})

    def test_mapping_matches_plan_references(self):
        """映射必须和 plan_references 实际保留 / 拼接的图一致。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            paths = []
            for i in range(8):
                p = td / f"{i}.png"
                Image.new("RGB", (20, 20), (i * 20, 0, 0)).save(p)
                paths.append(("character", p))
            planned = vc.plan_references(paths, 6, td / "c.png")
            mapping = rt.collage_mapping(8, 6)
            self.assertEqual(len(planned), max(mapping.values()))
            self.assertEqual(planned[-1][0], "collage")
            for old, new in mapping.items():
                if new < 6:
                    self.assertEqual(planned[new - 1][1], paths[old - 1][1])


class VideoStageNumberingTest(unittest.TestCase):
    def _ctx(self, td: Path, segment: dict):
        out = td / "output"
        board = out / "storyboard" / "Demo-storyboard.json"
        board.parent.mkdir(parents=True)
        board.write_text(json.dumps({"source": "Demo", "aspectRatio": "9:16", "videoModel": "comfyui_h3",
                                     "episodes": [{"ep": 1, "segments": [segment]}]}), encoding="utf-8")
        images = []
        for ref in ASSET_REFS:
            rel = f"images/{ref['name']}.png"
            (out / "images").mkdir(exist_ok=True)
            Image.new("RGB", (20, 20), (9, 9, 9)).save(out / rel)
            images.append({"kind": ref["kind"], "name": ref["name"], "asset_id": ref["id"].split(":")[1], "output_path": rel})
        (out / "images" / "image-manifest.json").write_text(json.dumps({"items": images}), encoding="utf-8")
        return SimpleNamespace(out=out, title="Demo", force=False,
                               artifact=lambda name: board if name == "storyboard" else out / f"{name}.json", log=lambda m: None)

    def test_collect_numbers_references_by_refOrder(self):
        seg = _segment(videoPrompt="x", h3Prompt="x")
        seg["refOrder"] = rt.build_ref_order(seg)
        with tempfile.TemporaryDirectory() as td:
            ctx = self._ctx(Path(td), seg)
            manifest = video._collect(ctx, [1], "h3", "manju")
        item = manifest["items"][0]
        self.assertEqual(item["reference_numbering"], "refOrder")
        self.assertEqual([(r["no"], r["name"]) for r in item["reference_assets_all"]],
                         [(1, "江边村民"), (2, "燕溪山"), (3, "徐今朝"), (4, "S01")])
        self.assertEqual(item["reference_missing"], [])

    def test_missing_image_is_reported_instead_of_shifting_numbers(self):
        seg = _segment(videoPrompt="x", h3Prompt="x")
        seg["refOrder"] = rt.build_ref_order(seg)
        with tempfile.TemporaryDirectory() as td:
            ctx = self._ctx(Path(td), seg)
            (ctx.out / "images" / "燕溪山.png").unlink()  # 第 2 张缺图
            manifest = video._collect(ctx, [1], "h3", "manju")
        item = manifest["items"][0]
        self.assertEqual([r["no"] for r in item["reference_assets_all"]], [1, 3, 4])  # 不让第 3 张顶成第 2 张
        self.assertEqual(len(item["reference_missing"]), 1)
        self.assertIn("<Picture 2>", item["reference_missing"][0])

    def test_legacy_storyboard_without_refOrder_keeps_old_behavior(self):
        seg = _segment(videoPrompt="x", h3Prompt="x")
        with tempfile.TemporaryDirectory() as td:
            ctx = self._ctx(Path(td), seg)
            manifest = video._collect(ctx, [1], "h3", "manju")
        self.assertEqual(manifest["items"][0]["reference_numbering"], "legacy")


class SubmitNumberingTest(unittest.TestCase):
    """真正 POST 给 ComfyUI 的提示词和上传顺序。"""

    def setUp(self):
        _Handler.state = {}
        self.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _run(self, td: Path, assets: list, prompt: str, numbering: str = "refOrder", previous_last=None, max_refs=6):
        out = td / "output"
        (out / "art").mkdir(parents=True)
        for i, a in enumerate(assets):
            Image.new("RGB", (20, 20), (i * 25, 0, 0)).save(out / "art" / f"a{i}.png")
            a["path"] = f"art/a{i}.png"
        item = {"id": "E01-01-C01", "duration_seconds": 6, "prompt": prompt, "reference_assets_all": assets,
                "reference_numbering": numbering, "input_mode": "scene_or_assets"}
        cfg = {"base_url": self.base, "workflow_ref": str(REF_WF), "max_refs": max_refs, "poll": 0.01, "max_wait": 10, "size_preset": "360p"}
        logs = []
        handle = vc.submit(item, {"aspect_ratio": "16:9"}, cfg, out, out / "video" / "work", previous_last, out / "video" / "v", log=logs.append)
        return _Handler.state["prompt"]["h3"]["inputs"]["prompt"], handle, logs

    def test_four_assets_keep_their_own_numbers_in_posted_prompt(self):
        """用户的真实场景：村民=1 燕溪山=2 徐今朝=3 祭坛=4，提示词不能全是 Picture 1。"""
        assets = [{"kind": "character", "name": "江边村民", "no": 1}, {"kind": "character", "name": "燕溪山", "no": 2},
                  {"kind": "character", "name": "徐今朝", "no": 3}, {"kind": "scene", "name": "江边祭坛", "no": 4}]
        prompt = ("subject_definitions:\n<Picture 2>: swordsman\n<Picture 3>: sister\n<Picture 1>: villagers\n<Picture 4>: altar\n"
                  "detailed_description:\n[Shot 1] <Picture 4> altar, <Picture 2> and <Picture 3> stand, <Picture 1> push.")
        with tempfile.TemporaryDirectory() as td:
            sent, _handle, _logs = self._run(Path(td), assets, prompt)
            self.assertEqual(len(_Handler.state["uploads"]), 4)
        self.assertEqual(sent, prompt)  # 没有拼图、没有上一镜帧：编号原样
        self.assertEqual(rt.picture_numbers(sent), [1, 2, 3, 4])

    def test_previous_frame_shifts_numbers_by_one(self):
        assets = [{"kind": "character", "name": "燕溪山", "no": 1}, {"kind": "scene", "name": "祭坛", "no": 2}]
        with tempfile.TemporaryDirectory() as td:
            prev = Path(td) / "prev.png"
            Image.new("RGB", (20, 20), (1, 1, 1)).save(prev)
            sent, _h, _l = self._run(Path(td), assets, "<Picture 1> swordsman at <Picture 2> altar", previous_last=prev)
            self.assertEqual(len(_Handler.state["uploads"]), 3)
        self.assertIn("<Picture 2> swordsman at <Picture 3> altar", sent)

    def test_overflow_collage_remaps_tail_to_collage_slot(self):
        kinds = ["character"] * 4 + ["scene"] + ["prop"] * 3   # 8 张，最多 6 张：第 6~8 张拼成第 6 位
        assets = [{"kind": k, "name": f"n{i}", "no": i + 1} for i, k in enumerate(kinds)]
        prompt = " ".join(f"<Picture {i}>" for i in range(1, 9))
        with tempfile.TemporaryDirectory() as td:
            sent, _h, _l = self._run(Path(td), assets, prompt)
            self.assertEqual(len(_Handler.state["uploads"]), 6)
        self.assertEqual(rt.picture_numbers(sent.split("\n")[-1]), [1, 2, 3, 4, 5, 6])  # 没有 7、8
        self.assertTrue(sent.endswith("<Picture 1> <Picture 2> <Picture 3> <Picture 4> <Picture 5> <Picture 6> <Picture 6> <Picture 6>"))

    def test_legacy_numbering_is_not_remapped(self):
        kinds = ["character"] * 4 + ["scene"] + ["prop"] * 3
        assets = [{"kind": k, "name": f"n{i}", "no": i + 1} for i, k in enumerate(kinds)]
        prompt = "<Picture 7> <Picture 8>"
        with tempfile.TemporaryDirectory() as td:
            sent, _h, logs = self._run(Path(td), assets, prompt, numbering="legacy")
        self.assertTrue(sent.endswith("<Picture 7> <Picture 8>"))
        self.assertTrue(any("旧格式" in line for line in logs))

    def test_missing_reference_blocks_submit(self):
        out_item = {"id": "E01-01-C01", "duration_seconds": 6, "prompt": "p", "reference_assets_all": [],
                    "reference_missing": ["<Picture 2> 的character「燕溪山」没有参考图"]}
        cfg = {"base_url": self.base, "workflow_ref": str(REF_WF), "poll": 0.01, "max_wait": 10}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(vc.ComfyError) as cm:
                vc.submit(out_item, {}, cfg, Path(td), Path(td) / "w", None, Path(td) / "v")
        self.assertIn("编号会错位", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
