import unittest
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


if __name__ == "__main__":
    unittest.main()
