import unittest

from zhipu_agent.stages.script import repair_hook_beat, repair_speakers


class HookBeatRepairTest(unittest.TestCase):
    def test_zero_based_hook_beat(self):
        ep = {"hookBeat": [0, 0]}
        self.assertTrue(repair_hook_beat(ep))
        self.assertEqual(ep["hookBeat"], [1, 1])
        ep = {"hookBeat": [1, 0]}
        repair_hook_beat(ep)
        self.assertEqual(ep["hookBeat"], [2, 1])

    def test_valid_untouched(self):
        ep = {"hookBeat": [2, 1]}
        self.assertFalse(repair_hook_beat(ep))
        self.assertEqual(ep["hookBeat"], [2, 1])
        self.assertFalse(repair_hook_beat({}))

    def test_speaker_added_to_scene(self):
        ep = {"scenes": [{"sceneId": "S04", "characters": ["C09", "C08"], "flow": [
            {"speaker": "C03", "line": "x"}, {"speaker": "VO", "line": "y"}, {"speaker": "C99", "line": "z"}, {"speaker": "C09", "line": "w"}]}]}
        fixes = repair_speakers(ep, {"C03", "C08", "C09"})
        self.assertEqual(ep["scenes"][0]["characters"], ["C09", "C08", "C03"])
        self.assertEqual(len(fixes), 1)


if __name__ == "__main__":
    unittest.main()
