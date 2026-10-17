import unittest

from zhipu_agent.stages.images import _extension, _find_image_payload, _prompt_for


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


if __name__ == "__main__":
    unittest.main()
