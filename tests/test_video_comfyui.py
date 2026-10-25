import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image

from zhipu_agent.stages import video_comfyui as vc

ROOT = Path(__file__).resolve().parent.parent
REF_WF = ROOT / "workflow" / "kaggle_h3_ref2va.json"
UI_WF = ROOT / "workflow" / "MiniMax_H3_Fast_I2V.json"

I2V_API = {
    "114": {"class_type": "LoadImage", "inputs": {"image": "x.png"}, "_meta": {"title": "START FRAME (required)"}},
    "133": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"prompt": "old", "first_frame": ["114", 0]}},
    "135": {"class_type": "PrimitiveFloat", "inputs": {"value": 3.0}, "_meta": {"title": "DURATION (seconds)"}},
    "131": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
    "92": {"class_type": "SaveVideo", "inputs": {"video": ["x", 0], "filename_prefix": "old"}},
}


def _png(path: Path, color=(200, 10, 10), size=(80, 60)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


class BuildTest(unittest.TestCase):
    def test_ref2va_fills_prompt_refs_and_seed(self):
        tpl = vc.load_workflow(REF_WF)
        wf = vc.build_ref2va(tpl, prompt="P", seconds=7.5, aspect="9:16", size_preset="720p",
                             uploaded=["a.png", "b.png", "c.png"], seed=123, prefix="shuohao/E01-01-C01")
        ins = wf["h3"]["inputs"]
        self.assertEqual([ins[f"reference_{i}.file"] for i in range(3)], ["a.png", "b.png", "c.png"])
        self.assertEqual((ins["prompt"], ins["seconds"], ins["aspect_ratio"], ins["size_preset"]), ("P", 8, "9:16", "360p"))  # 720p = 360p 生成 + 2 倍放大
        self.assertTrue(wf["upscale"]["inputs"]["enabled"])
        self.assertEqual(wf["sample"]["inputs"]["noise_seed"], 123)
        self.assertEqual(wf["save"]["inputs"]["filename_prefix"], "shuohao/E01-01-C01")
        self.assertEqual(tpl["h3"]["inputs"]["reference_0.file"], "CHARACTER_REFERENCE.png")  # 模板不被改动
        one = vc.build_ref2va(tpl, prompt="P", seconds=5, aspect=None, size_preset=None, uploaded=["a.png"], seed=1, prefix="p")
        self.assertNotIn("reference_1.file", one["h3"]["inputs"])

    def test_ref2va_480p_disables_upscale(self):
        tpl = vc.load_workflow(REF_WF)
        wf = vc.build_ref2va(tpl, prompt="P", seconds=5, aspect="16:9", size_preset="480p", uploaded=["a.png"], seed=1, prefix="p")
        self.assertEqual(wf["h3"]["inputs"]["size_preset"], "480p")
        self.assertFalse(wf["upscale"]["inputs"]["enabled"])
        self.assertTrue(tpl["upscale"]["inputs"]["enabled"])  # 模板不被改动

    def test_ui_format_is_rejected_with_clear_message(self):
        with self.assertRaises(vc.ComfyError) as cm:
            vc.load_workflow(UI_WF)
        self.assertIn("API", str(cm.exception))

    def test_i2v_build(self):
        wf = vc.build_i2v(I2V_API, prompt="go", seconds=6, uploaded="f.png", seed=9, prefix="q")
        self.assertEqual(wf["114"]["inputs"]["image"], "f.png")
        self.assertEqual(wf["133"]["inputs"]["prompt"], "go")
        self.assertEqual(wf["135"]["inputs"]["value"], 6.0)
        self.assertEqual(wf["131"]["inputs"]["noise_seed"], 9)
        self.assertEqual(wf["92"]["inputs"]["filename_prefix"], "q")

    def test_collage_when_over_limit(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            refs = [("character", _png(td / f"c{i}.png")) for i in range(5)] + [("scene", _png(td / "s.png")), ("prop", _png(td / "p1.png")), ("prop", _png(td / "p2.png"))]
            planned = vc.plan_references(refs, 6, td / "collage.png")
            self.assertEqual(len(planned), 6)
            self.assertEqual([k for k, _ in planned[:5]], ["character"] * 5)
            self.assertEqual(planned[5][0], "collage")
            self.assertTrue((td / "collage.png").is_file())
            self.assertEqual(len(vc.plan_references(refs[:6], 6, td / "x.png")), 6)

    def test_whole_seconds(self):
        self.assertEqual([vc.whole_seconds(x) for x in (5, 5.0, 4.2, 7.5, 0.3, 4.9999999999)], [5, 5, 5, 8, 1, 5])
        self.assertIsInstance(vc.whole_seconds(5.0), int)

    def test_header(self):
        h = vc.reference_header(["character", "character", "scene", "prop", "collage"])
        self.assertIn("<Picture 1>, <Picture 2>", h)
        self.assertIn("the scene from <Picture 3>", h)
        self.assertIn("<Picture 4>", h)
        self.assertIn("<Picture 5> is a collage", h)

    def _canvas_refs(self, count):
        tpl = {"h3": {"class_type": "KaggleH3Conditioning", "inputs": {"prompt": "x", "reference_0": "image", "reference_0.file": "a.png", "seconds": 5}},
               "save": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "p"}}}
        names = [f"u{i}.png" for i in range(count)]
        wf = vc.build_ref2va(tpl, prompt="p", seconds=5, aspect="16:9", size_preset="360p", uploaded=names, seed=1, prefix="x")
        node = next(n for n in vc.canvas_from_api(wf)["nodes"] if n["id"] == "h3")
        named = node["widgets_values_named"]
        files = [named[k] for k in named if k.startswith("reference_") and k.endswith(".file")]
        return node, named, files, names

    def test_canvas_has_only_uploaded_references(self):
        # 4 张参考图时，画布工作流里不能残留模板里的第 5 张旧图
        for count in (1, 4, 5, 6):
            node, named, files, names = self._canvas_refs(count)
            self.assertEqual(files, names)
            used = [k for k in named if k.startswith("reference_") and not k.endswith(".file") and named[k] != "none"]
            self.assertEqual(len(used), count)
            self.assertEqual(len(node["widgets_values"]), len(named))
            self.assertEqual(node["widgets_values"], list(named.values()))


