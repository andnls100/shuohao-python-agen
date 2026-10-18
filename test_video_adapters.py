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


class ImageToVideoTest(unittest.TestCase):
    PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
           b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")

    def _ctx(self, tmp, with_frames=True):
        import json
        from pathlib import Path
        from types import SimpleNamespace
        out = Path(tmp) / "output"
        board = out / "storyboard" / "Demo-storyboard.json"
        board.parent.mkdir(parents=True)
        board.write_text(json.dumps({"aspectRatio": "9:16", "episodes": [{"ep": 1, "segments": [{
            "id": "E01-01", "sceneIndex": 1, "h3Prompt": "x",
            "cuts": [{"seconds": 3, "camera": "推镜", "shot": "女人转身看向门口", "sfx": "雨声"},
                     {"seconds": 6, "camera": "固定", "shot": "门被推开"}]}]}]}), encoding="utf-8")
        if with_frames:
            frames = out / "frames" / "E01-01"
            frames.mkdir(parents=True)
            for n in (1, 2):
                (frames / f"f{n}.png").write_bytes(self.PNG)
        return SimpleNamespace(out=out, title="Demo", force=False, logs=[],
                               artifact=lambda name: board, log=lambda m: None)

    def _cfg(self):
        return [{"key": "c", "name": "c", "adapter": "cogvideox_zhipu", "VIDEO_API_SUBMIT_URL": "http://x/submit",
                 "VIDEO_API_KEY": "k", "VIDEO_API_MODEL": "cogvideox-3", "status_template": "http://x/{task_id}",
                 "timeout": 10, "poll": 1, "max_wait": 5, "quality": "quality", "with_audio": True, "fps": 30,
                 "size": "", "video_url_keys": ["url"], "task_id_keys": ["id"], "status_keys": ["task_status"],
                 "failed_states": ["fail"]}]

    def test_i2v_manifest_and_requests(self):
        import json, tempfile
        from unittest import mock
        from zhipu_agent.stages import video
        sent = []

        def fake_request(url, cfg, body=None):
            if body is not None:
                sent.append(body)
                return {"id": "t%d" % len(sent), "task_status": "PROCESSING"}
            return {"task_status": "SUCCESS", "video_result": [{"url": "http://x/v.mp4"}]}

        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            with mock.patch.object(video, "_config", return_value=self._cfg()), \
                 mock.patch.object(video, "_request", side_effect=fake_request), \
                 mock.patch.object(video, "_download", side_effect=lambda u, t, c: None):
                path = video.run(ctx, eps=[1], protocol="i2v", submit=True, flf=True)
            manifest = json.loads(path.read_text(encoding="utf-8"))
        items = manifest["items"]
        self.assertEqual([i["id"] for i in items], ["E01-01-C01", "E01-01-C02"])
        self.assertEqual([i["mode"] for i in items], ["flf", "i2v"])
        self.assertTrue(all(i["state"] == "succeeded" for i in items))
        self.assertNotIn("data:image", json.dumps(manifest))  # Base64 不写进清单
        first, second = sent
        self.assertEqual(len(first["image_url"]), 2)         # 首尾帧
        self.assertEqual(first["quality"], "speed")          # 首尾帧只支持 speed
        self.assertEqual((first["duration"], second["duration"]), (5, 10))
        self.assertTrue(second["image_url"].startswith("data:image/png;base64,"))
        self.assertNotIn("size", second)                     # 不传 size，按图片比例
        self.assertIn("画面：门被推开", second["prompt"])    # 提示词只有动作，不含 h3 全文

    def test_missing_frames_fail_without_calling_api(self):
        import json, tempfile
        from unittest import mock
        from zhipu_agent.stages import video
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, with_frames=False)
            with mock.patch.object(video, "_config", return_value=self._cfg()), \
                 mock.patch.object(video, "_request") as req:
                path = video.run(ctx, eps=[1], protocol="i2v", submit=True)
            manifest = json.loads(path.read_text(encoding="utf-8"))
            req.assert_not_called()
        self.assertTrue(all(i["state"] == "failed" and "缺少分镜图" in i["error"] for i in manifest["items"]))

    def test_other_adapters_reject_images_and_bad_format(self):
        import tempfile
        from pathlib import Path
        from zhipu_agent.stages.video_adapters import build_request, encode_image
        with self.assertRaises(ValueError):
            build_request({"adapter": "aliyun_wan", "VIDEO_API_MODEL": "m"},
                          {"prompt": "p", "duration_seconds": 5, "images": ["x"]}, {})
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "a.webp"
            bad.write_bytes(b"x")
            with self.assertRaises(ValueError):
                encode_image(bad)
