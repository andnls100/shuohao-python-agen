import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zhipu_agent.stages import video


class VideoContinuityTest(unittest.TestCase):
    def _ctx(self, board):
        td = tempfile.TemporaryDirectory()
        out = Path(td.name) / "output"
        (out / "storyboard").mkdir(parents=True)
        (out / "storyboard" / "T-storyboard.json").write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
        ctx = SimpleNamespace(out=out, title="T", force=False,
                              artifact=lambda n: out / "storyboard" / "T-storyboard.json" if n == "storyboard" else out / n,
                              log=lambda *_: None)
        ctx._td = td
        return ctx

    def test_only_explicit_inherit_to_immediately_previous_shot(self):
        board = {"aspectRatio": "16:9", "episodes": [{"ep": 1, "segments": [
            {"id": "E01-01", "sceneIndex": 1, "cuts": [
                {"seconds": 3},
                {"seconds": 3, "continuity": {"mode": "inherit", "referenceFrame": "E01-01/f1.png"}},
            ]},
            {"id": "E01-02", "sceneIndex": 1, "continuityMode": "hard_cut", "cuts": [
                {"seconds": 3, "continuity": {"mode": "inherit", "referenceFrame": "E01-01/f2.png"}},
            ]},
            {"id": "E01-03", "sceneIndex": 2, "continuityMode": "continuous", "cuts": [
                {"seconds": 3, "continuity": {"mode": "inherit", "referenceFrame": "E01-02/f1.png"}},
            ]},
        ]}]}
        ctx = self._ctx(board)
        with mock.patch.object(video, "_reference_assets", return_value=[]):
            items = video._collect(ctx, None, "seedance", "live-action")["items"]
        self.assertEqual(items[0]["input_mode"], "scene_or_assets")
        self.assertEqual(items[1]["input_mode"], "previous_last_frame")
        self.assertEqual(items[1]["previous_shot_id"], "E01-01-C01")
        self.assertEqual(items[2]["input_mode"], "scene_or_assets")
        self.assertEqual(items[3]["input_mode"], "scene_or_assets")

    def test_cross_segment_requires_continuous_and_previous_reference(self):
        board = {"episodes": [{"ep": 1, "segments": [
            {"id": "E01-01", "sceneIndex": 1, "cuts": [{"seconds": 3}]},
            {"id": "E01-02", "sceneIndex": 1, "continuityMode": "continuous", "cuts": [
                {"seconds": 3, "continuity": {"mode": "inherit", "referenceFrame": "E01-01/f1.png"}},
            ]},
        ]}]}
        ctx = self._ctx(board)
        with mock.patch.object(video, "_reference_assets", return_value=[]):
            item = video._collect(ctx, None, "seedance", "live-action")["items"][1]
        self.assertEqual(item["input_mode"], "previous_last_frame")


if __name__ == "__main__":
    unittest.main()
