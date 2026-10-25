import tempfile
import unittest
from pathlib import Path

from zhipu_agent import video_prompt_optimizer as vpo

ORIGINAL = (
    "参考图与目标视频的对齐——图1在0.00秒。\n"
    "[镜头 1] 镜头缓推向<Picture 5>。<Picture 1>与<Picture 2>被绑住双臂，<Picture 1>失去平衡。\n"
    "[镜头 2] 于 00:05.000，近景。<Picture 2>低声说：<d>[Chinese] 师兄别喊了，他们听不懂人话。</d>\n"
    "整体音景：火焰噼啪声。"
)
GOOD = (
    "Alignment: reference 1 at 0.00s.\n"
    "[Shot 1] The camera pushes in slowly toward <Picture 5>. <Picture 1> and <Picture 2> have their arms bound; <Picture 1> loses balance.\n"
    "[Shot 2] At 00:05.000, close-up. <Picture 2> whispers: <d>[Chinese] 师兄别喊了，他们听不懂人话。</d>\n"
    "Overall soundscape: crackling flames."
)


class ValidateTest(unittest.TestCase):
    def test_good_passes(self):
        self.assertEqual(vpo.validate(ORIGINAL, GOOD), [])

    def test_missing_picture_is_rejected(self):
        bad = GOOD.replace("<Picture 5>", "the altar")
        self.assertTrue(any("<Picture 5>" in p for p in vpo.validate(ORIGINAL, bad)))

    def test_repeated_picture_must_keep_count(self):
        bad = GOOD.replace("<Picture 1> loses balance", "he loses balance")  # <Picture 1> 原文 2 处
        self.assertTrue(any("<Picture 1>" in p for p in vpo.validate(ORIGINAL, bad)))

    def test_renumbered_picture_is_rejected(self):
        bad = GOOD.replace("<Picture 2> whispers", "<Picture 3> whispers")
        problems = vpo.validate(ORIGINAL, bad)
        self.assertTrue(any("<Picture 2>" in p for p in problems) and any("<Picture 3>" in p for p in problems))

    def test_dialogue_must_be_verbatim(self):
        bad = GOOD.replace("师兄别喊了", "Brother, stop shouting")
        self.assertTrue(any("台词" in p for p in vpo.validate(ORIGINAL, bad)))

    def test_chinese_outside_dialogue_is_rejected(self):
        bad = GOOD.replace("crackling flames", "火焰")
        self.assertTrue(any("中文" in p for p in vpo.validate(ORIGINAL, bad)))

    def test_shot_markers_must_match(self):
        bad = GOOD.replace("[Shot 2]", "[Shot 3]")
        self.assertTrue(any("[Shot" in p for p in vpo.validate(ORIGINAL, bad)))


class OptimizeTest(unittest.TestCase):
    def test_retry_then_success(self):
        calls = []

        def chat(messages, **kw):
            calls.append(messages)
            return GOOD.replace("<Picture 5>", "the altar") if len(calls) == 1 else GOOD

        text, problems = vpo.optimize_one(ORIGINAL, chat)
        self.assertEqual((text, problems), (GOOD, []))
        self.assertEqual(len(calls), 2)
        self.assertIn("<Picture 5>", calls[1][-1]["content"])  # 重试时把问题告诉模型

    def test_falls_back_to_original_when_still_bad(self):
        text, problems = vpo.optimize_one(ORIGINAL, lambda m, **k: "no tags at all")
        self.assertEqual(text, ORIGINAL)
        self.assertTrue(problems)

    def test_model_error_falls_back(self):
        def boom(m, **k):
            raise RuntimeError("down")
        text, problems = vpo.optimize_one(ORIGINAL, boom)
        self.assertEqual(text, ORIGINAL)
        self.assertIn("down", problems[0])


class RewriteFnTest(unittest.TestCase):
    def test_rewrite_fn_result_is_still_validated(self):
        text, problems = vpo.optimize_one(ORIGINAL, rewrite_fn=lambda p: GOOD)
        self.assertEqual((text, problems), (GOOD, []))
        text, problems = vpo.optimize_one(ORIGINAL, rewrite_fn=lambda p: GOOD.replace("<Picture 5>", "x"))
        self.assertEqual(text, ORIGINAL)
        self.assertTrue(problems)

    def test_rewrite_fn_error_falls_back(self):
        def boom(p):
            raise ValueError("placeholder lost")
        text, problems = vpo.optimize_one(ORIGINAL, rewrite_fn=boom)
        self.assertEqual(text, ORIGINAL)
        self.assertIn("placeholder lost", problems[0])