class _Handler(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        st = self.state
        if self.path == "/upload/image":
            marker = b'filename="'
            i = body.index(marker) + len(marker)
            name = body[i:body.index(b'"', i)].decode()
            st.setdefault("uploads", []).append(name)
            start = body.index(b"\r\n\r\n", i) + 4
            st.setdefault("files", {})[name] = body[start:body.rindex(b"\r\n--")]
            self._send(200, json.dumps({"name": name, "subfolder": "", "type": "input"}).encode())
        elif self.path == "/prompt":
            st.setdefault("events", []).append("queue")
            st["prompt"] = json.loads(body)["prompt"]
            self._send(200, json.dumps({"prompt_id": "p1"}).encode())

    def do_GET(self):
        st = self.state
        if self.path.startswith("/history/"):
            st.setdefault("events", []).append("poll")
            st["polls"] = st.get("polls", 0) + 1
            if st["polls"] < 2:
                self._send(200, b"{}")
            else:
                out = {"p1": {"status": {"status_str": "success", "completed": True},
                              "outputs": {"save": {"images": [{"filename": "r.mp4", "subfolder": "shuohao", "type": "output"}], "animated": [True]}}}}
                self._send(200, json.dumps(out).encode())
        elif self.path.startswith("/view") and "type=input" in self.path:
            name = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)["filename"][0]
            if name in st.get("files", {}):
                self._send(200, st["files"][name], "image/png")
            else:
                self._send(404, b"not found", "text/plain")
        elif self.path.startswith("/view"):
            st["view"] = self.path
            self._send(200, b"VIDEOBYTES", "video/mp4")


