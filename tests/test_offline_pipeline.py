"""离线端到端测试：模拟模型 + 真实的 Node 校验器。

运行：  python tests/test_offline_pipeline.py        （也兼容 pytest）
需要：  Node >= 18。不需要 openai 包、密钥或网络。

验证的是代码接线，不是真实模型的质量：
  1. 六个阶段能从《渡口》一路跑到分镜，每个产物都通过各自技能包的 validate
  2. 校验不过时，报错会被归给具体的集 / 角色，只让它们回炉，其余不重跑
  3. 已完成的阶段再跑一遍会被跳过（不再调用模型）
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_llm import FakeModel  # noqa: E402
from zhipu_agent import llm, workflow  # noqa: E402
from zhipu_agent.config import load_settings  # noqa: E402
from zhipu_agent.stages.base import read_json  # noqa: E402

BOOK = ROOT / "skills" / "novel-characters" / "examples" / "渡口.txt"


def _context(out: Path):
    settings = load_settings(api_key="fake", model="fake-model", concurrency=2, fix_rounds=2)
    return workflow.build_context(settings, BOOK, out, interactive=False)


def _outline_params():
    p = read_json(ROOT / "skills/novel-outline/examples/渡口-outline.json")["params"]
    return {k: v for k, v in p.items() if v not in (None, "")}


def run_all(out: Path, fake: FakeModel):
    llm.set_backend(fake)
    try:
        ctx = _context(out)
        opts = {"params": _outline_params()}
        first = workflow.run_pipeline(ctx, ["outline", "characters", "art", "script", "staging"], opts)
        # 分镜只切第 1 集（样例分镜只有第 1 集）
        second = workflow.run_pipeline(ctx, ["storyboard"], {**opts, "eps": [1]})
        return ctx, {**first, **second}
    finally:
        llm.set_backend(None)


def test_pipeline_offline():
    out = Path(tempfile.mkdtemp(prefix="agent-test-"))
    try:
        fake = FakeModel(break_script_ep=2, break_cast_name="沈知微")
        ctx, results = run_all(out, fake)

        # 1) 每个阶段都有产物，且重新校验通过
        for name in ("outline", "characters", "art", "script", "staging", "storyboard"):
            module = workflow.MODULES[name]
            assert results[name].is_file(), f"{name} 没有产物"
            res = module.check(ctx)
            assert res.ok, f"{name} 重新校验未通过：\n{res.text}"

        # 1b) 大纲定的角色 id 必须原样进到 cast.json（下游分镜靠 id 对角色；校验器不查这一项）
        outline_ids = {c["name"]: c["id"] for c in read_json(results["outline"])["characters"]}
        cast_ids = {c["name"]: c.get("id") for c in read_json(results["characters"])["characters"]}
        assert cast_ids == outline_ids, f"cast.json 的角色 id 丢了或错了：{cast_ids} != {outline_ids}"

        # 2) 回炉只发生在出错的条目上
        script_calls = {k[1]: v for k, v in fake.calls.items() if k[0] == "novel-script/script-pass.md"}
        assert script_calls["2"] == 2, f"第 2 集应回炉一次（共 2 次调用），实际 {script_calls}"
        assert all(v == 1 for k, v in script_calls.items() if k != "2"), f"其他集不应重跑：{script_calls}"
        card_calls = {k[1]: v for k, v in fake.calls.items() if k[0] == "novel-characters/profile-pass.md"}
        assert card_calls["沈知微"] == 2, f"沈知微的卡应回炉一次，实际 {card_calls}"
        assert all(v == 1 for k, v in card_calls.items() if k != "沈知微"), f"其他角色不应重跑：{card_calls}"

        # 3) 报告和导出
        assert (out / "outline" / "outline-report.html").is_file()
        assert (out / "script" / "script-report.html").is_file()
        assert (out / "staging" / "staging-report.html").is_file()
        assert (out / "staging" / "blocking.txt").is_file()
        assert (out / "storyboard" / "manifest.json").is_file()
        assert (out / "storyboard" / "storyboard-report.html").is_file()

        # 4) 再跑一遍：全部跳过，不再调用模型
        before = sum(fake.calls.values())
        llm.set_backend(fake)
        try:
            workflow.run_pipeline(ctx, ["outline", "characters", "art", "script", "staging"], {"params": _outline_params()})
            workflow.run_pipeline(ctx, ["storyboard"], {"params": {}, "eps": [1]})
        finally:
            llm.set_backend(None)
        # storyboard 带 --eps 时不整体跳过，但已切好的集会被内部跳过
        assert sum(fake.calls.values()) == before, "第二次运行不应再调用模型"
    finally:
        if "--keep" not in sys.argv:
            shutil.rmtree(out, ignore_errors=True)
        else:
            print(f"产物保留在 {out}")


def test_long_text_uses_volume_summaries():
    """原文超过阈值时：chunk 分卷 → 每卷一次摘要调用 → 骨架只看摘要，不再给全文。"""
    out = Path(tempfile.mkdtemp(prefix="agent-test-long-"))
    try:
        fake = FakeModel()
        llm.set_backend(fake)
        try:
            settings = load_settings(api_key="fake", model="fake-model", concurrency=2, fix_rounds=1, direct_chars=100)
            ctx = workflow.build_context(settings, BOOK, out, interactive=False)
            path = workflow.run_pipeline(ctx, ["outline"], {"params": _outline_params()})["outline"]
        finally:
            llm.set_backend(None)
        volume_calls = [k for k in fake.calls if k[0] == "novel-outline/volume-pass.md"]
        assert volume_calls, "长文本应当走分卷摘要"
        assert "分卷摘要" in fake.last_skeleton_prompt and "这是第" not in fake.last_skeleton_prompt
        assert workflow.MODULES["outline"].check(ctx).ok and path.is_file()
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_refs_stage_builds_assets_without_image_model():
    """refs 阶段：对话模型写 intake → intake-check → new；没配出图模型时不出图，也不报错。"""
    out = Path(tempfile.mkdtemp(prefix="agent-test-refs-"))
    try:
        fake = FakeModel()
        llm.set_backend(fake)
        try:
            ctx = _context(out)
            opts = {"params": _outline_params()}
            workflow.run_pipeline(ctx, ["outline", "characters"], opts)
            workflow.run_pipeline(ctx, ["refs"], {**opts, "refs_names": ["沈知微"], "refs_gen": False})
        finally:
            llm.set_backend(None)
        asset = out / "character-refs" / "沈知微" / "asset.json"
        assert asset.is_file(), "应当建出 asset.json"
        assert read_json(asset)["name"] == "沈知微"
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_image_goes_to_vision_model_as_multimodal_message():
    """带图的条目：消息里有 image_url 数据，并且使用指定的视觉模型。"""
    seen = {}

    def backend(messages, *, model):
        seen["model"] = model
        seen["content"] = messages[-1]["content"]
        return "{}"

    png = Path(tempfile.mkdtemp(prefix="agent-test-img-")) / "x-sheet.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    llm.set_backend(backend)
    try:
        conv = llm.Conversation("sys", load_settings(api_key="k", model="text-model"))
        conv.ask_json("认图", images=[png], model="vision-model")
    finally:
        llm.set_backend(None)
        shutil.rmtree(png.parent, ignore_errors=True)
    assert seen["model"] == "vision-model"
    kinds = [part["type"] for part in seen["content"]]
    assert kinds == ["text", "image_url"]
    assert seen["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_extract_json_tolerates_fences_and_chatter():
    assert llm.extract_json('好的，结果如下：\n```json\n{"a": [1, 2]}\n```\n以上。') == {"a": [1, 2]}
    assert llm.extract_json('前言 {"a": 1} 后记') == {"a": 1}
    assert llm.extract_json("[1, 2, 3]") == [1, 2, 3]
    try:
        llm.extract_json("没有任何 JSON")
    except llm.JSONExtractError:
        pass
    else:
        raise AssertionError("应当抛出 JSONExtractError")


if __name__ == "__main__":
    for test in (test_extract_json_tolerates_fences_and_chatter,
                 test_image_goes_to_vision_model_as_multimodal_message,
                 test_long_text_uses_volume_summaries,
                 test_refs_stage_builds_assets_without_image_model,
                 test_pipeline_offline):
        test()
        print(f"✓ {test.__name__}")
    print("全部离线测试通过")
