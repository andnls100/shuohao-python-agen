"""分镜图阶段：任务编号、参考图引用、请求体、单张/整集/全部选择、断点续跑（本地假出图服务）。"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zhipu_agent.stages import frames

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")


class _Handler(BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _Handler.bodies.append(json.loads(self.rfile.read(length)))
        import base64
        out = json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def make_ctx(tmp: str, with_assets=True):
    out = Path(tmp) / "output"
    (out / "storyboard").mkdir(parents=True)
    (out / "script").mkdir()
    board = {"aspectRatio": "9:16", "episodes": [
        {"ep": 1, "segments": [
            {"id": "E01-01", "sceneIndex": 1, "cuts": [
                {"seconds": 3, "frame": "中景，徐今朝站在门口", "characters": ["徐今朝"], "props": ["信"]},
                {"seconds": 3, "frame": "特写，徐今朝的手", "characters": ["徐今朝"]}]},
            {"id": "E01-02", "sceneIndex": 1, "cuts": [{"seconds": 3, "frame": "全景，空院子", "characters": []}]}]},
        {"ep": 2, "segments": [
            {"id": "E02-01", "sceneIndex": 1, "cuts": [{"seconds": 3, "frame": "全景，雨夜", "characters": []}]}]}]}
    (out / "storyboard" / "T-storyboard.json").write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    script = {"episodes": [{"ep": 1, "scenes": [{"sceneId": "S01"}]}, {"ep": 2, "scenes": [{"sceneId": "S01"}]}]}
    (out / "script" / "T-script.json").write_text(json.dumps(script), encoding="utf-8")
    if with_assets:
        (out / "images" / "characters").mkdir(parents=True)
        (out / "art" / "images").mkdir(parents=True)
        (out / "images" / "props").mkdir(parents=True)
        (out / "images" / "characters" / "a.png").write_bytes(PNG)
        (out / "art" / "images" / "s.png").write_bytes(PNG)
        (out / "images" / "props" / "p.png").write_bytes(PNG)
        manifest = {"items": [
            {"kind": "character", "name": "徐今朝", "asset_id": "C001", "output_path": "images/characters/a.png"},
            {"kind": "scene", "name": "院子", "asset_id": "S01", "output_path": "art/images/s.png"},
            {"kind": "prop", "name": "信", "asset_id": "P001", "output_path": "images/props/p.png"}]}
        (out / "images" / "image-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    paths = {"storyboard": out / "storyboard" / "T-storyboard.json", "script": out / "script" / "T-script.json"}
    return SimpleNamespace(out=out, title="T", force=False, artifact=lambda n: paths[n], log=lambda m: None)


class FramesTest(unittest.TestCase):
    def test_ids_references_and_prompt_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp)
            path = frames.run(ctx)
            items = {i["id"]: i for i in json.loads(path.read_text(encoding="utf-8"))["items"]}
        self.assertEqual(list(items), ["E01-01-C01", "E01-01-C02", "E01-02-C01", "E02-01-C01"])
        first = items["E01-01-C01"]
        self.assertEqual([(r["no"], r["kind"], r["name"]) for r in first["references"]],
                         [(1, "character", "徐今朝"), (2, "scene", "院子"), (3, "prop", "信")])
        self.assertTrue(first["prompt"].startswith("参考图说明：图1是角色「徐今朝」"))
        self.assertIn("图2是场景「院子」", first["prompt"])
        self.assertEqual(first["output_path"], "frames/E01-01/f1.png")
        self.assertEqual(items["E01-01-C02"]["output_path"], "frames/E01-01/f2.png")
        self.assertEqual(items["E01-02-C01"]["references"][0]["kind"], "scene")  # 无角色时只引用场景

    def test_missing_assets_are_reported_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp, with_assets=False)
            items = json.loads(frames.run(ctx).read_text(encoding="utf-8"))["items"]
        self.assertEqual(items[0]["references"], [])
        self.assertTrue(any("徐今朝" in m for m in items[0]["missing_references"]))

    def test_selection_single_episode_all_and_resume(self):
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        provider = [{"name": "p", "url": f"http://127.0.0.1:{server.server_port}/gen", "api_key": "k", "model": "m",
                     "adapter": "openai", "size": "1536x1024", "sizes": {"9:16": "1024x1536"}, "quality": None, "timeout": 10,
                     "reference_field": "image", "reference_style": "list", "image_encoding": "data_uri",
                     "max_references": 4, "request_template": None, "headers": {}}]
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(frames, "_providers", return_value=provider):
                ctx = make_ctx(tmp)
                _Handler.bodies.clear()
                frames.run(ctx, generate=True, only=["E01-01-C01"])          # 单个分镜
                self.assertEqual(len(_Handler.bodies), 1)
                body = _Handler.bodies[0]
                self.assertEqual(body["size"], "1024x1536")                    # 按竖屏画幅选尺寸
                self.assertEqual(len(body["image"]), 3)                        # 角色 + 场景 + 道具
                self.assertTrue(body["image"][0].startswith("data:image/png;base64,"))
                self.assertTrue((ctx.out / "frames/E01-01/f1.png").is_file())
                frames.run(ctx, generate=True, eps=[1])                        # 一集：C01 已完成会跳过
                self.assertEqual(len(_Handler.bodies), 3)
                frames.run(ctx, generate=True)                                 # 全部：只补剩下的 1 张
                self.assertEqual(len(_Handler.bodies), 4)
                self.assertTrue(frames.check(ctx).ok)
                items = json.loads(frames.artifact(ctx).read_text(encoding="utf-8"))["items"]
                self.assertTrue(all(i["state"] == "succeeded" for i in items))
        finally:
            server.shutdown()
            server.server_close()

    def _inherit_board(self, ctx, *, second_mode="inherit", segment_mode=None):
        path = ctx.out / "storyboard" / "T-storyboard.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        cuts = board["episodes"][0]["segments"][0]["cuts"]
        cuts[1]["continuity"] = {"mode": second_mode, "referenceFrame": "E01-01-C01"}
        nxt = board["episodes"][0]["segments"][1]
        nxt["cuts"][0]["continuity"] = {"mode": "inherit"}
        if segment_mode:
            nxt["continuityMode"] = segment_mode
        path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")

    def test_inherit_cut_gets_previous_frame_as_last_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp)
            self._inherit_board(ctx)
            items = {i["id"]: i for i in json.loads(frames.run(ctx).read_text(encoding="utf-8"))["items"]}
        self.assertNotIn("previous", [r["kind"] for r in items["E01-01-C01"]["references"]])      # 首镜没有上一镜
        refs = items["E01-01-C02"]["references"]
        self.assertEqual([r["kind"] for r in refs], ["character", "scene", "previous"])
        self.assertEqual(refs[-1]["name"], "E01-01-C01")
        self.assertIn("图3是上一镜参考帧「E01-01-C01」", items["E01-01-C02"]["prompt"])
        self.assertIn("不要照搬上一镜的机位", items["E01-01-C02"]["prompt"])
        # 跨段连续：下一段首切引用上一段末切
        cross = items["E01-02-C01"]["references"]
        self.assertEqual(cross[-1]["kind"], "previous")
        self.assertEqual(cross[-1]["name"], "E01-01-C02")

    def test_cut_and_hard_cut_and_legacy_storyboards_get_no_previous_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp)
            self._inherit_board(ctx, second_mode="cut", segment_mode="hard_cut")
            items = {i["id"]: i for i in json.loads(frames.run(ctx).read_text(encoding="utf-8"))["items"]}
        for item in items.values():
            self.assertNotIn("previous", [r["kind"] for r in item["references"]])

    def test_previous_frame_keeps_its_slot_when_over_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp)
            self._inherit_board(ctx)
            tasks, _ = frames._collect(ctx)
            task = next(t for t in tasks if t["id"] == "E01-01-C02")
            prev = next(t for t in tasks if t["id"] == "E01-01-C01")
            refs, missing = frames._references(ctx, 1, task["_segment"], task["_cut"], frames._asset_index(ctx),
                                               frames._scene_ids(ctx), 2, prev)
        self.assertEqual([r["kind"] for r in refs], ["character", "previous"])   # 场景让位，上一镜保留
        self.assertTrue(any("超过接口上限" in m for m in missing))

    def test_generation_in_order_sends_the_just_generated_previous_frame(self):
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        provider = [{"name": "p", "url": f"http://127.0.0.1:{server.server_port}/gen", "api_key": "k", "model": "m",
                     "adapter": "openai", "size": "1536x1024", "sizes": {}, "quality": None, "timeout": 10,
                     "reference_field": "image", "reference_style": "list", "image_encoding": "data_uri",
                     "max_references": 4, "request_template": None, "headers": {}}]
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(frames, "_providers", return_value=provider):
                ctx = make_ctx(tmp)
                self._inherit_board(ctx)
                _Handler.bodies.clear()
                frames.run(ctx, generate=True, only=["E01-01"])               # 整段，C01 先出，C02 再出
                self.assertEqual(len(_Handler.bodies), 2)
                self.assertEqual(len(_Handler.bodies[1]["image"]), 3)         # 角色 + 场景 + 上一镜
                self.assertIn("上一镜参考帧", _Handler.bodies[1]["prompt"])
        finally:
            server.shutdown()
            server.server_close()

    def test_missing_previous_frame_is_dropped_and_renumbered(self):
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        provider = [{"name": "p", "url": f"http://127.0.0.1:{server.server_port}/gen", "api_key": "k", "model": "m",
                     "adapter": "openai", "size": "1536x1024", "sizes": {}, "quality": None, "timeout": 10,
                     "reference_field": "image", "reference_style": "list", "image_encoding": "data_uri",
                     "max_references": 4, "request_template": None, "headers": {}}]
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(frames, "_providers", return_value=provider):
                ctx = make_ctx(tmp)
                self._inherit_board(ctx)
                _Handler.bodies.clear()
                frames.run(ctx, generate=True, only=["E01-01-C02"])           # 单独出 C02，上一镜还没有
                self.assertEqual(len(_Handler.bodies[0]["image"]), 2)
                self.assertNotIn("上一镜参考帧", _Handler.bodies[0]["prompt"])
                items = json.loads(frames.artifact(ctx).read_text(encoding="utf-8"))["items"]
                c02 = next(i for i in items if i["id"] == "E01-01-C02")
                self.assertTrue(any("上一镜参考帧" in m for m in c02["missing_references"]))
        finally:
            server.shutdown()
            server.server_close()

    def test_unknown_only_and_request_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                frames.run(make_ctx(tmp), only=["E09-01-C01"])
        base = {"model": "m", "reference_field": "image", "quality": None}
        self.assertEqual(frames.build_request({**base, "reference_style": "single"}, "p", "s", ["a", "b"])["image"], "a")
        self.assertEqual(frames.build_request({**base, "reference_style": "objects"}, "p", "s", ["a"])["image"],
                         [{"image_url": "a"}])
        templ = {**base, "request_template": {"model": "{{model}}", "input": {"images": "{{images}}", "text": "{{prompt}}"}}}
        self.assertEqual(frames.build_request(templ, "p", "s", ["a", "b"])["input"], {"images": ["a", "b"], "text": "p"})

    def test_video_picks_existing_jpg_frame(self):
        from zhipu_agent.stages.video import _frame_file
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "E01-01").mkdir()
            (Path(tmp) / "E01-01" / "f1.jpg").write_bytes(b"x")
            self.assertEqual(_frame_file(Path(tmp), "E01-01", 1).name, "f1.jpg")
            self.assertEqual(_frame_file(Path(tmp), "E01-01", 2).name, "f2.png")


if __name__ == "__main__":
    unittest.main()
