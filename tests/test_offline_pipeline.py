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
        conv = llm.Conversation("sys", load_settings(api_key="k", model="text-model", vision_model="vision-model"))
        conv.ask_json("认图", images=[png], kind="vision")
    finally:
        llm.set_backend(None)
        shutil.rmtree(png.parent, ignore_errors=True)
    assert seen["model"] == "vision-model"
    kinds = [part["type"] for part in seen["content"]]
    assert kinds == ["text", "image_url"]
    assert seen["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def _pool_settings(strategy="failover", vision=True, **kw):
    from zhipu_agent.config import Endpoint
    text = (Endpoint("a", "http://a", "ka", "model-a"), Endpoint("b", "http://b", "kb", "model-b"),
            Endpoint("c", "http://c", "kc", "model-c"))
    pics = (Endpoint("v1", "http://v1", "kv1", "vision-1"), Endpoint("v2", "http://v2", "kv2", "vision-2")) if vision else ()
    return load_settings(api_key="", model="", text_pool=text, vision_pool=pics, strategy=strategy,
                         retries=0, cooldown=60, **kw)


def test_pool_failover_order_and_cooldown():
    """failover：平台 a 出错就换 b；a 进入冷却期，后面的调用先走 b；全部出错才报错并列出每个平台。"""
    llm.reset_pool_state()
    used = []

    def backend(messages, *, model):
        used.append(model)
        if model == "model-a":
            raise RuntimeError("a 欠费了")
        return "ok"

    llm.set_backend(backend)
    try:
        s = _pool_settings()
        assert llm.chat([{"role": "user", "content": "x"}], s) == "ok"
        assert used == ["model-a", "model-b"], used
        used.clear()
        assert llm.chat([{"role": "user", "content": "x"}], s) == "ok"
        assert used == ["model-b"], f"a 在冷却期，应先走 b：{used}"

        llm.reset_pool_state()
        llm.set_backend(lambda messages, *, model: (_ for _ in ()).throw(RuntimeError(f"{model} 挂了")))
        try:
            llm.chat([{"role": "user", "content": "x"}], s)
        except llm.LLMError as exc:
            text = str(exc)
            assert all(m in text for m in ("model-a", "model-b", "model-c")), text
        else:
            raise AssertionError("全部平台失败应当抛出 LLMError")
    finally:
        llm.set_backend(None)
        llm.reset_pool_state()


def test_pool_round_robin_spreads_calls_and_vision_uses_vision_pool():
    llm.reset_pool_state()
    used = []
    llm.set_backend(lambda messages, *, model: used.append(model) or "ok")
    try:
        s = _pool_settings("round_robin")
        for _ in range(4):
            llm.chat([{"role": "user", "content": "x"}], s)
        assert used == ["model-a", "model-b", "model-c", "model-a"], used
        used.clear()
        llm.chat([{"role": "user", "content": "x"}], s, kind="vision")
        llm.chat([{"role": "user", "content": "x"}], s, kind="vision")
        assert used == ["vision-1", "vision-2"], used
        try:
            llm.chat([{"role": "user", "content": "x"}], _pool_settings(vision=False), kind="vision")
        except llm.LLMError as exc:
            assert "视觉模型" in str(exc)
        else:
            raise AssertionError("没配视觉模型时应当报错")
    finally:
        llm.set_backend(None)
        llm.reset_pool_state()


def test_models_file_parsing_and_legacy_env():
    import json
    import os
    from zhipu_agent.config import ConfigError, load_models_file
    tmp = Path(tempfile.mkdtemp(prefix="agent-test-models-"))
    try:
        os.environ["POOL_TEST_KEY"] = "from-env"
        good = tmp / "models.json"
        good.write_text(json.dumps({
            "strategy": "round_robin", "cooldown": 30,
            "text": [{"name": "p1", "base_url": "http://p1/v1", "api_key": "k1", "model": "m1", "max_tokens": 4096},
                     {"name": "p2", "base_url": "http://p2/v1", "api_key_env": "POOL_TEST_KEY", "model": "m2", "stream": False}],
            "vision": [{"name": "v", "base_url": "http://v/v1", "api_key": "kv", "model": "mv"}],
        }), encoding="utf-8")
        cfg = load_models_file(good)
        assert [e.model for e in cfg["text"]] == ["m1", "m2"] and cfg["text"][1].api_key == "from-env"
        assert cfg["text"][0].max_tokens == 4096 and cfg["text"][1].stream is False and cfg["strategy"] == "round_robin"

        def bad(obj, needle):
            f = tmp / "bad.json"
            f.write_text(json.dumps(obj), encoding="utf-8")
            try:
                load_models_file(f)
            except ConfigError as exc:
                assert needle in str(exc), (needle, str(exc))
            else:
                raise AssertionError(f"应当报错：{needle}")

        bad({"text": []}, "text")
        bad({"text": [{"name": "x", "base_url": "http://x", "model": "m"}]}, "api_key")
        bad({"text": [{"name": "x", "base_url": "http://x", "api_key_env": "NO_SUCH_ENV_VAR_X", "model": "m"}]}, "NO_SUCH_ENV_VAR_X")
        one = {"name": "x", "base_url": "http://x", "api_key": "k", "model": "m"}
        bad({"text": [one, one]}, "重名")
        bad({"text": [one], "strategy": "random"}, "strategy")

        # 没有 models.json：沿用单平台配置；视觉模型默认同平台，也可以单独指定地址和 Key
        legacy = load_settings(api_key="K", model="M", base_url="http://t", vision_model="V")
        assert [(e.base_url, e.api_key, e.model) for e in legacy.text_endpoints()] == [("http://t", "K", "M")]
        assert [(e.base_url, e.api_key, e.model) for e in legacy.vision_endpoints()] == [("http://t", "K", "V")]
        split = load_settings(api_key="K", model="M", base_url="http://t", vision_model="V",
                              vision_base_url="http://v", vision_api_key="KV")
        assert [(e.base_url, e.api_key) for e in split.vision_endpoints()] == [("http://v", "KV")]
        assert load_settings(api_key="K", model="M").vision_endpoints() == ()
    finally:
        os.environ.pop("POOL_TEST_KEY", None)
        shutil.rmtree(tmp, ignore_errors=True)


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
    for test in (test_pool_failover_order_and_cooldown,
                 test_pool_round_robin_spreads_calls_and_vision_uses_vision_pool,
                 test_models_file_parsing_and_legacy_env,
                 test_extract_json_tolerates_fences_and_chatter,
                 test_image_goes_to_vision_model_as_multimodal_message,
                 test_long_text_uses_volume_summaries,
                 test_refs_stage_builds_assets_without_image_model,
                 test_pipeline_offline):
        test()
        print(f"✓ {test.__name__}")
    print("全部离线测试通过")
