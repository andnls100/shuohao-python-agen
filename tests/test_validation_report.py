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


if __name__ == "__main__":
    unittest.main()
