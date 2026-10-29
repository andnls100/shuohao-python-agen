import tempfile, unittest
from pathlib import Path
from zhipu_agent.stages.storyboard import _write_validation_report


class Ctx:
    def __init__(self): self.logs = []
    def log(self, m): self.logs.append(m)


class ReportTest(unittest.TestCase):
    def test_dedup_and_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d); c = Ctx()
            _write_validation_report(c, d, ["## a\n\nX错\nY错", "## 全剧\n\nX错\nZ错"])
            t = (d / "storyboard-validation-errors.md").read_text(encoding="utf-8")
            self.assertEqual(t.count("X错"), 1)
            self.assertIn("Z错", t)
            _write_validation_report(c, d, [])
            t = (d / "storyboard-validation-errors.md").read_text(encoding="utf-8")
            self.assertNotIn("X错", t)



class FamilyTest(unittest.TestCase):
    def test_prompt_family(self):
        from zhipu_agent.workflow import prompt_family
        self.assertEqual(prompt_family("comfyui_h3_1"), "comfyui_h3")
        self.assertEqual(prompt_family("comfyui_h3"), "comfyui_h3")
        self.assertEqual(prompt_family("comfyui_wan_2"), "comfyui_wan")
        self.assertEqual(prompt_family("wan_r2v"), "wan_r2v")
        self.assertIsNone(prompt_family(None))


if __name__ == "__main__":
    unittest.main()
