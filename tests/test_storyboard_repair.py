import math
import unittest

from zhipu_agent.stages.storyboard_repair import (
    balance_episode_durations,
    cut_violation_refs,
    extract_h3_cut_text,
    repair_deterministic_storyboard,
    repair_dialogue_fit,
    repair_minor_cut_durations,
    segment_h3_violation_refs,
    replace_h3_cut_text,
    sync_segment_timing,
)


def _fit_fixture(line: str, seconds: float, max_cut=None):
    segment = {
        "id": "E01-01", "sceneIndex": 1,
        "cuts": [{"seconds": seconds, "beats": [1, 1]}, {"seconds": 3.0, "beats": [2, 2]}],
        "h3Prompt": (
            "How the reference pictures align with the target video — "
            "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
            "Picture 2 (from Shot 2) aligns with the %.2f-second mark of the target video.\n"
            "[Shot 1] a\n[Shot 2] At 00:%06.3f, b" % (seconds, seconds)
        ),
    }
    doc = {"episodes": [{"ep": 1, "segments": [segment]}]}
    if max_cut is not None:
        doc["params"] = {"maxCutSeconds": max_cut}
    script = {"episodes": [{"ep": 1, "scenes": [{"flow": [{"line": line}, {"action": "x"}]}]}]}
    return doc, script, segment