class H3ValidateTest(unittest.TestCase):
    def test_length_and_timeline(self):
        item = {"duration_seconds": 8}
        self.assertEqual(vpo.validate_h3(item, ORIGINAL, GOOD), [])
        self.assertTrue(any("7000" in p for p in vpo.validate_h3(item, ORIGINAL, GOOD + " x" * 4000)))
        late = GOOD + " The camera drifts during 6-12 seconds."
        self.assertTrue(any("时间轴" in p for p in vpo.validate_h3(item, ORIGINAL, late)))
        self.assertFalse(any("时间轴" in p for p in vpo.validate_h3({"duration_seconds": 12}, ORIGINAL, late)))


class StrategyTest(unittest.TestCase):
    def test_strategy_names_and_isolation(self):
        from zhipu_agent import video_prompt_strategies as vps
        self.assertEqual(vps.strategy_for({"adapter": "comfyui"}), "minimax_h3")
        self.assertEqual(vps.strategy_for({"adapter": "aliyun_wan"}), "aliyun_wan")
        self.assertIn("--video-model comfyui_h3 --video-submit", vps.next_command({"key": "comfyui_h3"}, [1]))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            item = {"id": "S1", "prompt": ORIGINAL, "duration_seconds": 8}
            vpo.optimize_items(root, [item], strategy="minimax_h3", rewrite_fn=lambda p: GOOD, log=lambda *_a: None)
            # 另一个模型的策略读不到 minimax_h3 的结果；wan 策略只要求非空，不要求保留 <Picture N>
            self.assertIsNone(vpo.load_optimized(root, item, "aliyun_wan"))
            vpo.optimize_items(root, [item], strategy="aliyun_wan", rewrite_fn=lambda p: "wan 版提示词", log=lambda *_a: None)
            self.assertEqual(vpo.load_optimized(root, item, "aliyun_wan"), "wan 版提示词")
            self.assertEqual(vpo.load_optimized(root, item, "minimax_h3"), GOOD)
            self.assertTrue(vpo.optimized_path(root, "S1", "aliyun_wan").is_file())


class LazySaveTest(unittest.TestCase):
    def test_save_then_load_roundtrip_and_invalidation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vpo.save_optimized(root, "S1", "原提示词", "wan 优化结果", "aliyun_wan")
            self.assertEqual(vpo.load_optimized(root, {"id": "S1", "prompt": "原提示词"}, "aliyun_wan"), "wan 优化结果")
            self.assertIsNone(vpo.load_optimized(root, {"id": "S1", "prompt": "原提示词改了"}, "aliyun_wan"))
            self.assertIsNone(vpo.load_optimized(root, {"id": "S1", "prompt": "原提示词"}, "minimax_h3"))


class IndexTest(unittest.TestCase):
    def test_cache_invalidates_when_source_changes_and_force(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            item = {"id": "E01-01-C01", "prompt": ORIGINAL}
            n = {"calls": 0}

            def chat(m, **k):
                n["calls"] += 1
                return GOOD

            log = lambda *_a: None  # noqa: E731
            self.assertIsNone(vpo.load_optimized(root, item))
            self.assertEqual(vpo.optimize_items(root, [item], chat_fn=chat, log=log), {"E01-01-C01": "optimized"})
            self.assertEqual(vpo.load_optimized(root, item), GOOD)
            # 再跑：已有结果，不再调用模型
            self.assertEqual(vpo.optimize_items(root, [item], chat_fn=chat, log=log), {"E01-01-C01": "cached"})
            self.assertEqual(n["calls"], 1)
            # --video-optimize-force：重做
            self.assertEqual(vpo.optimize_items(root, [item], force=True, chat_fn=chat, log=log), {"E01-01-C01": "optimized"})
            self.assertEqual(n["calls"], 2)
            # 原提示词变了：旧优化结果作废
            self.assertIsNone(vpo.load_optimized(root, {"id": "E01-01-C01", "prompt": ORIGINAL + " changed"}))

    def test_failed_optimization_is_not_used(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            item = {"id": "S1", "prompt": ORIGINAL}
            vpo.optimize_items(root, [item], chat_fn=lambda m, **k: "bad", log=lambda *_a: None)
            self.assertIsNone(vpo.load_optimized(root, item))


if __name__ == "__main__":
    unittest.main()
