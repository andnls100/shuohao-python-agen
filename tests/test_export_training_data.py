import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("export_training_data", Path(__file__).resolve().parents[1] / "scripts" / "export_training_data.py")
etd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(etd)

GOOD = ("subject_definitions:\n<Picture 1> is the heroine.\nsummary:\nx\nretention_analysis:\n<Picture 1> kept.\n"
        "detailed_description:\n[Shot 1] <Picture 1> runs. <d>[Chinese] 等等我！</d>\noverall_soundscape:\nwind\nnon_diegetic_music:\nnone")


def segment(seg_id, prompt):
    return {"id": seg_id, "sceneIndex": 1, "refOrder": [{"no": 1, "kind": "character", "name": "沈知微", "tag": "C01", "id": "character:C01"}],
            "cuts": [{"seconds": 5, "size": "wide", "camera": "Static Shot", "characters": ["C01"], "props": [], "frame": "f", "shot": "s",
                      "assetRefs": [{"id": "character:C01"}], "beats": [1, 1]}],
            "h3Prompt": prompt, "videoPrompt": prompt, "soundscape": "wind"}


class ExportTest(unittest.TestCase):
    def test_keeps_good_drops_bad_and_formats(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "output"
            (root / "storyboard").mkdir(parents=True)
            doc = {"episodes": [{"ep": 1, "segments": [
                segment("E01-01", GOOD),
                segment("E01-02", "no sections here <Picture C01>"),
                segment("E01-03", GOOD.replace("<Picture 1> runs", "<Picture 3> runs")),
            ]}]}
            (root / "storyboard" / "T-storyboard.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            samples, stats = etd.collect([root], "prompt", etd.PROMPT_SYSTEM, False, lambda *_: None)
            self.assertEqual((stats["kept"], stats["dropped_local"]), (1, 2))
            row = etd.render(samples[0], "messages")
            self.assertEqual([m["role"] for m in row["messages"]], ["system", "user", "assistant"])
            user = json.loads(row["messages"][1]["content"])
            self.assertEqual(user["refOrder"][0]["no"], 1)
            self.assertNotIn("assetRefs", json.dumps(user))  # 内部字段不进训练输入
            self.assertEqual(row["messages"][2]["content"], GOOD)
            alpaca = etd.render(samples[0], "alpaca")
            self.assertEqual(set(alpaca), {"instruction", "input", "output"})

    def test_validator_flagged_segments_and_dedupe(self):
        segs, eps = etd.flagged_ids("质量门未过：E01-02 缺少章节；E03 总时长超限")
        self.assertEqual(segs, {"E01-02"})
        self.assertEqual(eps, {3})


if __name__ == "__main__":
    unittest.main()
