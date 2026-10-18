import unittest

from zhipu_agent.stages.video_adapters import build_request, request_headers


class VideoAdapterTests(unittest.TestCase):
    def setUp(self):
        self.item = {"prompt": "cinematic shot", "duration_seconds": 4.5}
        self.manifest = {"aspect_ratio": "16:9"}

    def test_aliyun_wan_request_shape(self):
        provider = {
            "adapter": "aliyun_wan",
            "VIDEO_API_MODEL": "wan3.0-video-prime",
            "VIDEO_API_KEY": "test-key",
            "resolution": "480P",
            "ratio": "16:9",
            "prompt_extend": True,
        }
        body = build_request(provider, self.item, self.manifest)
        self.assertEqual(body["model"], "wan3.0-video-prime")
        self.assertEqual(body["input"]["prompt"], "cinematic shot")
        self.assertEqual(body["parameters"]["duration"], 4)
        self.assertEqual(body["parameters"]["resolution"], "480P")
        self.assertEqual(request_headers(provider)["X-DashScope-Async"], "enable")

    def test_template_preserves_numeric_types(self):
        provider = {
            "adapter": "generic",
            "VIDEO_API_MODEL": "seedance-mini",
            "VIDEO_API_KEY": "test-key",
            "request_template": {
                "model": "{{model}}",
                "input": {"prompt": "{{prompt}}"},
                "parameters": {"duration": "{{duration_seconds}}"},
            },
        }
        body = build_request(provider, self.item, self.manifest)
        self.assertEqual(body["model"], "seedance-mini")
        self.assertEqual(body["input"]["prompt"], "cinematic shot")
        self.assertEqual(body["parameters"]["duration"], 4.5)
        self.assertIsInstance(body["parameters"]["duration"], float)

    def test_cogvideox3_request_shape(self):
        provider = {
            "adapter": "cogvideox_zhipu",
            "VIDEO_API_MODEL": "cogvideox-3",
            "VIDEO_API_KEY": "test-key",
            "size": "1920x1080",
            "quality": True,
        }
        body = build_request(provider, self.item, self.manifest)
        self.assertEqual(body["model"], "cogvideox-3")
        self.assertEqual(body["prompt"], "cinematic shot")
        self.assertEqual(body["duration"], 5)
        self.assertEqual(body["size"], "1920x1080")
        self.assertTrue(body["quality"])

    def test_custom_headers(self):
        provider = {
            "adapter": "generic", "VIDEO_API_KEY": "test-key",
            "headers": {"X-Client": "test"},
        }
        headers = request_headers(provider)
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(headers["X-Client"], "test")


if __name__ == "__main__":
    unittest.main()


class CogVideoXFixesTest(unittest.TestCase):
    def test_prompt_is_fitted_to_512_and_alignment_line_dropped(self):
        from zhipu_agent.stages.video_adapters import fit_cogvideox_prompt
        prompt = "How the reference pictures align with the target video — Picture 1 aligns.\n" + "镜头描述。" * 200
        out = fit_cogvideox_prompt(prompt)
        self.assertLessEqual(len(out), 512)
        self.assertNotIn("How the reference", out)
        self.assertTrue(out.endswith("。"))
        self.assertEqual(fit_cogvideox_prompt("短提示"), "短提示")

    def test_vertical_ratio_and_invalid_size(self):
        from zhipu_agent.stages.video_adapters import build_request
        provider = {"adapter": "cogvideox_zhipu", "VIDEO_API_MODEL": "cogvideox-3"}
        item = {"prompt": "p", "duration_seconds": 7.5}
        body = build_request(provider, item, {"aspect_ratio": "9:16"})
        self.assertEqual((body["size"], body["duration"]), ("1080x1920", 10))
        with self.assertRaises(ValueError):
            build_request({**provider, "size": "800x600"}, item, {"aspect_ratio": "16:9"})

    def test_fail_status_is_treated_as_failure(self):
        import json as _json
        from unittest import mock
        from zhipu_agent.stages import video
        cfg = {"status_template": "http://x/{task_id}", "max_wait": 5, "poll": 1,
               "video_url_keys": ["url"], "status_keys": ["task_status"],
               "failed_states": ["failed", "fail"]}
        with mock.patch.object(video, "_request", return_value={"task_status": "FAIL"}):
            with self.assertRaises(RuntimeError):
                video._poll("t1", cfg)