class GenerateTest(unittest.TestCase):
    def setUp(self):
        _Handler.state = {}
        self.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()

    def test_ref2va_end_to_end_with_collage(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = td / "output"
            (out / "art").mkdir(parents=True)
            assets = []
            for i, kind in enumerate(["character"] * 4 + ["scene"] + ["prop"] * 4):
                assets.append({"kind": kind, "name": f"n{i}", "path": str(_png(out / "art" / f"a{i}.png").relative_to(out))})
            item = {"id": "E01-01-C01", "duration_seconds": 8, "prompt": "镜头正文", "reference_assets_all": assets, "input_mode": "scene_or_assets"}
            cfg = {"base_url": self.base, "workflow_ref": str(REF_WF), "max_refs": 6, "poll": 0.01, "max_wait": 10, "size_preset": "360p"}
            target, note = vc.generate(item, {"aspect_ratio": "16:9"}, cfg, out, out / "video" / "work", None, out / "video" / "videos" / "E01-01-C01")
            self.assertEqual(target.read_bytes(), b"VIDEOBYTES")
            self.assertEqual(target.suffix, ".mp4")
            self.assertEqual(len(_Handler.state["uploads"]), 6)
            ins = _Handler.state["prompt"]["h3"]["inputs"]
            self.assertTrue(ins["prompt"].startswith("Preserve the subject from <Picture 1>"))
            self.assertIn("镜头正文", ins["prompt"])
            self.assertEqual(ins["seconds"], 8)
            self.assertIsInstance(ins["seconds"], int)
            self.assertIn("reference_5.file", ins)
            self.assertNotIn("reference_6.file", ins)
            self.assertIn("subfolder=shuohao", _Handler.state["view"])
            self.assertIn("含拼图", note)

    def test_upload_keeps_name_and_skips_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            a = _png(td / "same.png", (1, 2, 3))
            self.assertEqual(vc.Client(self.base).upload_image(a), "same.png")  # 不加任何前缀
            self.assertEqual(vc.Client(self.base).upload_image(a), "same.png")  # 本次运行内重复：不再上传
            self.assertEqual(_Handler.state["uploads"], ["same.png"])
            # 换一次运行（清掉进程内缓存）：服务器上已有相同内容的同名文件，也不重复上传
            vc._UPLOADED.clear()
            vc._UPLOAD_NAMES.clear()
            self.assertEqual(vc.Client(self.base).upload_image(a), "same.png")
            self.assertEqual(_Handler.state["uploads"], ["same.png"])
            # 同名但内容不同：不覆盖已有文件
            (td / "other").mkdir()
            other = _png(td / "other" / "same.png", (9, 9, 9))
            name = vc.Client(self.base).upload_image(other)
            self.assertNotEqual(name, "same.png")
            self.assertTrue(name.endswith("_same.png"))
            self.assertEqual(len(_Handler.state["uploads"]), 2)

    def test_queue_all_shots_before_waiting_and_ignore_previous_frame(self):
        from zhipu_agent.stages import video as vs
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = td / "output"
            (out / "art").mkdir(parents=True)
            asset = {"kind": "character", "name": "n0", "path": str(_png(out / "art" / "a0.png").relative_to(out))}
            items = [{"id": f"E01-01-C0{i}", "duration_seconds": 5, "prompt": "p", "reference_assets_all": [asset],
                      "input_mode": "previous_last_frame" if i > 1 else "scene_or_assets",
                      "previous_shot_id": f"E01-01-C0{i - 1}" if i > 1 else None, "state": "planned"} for i in (1, 2, 3)]
            manifest = {"aspect_ratio": "16:9", "items": items}
            cfg = {"base_url": self.base, "workflow_ref": str(REF_WF), "workflow_i2v": str(UI_WF), "max_refs": 6, "poll": 0.01, "max_wait": 10, "size_preset": "480p"}
            root = out / "video"
            root.mkdir()
            vs._run_comfyui_queue(manifest, [{"name": "c", "adapter": "comfyui", "comfyui": cfg}], root, root / "video-manifest.json", set(), None)
            events = _Handler.state["events"]
            self.assertEqual(events[:3], ["queue"] * 3)  # 3 个镜头先全部排队，再开始轮询
            self.assertIn("poll", events[3:])
            self.assertEqual([i["state"] for i in items], ["succeeded"] * 3)
            self.assertTrue(all((root / i["video_path"]).is_file() for i in items))
            self.assertTrue(all(i["last_frame_path"] is None for i in items))  # 不提取最后一帧
            self.assertEqual(len(_Handler.state["uploads"]), 1)  # 同一张参考图只上传一次

    def test_queue_uses_cached_optimized_prompt_without_calling_model(self):
        from zhipu_agent import video_prompt_optimizer as vpo
        from zhipu_agent.stages import video as vs
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = td / "output"
            (out / "art").mkdir(parents=True)
            asset = {"kind": "character", "name": "n0", "path": str(_png(out / "art" / "a0.png").relative_to(out))}
            item = {"id": "E01-01-C01", "duration_seconds": 5, "prompt": "原提示词 <Picture 1>", "reference_assets_all": [asset],
                    "input_mode": "scene_or_assets", "state": "planned"}
            root = out / "video"
            root.mkdir()
            vpo.optimize_items(root, [item], chat_fn=lambda m, **k: "English prompt <Picture 1>", log=lambda *_a: None)
            cfg = {"base_url": self.base, "workflow_ref": str(REF_WF), "max_refs": 6, "poll": 0.01, "max_wait": 10, "size_preset": "480p"}
            provider = {"name": "c", "key": "comfyui_h3", "adapter": "comfyui", "comfyui": cfg}
            vs._run_comfyui_queue({"aspect_ratio": "16:9", "items": [item]}, [provider], root, root / "video-manifest.json", set(), None)  # settings=None：真调模型会炸
            sent = _Handler.state["prompt"]["h3"]["inputs"]["prompt"]
            self.assertIn("English prompt <Picture 1>", sent)
            self.assertNotIn("原提示词", sent)

    def test_continuation_uses_i2v_with_previous_frame(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            wf = td / "i2v.json"
            wf.write_text(json.dumps(I2V_API), encoding="utf-8")
            last = _png(td / "last.png")
            item = {"id": "E01-01-C02", "duration_seconds": 5, "prompt": "继续", "input_mode": "previous_last_frame"}
            cfg = {"base_url": self.base, "workflow_ref": str(REF_WF), "workflow_i2v": str(wf), "poll": 0.01, "max_wait": 10}
            target, note = vc.generate(item, {}, cfg, td, td / "work", last, td / "videos" / "E01-01-C02")
            self.assertTrue(target.is_file())
            self.assertTrue(note.startswith("i2v"))
            self.assertTrue(_Handler.state["prompt"]["114"]["inputs"]["image"].endswith("last.png"))
            self.assertEqual(_Handler.state["prompt"]["133"]["inputs"]["prompt"], "继续")


if __name__ == "__main__":
    unittest.main()
