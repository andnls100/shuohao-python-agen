import unittest

from zhipu_agent.stages.storyboard_repair import (
    cut_violation_refs,
    extract_h3_cut_text,
    repair_minor_cut_durations,
    replace_h3_cut_text,
    sync_segment_timing,
)


class StoryboardRepairTest(unittest.TestCase):
    def _segment(self):
        return {
            "id": "E01-10",
            "cuts": [{"seconds": 3.0}, {"seconds": 5.1}, {"seconds": 3.0}],
            "h3Prompt": (
                "How the reference pictures align with the target video — "
                "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
                "Picture 2 (from Shot 2) aligns with the 3.00-second mark of the target video; "
                "Picture 3 (from Shot 3) aligns with the 8.10-second mark of the target video.\n"
                "integrated_multimodal_description:\n"
                "[Shot 1] first shot\n[Shot 2] At 00:03.000, second shot\n"
                "[Shot 3] At 00:08.100, third shot\n"
                "overall_soundscape: sound\nnon_diegetic_music: N/A"
            ),
        }

    def test_minor_overrun_is_snapped_and_alignment_rebuilt(self):
        doc = {"episodes": [{"segments": [self._segment()]}]}
        fixed = repair_minor_cut_durations(doc, "en")
        segment = doc["episodes"][0]["segments"][0]
        self.assertEqual(fixed, ["E01-10#2: 5.1s -> 5.0s"])
        self.assertEqual(segment["cuts"][1]["seconds"], 5.0)
        self.assertIn("8.00-second mark", segment["h3Prompt"])
        self.assertIn("[Shot 3] At 00:08.000,", segment["h3Prompt"])

    def test_zh_alignment_is_rebuilt(self):
        segment = {
            "cuts": [{"seconds": 3.0}, {"seconds": 5.1}],
            "h3Prompt": "旧对齐行\n整体视听描述：\n[镜头 1] 第一镜\n[镜头 2] 于 00:03.000，第二镜\n整体音景：声音",
        }
        segment["cuts"][1]["seconds"] = 5.0
        self.assertTrue(sync_segment_timing(segment, "zh"))
        self.assertTrue(segment["h3Prompt"].startswith("参考图与目标视频的对齐——"))
        self.assertIn("[镜头 2] 于 00:03.000，", segment["h3Prompt"])

    def test_only_explicit_cut_errors_are_routed(self):
        found = cut_violation_refs([
            "质量门未过：E01-10#4 5.1 秒",
            "第 1 集总时长超出目标",
            "质量门未过：E02-03#1 缺少景别",
        ])
        self.assertEqual(set(found), {("E01-10", 4), ("E02-03", 1)})

    def test_replace_extracts_one_shot_without_touching_neighbors(self):
        segment = self._segment()
        old = extract_h3_cut_text(segment, 2, "en")
        self.assertIn("second shot", old)
        self.assertNotIn("third shot", old)
        self.assertTrue(replace_h3_cut_text(segment, 2, "[Shot 2] At 00:03.000, repaired shot", "en"))
        self.assertIn("repaired shot", segment["h3Prompt"])
        self.assertIn("[Shot 3] At 00:08.100, third shot", segment["h3Prompt"])


if __name__ == "__main__":
    unittest.main()