class DialogueFitRepairTest(unittest.TestCase):
    def test_cut_is_lengthened_to_dialogue_seconds_and_timing_resynced(self):
        # 23 字 / 4.5 = 5.11 -> 5.1 秒，分镜 5 秒装不下
        doc, script, segment = _fit_fixture("一二三四五六七八九十一二三四五六七八九十一二三", 5.0)
        fixed = repair_dialogue_fit(doc, script, "en")
        self.assertEqual(segment["cuts"][0]["seconds"], 5.1)
        self.assertEqual(len(fixed), 1)
        self.assertIn("5.10-second mark", segment["h3Prompt"])
        self.assertIn("At 00:05.100", segment["h3Prompt"])

    def test_default_cap_is_ten_not_five(self):
        doc, script, segment = _fit_fixture("一二三四五六七八九十一二三四五六七八九十一二三", 5.0)
        self.assertEqual(len(repair_dialogue_fit(doc, script, "en")), 1)
        # 5.1s 的分镜在默认（10 秒）上限下不再被吞成 5.0
        doc2 = {"episodes": [{"segments": [{"cuts": [{"seconds": 5.1}], "h3Prompt": "x"}]}]}
        self.assertEqual(repair_minor_cut_durations(doc2, "en"), [])

    def test_dialogue_longer_than_cap_is_left_for_the_model(self):
        doc, script, segment = _fit_fixture("字" * 60, 5.0, max_cut=10)  # 60/4.5 = 13.3 秒
        self.assertEqual(repair_dialogue_fit(doc, script, "en"), [])
        self.assertEqual(segment["cuts"][0]["seconds"], 5.0)

    def test_fitting_cut_is_untouched(self):
        doc, script, segment = _fit_fixture("短句", 3.0)
        self.assertEqual(repair_dialogue_fit(doc, script, "en"), [])


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
        doc = {"params": {"maxCutSeconds": 5}, "episodes": [{"segments": [self._segment()]}]}
        fixed = repair_minor_cut_durations(doc, "en")
        segment = doc["episodes"][0]["segments"][0]
        self.assertEqual(fixed, ["E01-10#2: 5.1s -> 5.0s"])
        self.assertEqual(segment["cuts"][1]["seconds"], 5.0)
        self.assertIn("8.00-second mark", segment["h3Prompt"])
        self.assertIn("[Shot 3] At 00:08.000,", segment["h3Prompt"])

    def test_alignment_rounding_matches_javascript_math_round(self):
        # JS Math.round(0.25 * 10) / 10 is 0.3; Python round(0.25, 1) is 0.2.
        segment = {
            "cuts": [{"seconds": 0.25}, {"seconds": 3.0}],
            "h3Prompt": (
                "How the reference pictures align with the target video — stale\n"
                "integrated_multimodal_description:\n"
                "[Shot 1] first\n[Shot 2] At 00:00.250, second\n"
                "overall_soundscape: sound\nnon_diegetic_music: N/A"
            ),
        }
        self.assertTrue(sync_segment_timing(segment, "en"))
        self.assertIn("Picture 2 (from Shot 2) aligns with the 0.30-second mark", segment["h3Prompt"])
        self.assertIn("[Shot 2] At 00:00.300,", segment["h3Prompt"])

    def test_alignment_rebuild_handles_leading_blank_lines(self):
        segment = {
            "cuts": [{"seconds": 3.0}, {"seconds": 4.0}],
            "h3Prompt": "\n\n旧对齐行\n整体视听描述：\n[镜头 1] 第一镜\n[镜头 2] 于 00:03.000，第二镜\n整体音景：声音",
        }
        self.assertTrue(sync_segment_timing(segment, "zh"))
        self.assertTrue(segment["h3Prompt"].startswith("参考图与目标视频的对齐——"))
        self.assertFalse(segment["h3Prompt"].startswith("\n"))

    def test_zh_alignment_is_rebuilt(self):
        segment = {
            "cuts": [{"seconds": 3.0}, {"seconds": 5.1}],
            "h3Prompt": "旧对齐行\n整体视听描述：\n[镜头 1] 第一镜\n[镜头 2] 于 00:03.000，第二镜\n整体音景：声音",
        }
        segment["cuts"][1]["seconds"] = 5.0
        self.assertTrue(sync_segment_timing(segment, "zh"))
        self.assertTrue(segment["h3Prompt"].startswith("参考图与目标视频的对齐——"))
        self.assertIn("[镜头 2] 于 00:03.000，", segment["h3Prompt"])


    def test_missing_shot_block_is_inserted_without_losing_neighbours(self):
        segment = {
            "h3Prompt": (
                "How the reference pictures align with the target video — line\n"
                "integrated_multimodal_description:\n"
                "[Shot 1] first\n[Shot 3] At 00:06.000, third\n"
                "overall_soundscape: sound\nnon_diegetic_music: N/A"
            )
        }
        self.assertTrue(replace_h3_cut_text(segment, 2, "[Shot 2] At 00:03.000, repaired second", "en"))
        self.assertIn("[Shot 2] At 00:03.000, repaired second", segment["h3Prompt"])
        self.assertLess(segment["h3Prompt"].index("[Shot 2]"), segment["h3Prompt"].index("[Shot 3]"))
        self.assertIn("[Shot 1] first", segment["h3Prompt"])

    def test_frame_size_and_alignment_repairs_are_deterministic(self):
        doc = {
            "promptLang": "zh",
            "episodes": [{"segments": [{
                "id": "E01-01",
                "cuts": [{"seconds": 3.0, "size": "close", "frame": "人物面部占据画面中心"}],
                "h3Prompt": "旧对齐行\n整体视听描述：\n[镜头 1] 人物静止\n整体音景：安静\n非叙事配乐：无",
            }]}],
        }
        fixes = repair_deterministic_storyboard(doc, "zh")
        segment = doc["episodes"][0]["segments"][0]
        self.assertIn("特写", segment["cuts"][0]["frame"])
        self.assertTrue(segment["h3Prompt"].startswith("目标视频在 0.00 秒处完全参照图 1"))
        self.assertGreaterEqual(len(fixes), 2)

    def test_episode_overrun_only_trims_action_cuts(self):
        doc = {
            "promptLang": "en",
            "params": {"tolerance": 0.15, "minCutSeconds": 2},
            "episodes": [{"ep": 1, "segments": [
                {"id": "E01-01", "sceneIndex": 1, "cuts": [
                    {"seconds": 8.0, "beats": [1, 1]},
                    {"seconds": 8.0, "beats": [2, 2]},
                ], "h3Prompt": "old"},
            ]}],
        }
        script = {"episodes": [{"ep": 1, "targetSeconds": 10, "scenes": [
            {"flow": [{"action": "move"}, {"action": "turn"}]}
        ]}]}
        fixes = balance_episode_durations(doc, script)
        cuts = doc["episodes"][0]["segments"][0]["cuts"]
        self.assertTrue(fixes)
        self.assertLessEqual(sum(c["seconds"] for c in cuts), 11.5)
        self.assertGreaterEqual(min(c["seconds"] for c in cuts), 2.0)

    def test_segment_h3_errors_are_grouped(self):
        found = segment_h3_violation_refs([
            "E01-01 首行对齐指令和分镜结构对不上",
            "E01-02 的 h3Prompt 设定中文却写成英文",
            "E01-03#2 的景别缺失",
        ])
        self.assertEqual(set(found), {"E01-01", "E01-02"})

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

    def test_repair_beats_size_and_symbols(self):
        from zhipu_agent.stages.storyboard_repair import repair_beats_and_symbols
        doc = {"episodes": [{"ep": 2, "segments": [
            {"id": "E02-01", "sceneIndex": 1, "soundscape": "<风声>", "music": "（低音弦乐）",
             "cuts": [{"beats": [1, 8], "size": "Wide Shot"}, {"beats": [9], "size": "close-up"}]},
            {"id": "E02-02", "sceneIndex": 1, "cuts": [{"beats": [14, 20], "size": "Low Angle"}, {"beats": [21]}]},
            {"id": "E02-03", "sceneIndex": 2, "cuts": [{"beats": [1]}]},
        ]}]}
        repair_beats_and_symbols(doc)
        a, b = doc["episodes"][0]["segments"][0]["cuts"]
        c, d = doc["episodes"][0]["segments"][1]["cuts"]
        self.assertEqual(a["size"], "wide")
        self.assertEqual(b["beats"], [9, 13])      # 终点 = 下一镜起点 - 1
        self.assertEqual(b["size"], "close")
        from zhipu_agent.stages.storyboard_repair import _norm_size
        for raw in ("medium-close", "medium_close_up", "Medium Close-Up"):
            self.assertEqual(_norm_size(raw), "close")
        self.assertEqual(_norm_size("medium"), "medium")
        self.assertEqual(c["size"], "Low Angle")   # 认不出来不猜
        self.assertEqual(d["beats"], [21, 21])     # 场内最后一镜：只补成单拍，缺口仍由质量门报出
        self.assertEqual(doc["episodes"][0]["segments"][2]["cuts"][0]["beats"], [1, 1])
        self.assertEqual(doc["episodes"][0]["segments"][0]["soundscape"], "风声")
        self.assertEqual(doc["episodes"][0]["segments"][0]["music"], "低音弦乐")

    def test_params_override_h3_gates(self):
        import os
        from zhipu_agent.stages import storyboard
        old = os.environ.pop("AGENT_STORYBOARD_PARAMS", None)
        try:
            self.assertEqual(storyboard._params_override(True), {})
            self.assertEqual(storyboard._params_override(False), {"h3Gates": False})
            os.environ["AGENT_STORYBOARD_PARAMS"] = '{"maxOnScreen": 5}'
            self.assertEqual(storyboard._params_override(False), {"maxOnScreen": 5, "h3Gates": False})
            os.environ["AGENT_STORYBOARD_PARAMS"] = '{"nameGate": "false"}'
            self.assertEqual(storyboard._params_override(True), {"nameGate": False})
        finally:
            os.environ.pop("AGENT_STORYBOARD_PARAMS", None)
            if old is not None:
                os.environ["AGENT_STORYBOARD_PARAMS"] = old

    def test_beats_odd_shapes(self):
        from zhipu_agent.stages.storyboard_repair import repair_beats_and_symbols
        doc = {"episodes": [{"ep": 1, "segments": [{"id": "E01-01", "sceneIndex": 1, "cuts": [
            {"beats": ["1"]}, {"beats": "2-4"}, {"beats": [5, 6, 7]}, {"beats": [8, None]}, {"beats": [9, 9]}]}]}]}
        repair_beats_and_symbols(doc)
        self.assertEqual([c["beats"] for c in doc["episodes"][0]["segments"][0]["cuts"]],
                         [[1, 1], [2, 4], [5, 7], [8, 8], [9, 9]])

    def test_clean_shot_and_dialogue_ceiling(self):
        from zhipu_agent.stages.storyboard_repair import _clean_shot, repair_dialogue_fit
        self.assertEqual(_clean_shot("白狐抬头<d>[C01] 你好</d>，{你好}看向（远处）<风声>"), "白狐抬头，看向远处风声")
        script = {"params": {"charsPerSecond": 4.5}, "episodes": [{"ep": 1, "scenes": [{"flow": [
            {"line": "一二三四五六七八九十" * 1}, {"line": "一二三四五六七八九十" * 1}, {"line": "一二三四五六七八九十一二三"}]}]}]}
        doc = {"params": {"maxCutSeconds": 10}, "episodes": [{"ep": 1, "segments": [{"id": "E01-01", "sceneIndex": 1, "cuts": [
            {"seconds": 4.0, "beats": [1, 3]}]}]}]}
        repair_dialogue_fit(doc, script, "zh")
        sec = doc["episodes"][0]["segments"][0]["cuts"][0]["seconds"]
        total = 0.0
        for line in script["episodes"][0]["scenes"][0]["flow"]:
            total += math.floor(len(line["line"]) / 4.5 * 10 + 0.5) / 10
        self.assertGreaterEqual(sec, total)


if __name__ == "__main__":
    unittest.main()
