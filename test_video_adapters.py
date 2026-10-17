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
