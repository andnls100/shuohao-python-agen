import base64
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from zhipu_agent.stages.images import (
    _character_prompt, _extension, _find_image_payload, _load_looks, _pick_look, _prompt_for,
)

SKILLS = Path(__file__).resolve().parent.parent / "skills"
PORTRAIT = {"character_mode": "portrait", "negative": "omit", "look": ""}


class ImageAssetHelpersTest(unittest.TestCase):
    def test_detects_png_jpeg_and_webp(self):
        self.assertEqual(_extension(b"\x89PNG\r\n\x1a\nrest"), ".png")
        self.assertEqual(_extension(b"\xff\xd8\xffrest"), ".jpg")
        self.assertEqual(_extension(b"RIFF0000WEBPrest"), ".webp")

    def test_reads_openai_compatible_image_payload(self):
        self.assertEqual(
            _find_image_payload({"data": [{"b64_json": "abc"}]}),
            ("abc", None),
        )
        self.assertEqual(
            _find_image_payload({"data": [{"url": "https://images.example/a.png"}]}),
            (None, "https://images.example/a.png"),
        )

    def test_prefers_saved_sheet_prompt_and_appends_negative_prompt(self):
        prompt, negative = _prompt_for({
            "name": "渡口",
            "image": {
                "prompt": "single view",
                "sheet": "saved reference sheet prompt",
                "negativePrompt": "people, text",
            },
        }, "场景")
        self.assertIn("saved reference sheet prompt", prompt)
        self.assertIn("Avoid the following visual elements: people, text", prompt)
        self.assertEqual(negative, "people, text")

    def test_missing_prompt_is_reported(self):
        with self.assertRaisesRegex(ValueError, "没有 image.sheet 或 image.prompt"):
            _prompt_for({"name": "缺提示词", "image": {}}, "角色")


class CharacterLookTest(unittest.TestCase):
    """角色提示词 = 人物描述 + character-refs 的画风层（默认写实照片）。"""

    @classmethod
    def setUpClass(cls):
        cls.table = _load_looks(SKILLS)
        cls.photo = _pick_look(cls.table)
        cls.record = {"name": "老周", "image": {
            "prompt": "An elderly ferryman, three-quarter view, soft key light, ambient occlusion under the chin, "
                      "subsurface scattering at the ear edge.",
            "sheet": "Single character model sheet on ONE 16:9 landscape canvas, three zones.",
            "negativePrompt": "plastic skin, doll face",
        }}

    def test_default_look_is_realistic_photo_from_character_refs(self):
        self.assertEqual(self.photo["id"], "realistic-photo")
        self.assertEqual(_pick_look(self.table, "动漫")["id"], "anime")
        with self.assertRaisesRegex(ValueError, "没有这个画风"):
            _pick_look(self.table, "水彩")

    def test_portrait_uses_single_prompt_not_sheet_and_adds_photo_style(self):
        prompt, negative = _character_prompt(self.record, PORTRAIT, self.photo)
        self.assertIn("An elderly ferryman", prompt)
        self.assertNotIn("model sheet", prompt)
        self.assertIn("A real photograph, not a render", prompt)
        self.assertIn("Exactly one person", prompt)
        self.assertNotIn("Avoid the following", prompt)   # 默认不发负面词
        self.assertEqual(negative, "")

    def test_photo_look_replaces_render_engine_terms(self):
        prompt, _ = _character_prompt(self.record, PORTRAIT, self.photo)
        self.assertNotIn("ambient occlusion", prompt)
        self.assertNotIn("subsurface scattering", prompt)
        self.assertIn("soft contact shadow", prompt)
        anime = _pick_look(self.table, "anime")
        drawn, _ = _character_prompt(self.record, PORTRAIT, anime)
        self.assertIn("ambient occlusion", drawn)           # 只改写实画风，动漫不动

    def test_sheet_mode_keeps_sheet_and_appends_style(self):
        prompt, _ = _character_prompt(self.record, {**PORTRAIT, "character_mode": "sheet"}, self.photo)
        self.assertTrue(prompt.startswith("Single character model sheet"))
        self.assertIn("A real photograph, not a render", prompt)

    def test_negative_append_merges_character_and_look_negatives(self):
        prompt, negative = _character_prompt(self.record, {**PORTRAIT, "negative": "append"}, self.photo)
        self.assertIn("Avoid the following visual elements: plastic skin, doll face", prompt)
        self.assertIn("CGI", prompt)
        self.assertIn("CGI", negative)

    def test_design_sheet_lead_is_stripped_in_portrait_mode(self):
        rec = {"name": "x", "image": {"prompt": "Character design sheet of an elderly Chinese ferryman, weathered face"}}
        prompt, _ = _character_prompt(rec, PORTRAIT, self.photo)
        self.assertTrue(prompt.startswith("An elderly Chinese ferryman"), prompt[:60])


PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()


