"""出图提示词优化：成功写入、不合格回退、可关闭、缓存。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zhipu_agent import llm  # noqa: E402
from zhipu_agent.image_prompt_optimizer import optimize_image  # noqa: E402
from zhipu_agent.config import load_settings  # noqa: E402
from zhipu_agent.stages.base import Context  # noqa: E402

SHEET = ("Single character model sheet on ONE 16:9 landscape canvas, bust on the left, front, side and back "
         "full-body views on the right, plain white background, thin dividing lines.")
IMAGE = {"prompt": "A lean ferryman in a patched indigo jacket, weathered face, standing on a wet dock at dawn.",
         "sheet": SHEET, "negativePrompt": "extra fingers", "tags": ["ferryman"]}


def make_ctx(tmp: Path) -> Context:
    os.environ.setdefault("ZHIPU_API_KEY", "x")
    os.environ.setdefault("ZHIPU_MODEL", "m")
    ctx = Context.__new__(Context)
    ctx.__dict__.update(settings=load_settings(), force=False, logs=[])
    ctx.work = lambda name: (tmp / name).mkdir(parents=True, exist_ok=True) or (tmp / name)
    ctx.log = ctx.logs.append
    from zhipu_agent.skill_loader import load_pack
    pack = load_pack(ctx.settings)
    ctx.skill = lambda name: pack[name]
    return ctx


class OptimizeImage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.calls = []
        os.environ.pop("AGENT_FLUX_OPTIMIZE", None)

    def tearDown(self):
        llm.set_backend(None)

    def backend(self, replies):
        def fn(messages, *, model=""):
            self.calls.append(messages[-1]["content"])
            return replies[min(len(self.calls) - 1, len(replies) - 1)]
        llm.set_backend(fn)

    def test_success_rewrites_prompt_and_sheet_only(self):
        new = {"prompt": IMAGE["prompt"] + " Soft side light.", "sheet": SHEET + " Even studio light."}
        self.backend([json.dumps(new)])
        out = optimize_image(make_ctx(self.tmp), IMAGE, kind="character", label="老周", forbid=["老周"])
        self.assertEqual(out["prompt"], new["prompt"])
        self.assertEqual(out["sheet"], new["sheet"])
        self.assertEqual(out["negativePrompt"], "extra fingers")
        self.assertEqual(out["tags"], ["ferryman"])
        self.assertEqual(IMAGE["prompt"], "A lean ferryman in a patched indigo jacket, weathered face, standing on a wet dock at dawn.")

    def test_bad_output_is_retried_then_falls_back(self):
        bad = json.dumps({"prompt": "老周 standing", "sheet": "short"})
        self.backend([bad])
        ctx = make_ctx(self.tmp)
        out = optimize_image(ctx, IMAGE, kind="character", label="老周", forbid=["老周"])
        self.assertEqual(out, IMAGE)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(any("保留原提示词" in m for m in ctx.logs))

    def test_lost_layout_phrase_is_rejected(self):
        bad = json.dumps({"prompt": IMAGE["prompt"] + " Soft side light.", "sheet": SHEET.replace("16:9", "wide") + " Even light."})
        self.backend([bad, bad])
        out = optimize_image(make_ctx(self.tmp), IMAGE, kind="character", label="x")
        self.assertEqual(out["sheet"], SHEET)

    def test_prop_scale_phrase_must_survive(self):
        img = {"prompt": "A brass key, handheld scale, on white background.", "sheet": "A brass key, handheld scale, white background, sheet."}
        bad = json.dumps({"prompt": "A shiny brass key on white background, softly lit.", "sheet": img["sheet"] + " Lit softly."})
        self.backend([bad, bad])
        out = optimize_image(make_ctx(self.tmp), img, kind="prop", label="钥匙", must_keep=["handheld scale"])
        self.assertEqual(out, img)

    def test_disabled_makes_no_call(self):
        os.environ["AGENT_FLUX_OPTIMIZE"] = "0"
        self.backend(["{}"])
        self.assertEqual(optimize_image(make_ctx(self.tmp), IMAGE, kind="character", label="x"), IMAGE)
        self.assertEqual(self.calls, [])

    def test_cache_avoids_second_call(self):
        new = {"prompt": IMAGE["prompt"] + " Soft side light.", "sheet": SHEET + " Even studio light."}
        self.backend([json.dumps(new)])
        ctx = make_ctx(self.tmp)
        optimize_image(ctx, IMAGE, kind="character", label="x")
        optimize_image(ctx, IMAGE, kind="character", label="x")
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