class OnlyFilterTest(unittest.TestCase):
    """--images-only：只请求被选中的那一张，其余保持待生成；用本地假接口，不联网。"""

    def setUp(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                payload = json.dumps({"data": [{"b64_json": PNG}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-test-images-"))
        models = self.tmp / "models.json"
        models.write_text(json.dumps({"image": [{
            "name": "fake", "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
            "api_key": "k", "model": "fake-image", "size": "1024x1024"}]}), encoding="utf-8")
        self.old_env = os.environ.get("AGENT_MODELS_FILE")
        os.environ["AGENT_MODELS_FILE"] = str(models)
        self.out = self.tmp / "out"
        (self.out / "characters").mkdir(parents=True)
        (self.out / "art").mkdir(parents=True)
        shutil.copy(SKILLS / "novel-characters/examples/渡口-cast.json", self.out / "characters" / "渡口-cast.json")
        shutil.copy(SKILLS / "novel-art/examples/渡口-art.json", self.out / "art" / "渡口-art.json")

    def tearDown(self):
        self.server.shutdown()
        if self.old_env is None:
            os.environ.pop("AGENT_MODELS_FILE", None)
        else:
            os.environ["AGENT_MODELS_FILE"] = self.old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, only):
        from zhipu_agent import workflow
        from zhipu_agent.config import load_settings
        ctx = workflow.build_context(load_settings(api_key="x", model="y"),
                                     SKILLS / "novel-characters/examples/渡口.txt", self.out, interactive=False)
        workflow.run_pipeline(ctx, ["images"], {"params": {}, "images_gen": True, "images_only": only})
        return json.loads((self.out / "images" / "image-manifest.json").read_text(encoding="utf-8"))

    def test_only_one_character_is_requested(self):
        manifest = self._run(["沈知微"])
        self.assertEqual(len(self.requests), 1)
        self.assertIn("A real photograph", self.requests[0]["prompt"])
        states = {item["name"]: item["state"] for item in manifest["items"]}
        self.assertEqual(states["沈知微"], "succeeded")
        self.assertTrue(all(v == "pending" for k, v in states.items() if k != "沈知微"), states)
        self.assertGreater(len(states), 1)

        # 再出另一张：第一张保持已完成，不重复请求
        manifest = self._run(["陆行远"])
        self.assertEqual(len(self.requests), 2)
        states = {item["name"]: item["state"] for item in manifest["items"]}
        self.assertEqual((states["沈知微"], states["陆行远"]), ("succeeded", "succeeded"))

    def test_unknown_name_lists_available_choices(self):
        with self.assertRaisesRegex(ValueError, "找不到对应的图.*沈知微"):
            self._run(["不存在的人"])
        self.assertEqual(self.requests, [])


PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()


class OnlyFilterTest(unittest.TestCase):
    """--images-only：只请求被选中的那几张，其余保持待生成；用本地假接口，不联网。"""

    def setUp(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                payload = json.dumps({"data": [{"b64_json": PNG}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-test-images-"))
        models = self.tmp / "models.json"
        # 故意只写 image、不写 text：文本模型读 .env，models.json 可以只放图片 / 视频
        models.write_text(json.dumps({"image": [{
            "name": "fake", "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
            "api_key": "k", "model": "fake-image", "size": "1024x1024"}]}), encoding="utf-8")
        self.old_env = os.environ.get("AGENT_MODELS_FILE")
        os.environ["AGENT_MODELS_FILE"] = str(models)
        self.out = self.tmp / "out"
        (self.out / "characters").mkdir(parents=True)
        (self.out / "art").mkdir(parents=True)
        shutil.copy(SKILLS / "novel-characters/examples/渡口-cast.json", self.out / "characters" / "渡口-cast.json")
        shutil.copy(SKILLS / "novel-art/examples/渡口-art.json", self.out / "art" / "渡口-art.json")

    def tearDown(self):
        self.server.shutdown()
        if self.old_env is None:
            os.environ.pop("AGENT_MODELS_FILE", None)
        else:
            os.environ["AGENT_MODELS_FILE"] = self.old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, only, force=False):
        from zhipu_agent import workflow
        from zhipu_agent.config import load_settings
        ctx = workflow.build_context(load_settings(api_key="x", model="y"),
                                     SKILLS / "novel-characters/examples/渡口.txt", self.out,
                                     interactive=False, force=force)
        workflow.run_pipeline(ctx, ["images"], {"params": {}, "images_gen": True, "images_only": only})
        manifest = json.loads((self.out / "images" / "image-manifest.json").read_text(encoding="utf-8"))
        return {item["name"]: item["state"] for item in manifest["items"]}

    def test_only_selected_images_are_requested(self):
        states = self._run(["沈知微"])
        self.assertEqual(len(self.requests), 1)
        self.assertIn("A real photograph", self.requests[0]["prompt"])
        self.assertEqual(states["沈知微"], "succeeded")
        self.assertGreater(len(states), 1)
        self.assertTrue(all(v == "pending" for k, v in states.items() if k != "沈知微"), states)

        # 生图阶段应自动刷新两个报告，并从 manifest 引用实际输出路径。
        character_report = (self.out / "characters" / "report.html").read_text(encoding="utf-8")
        art_report = (self.out / "art" / "art-report.html").read_text(encoding="utf-8")
        self.assertIn("../images/characters/沈知微-C001-sheet.png", character_report)
        self.assertIn("尚未出图", character_report)  # 未生成的角色仍显示占位
        self.assertIn("尚未出图", art_report)  # 未生成的场景/道具仍显示占位

        states = self._run(["陆行远"])          # 再出另一张：第一张保持已完成，不重复请求
        self.assertEqual(len(self.requests), 2)
        self.assertEqual((states["沈知微"], states["陆行远"]), ("succeeded", "succeeded"))

    def test_asset_id_works_and_force_only_resets_selected(self):
        self._run(["沈知微", "C002"])
        self.assertEqual(len(self.requests), 2)
        states = self._run(["沈知微"], force=True)
        self.assertEqual(len(self.requests), 3)                      # 只重出被选中的
        self.assertEqual(states["陆行远"], "succeeded")              # 另一张没被 --force 重置

    def test_unknown_name_lists_available_choices(self):
        with self.assertRaisesRegex(ValueError, "找不到对应的图.*沈知微"):
            self._run(["不存在的人"])
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
