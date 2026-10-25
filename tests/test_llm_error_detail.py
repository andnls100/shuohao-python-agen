import io
import json
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace

from zhipu_agent import llm
from zhipu_agent.config import Endpoint


class FakeAuthError(Exception):
    """模拟 openai.AuthenticationError 的字段：status_code / body / request_id / response / request。"""

    def __init__(self):
        super().__init__("Error code: 401 - {'error': {'message': 'Invalid API Key'}}")
        self.status_code = 401
        self.body = {"error": {"message": "Invalid API Key", "type": "authentication_error", "code": 401}}
        self.request_id = "req_123"
        self.request = SimpleNamespace(method="POST", url="https://api.example.com/v1/chat/completions")
        self.response = SimpleNamespace(status_code=401, headers={}, text="{}", request=self.request)


class DescribeErrorTest(unittest.TestCase):
    def test_details_for_401(self):
        ep = Endpoint(name="saluki-27b", base_url="https://api.example.com/v1", api_key=" sk-abcdef123456\n", model="saluki-27b")
        wrapped = llm.LLMError("AuthenticationError: boom")
        wrapped.__cause__ = FakeAuthError()
        text = llm.describe_error(ep, wrapped)
        for expected in ("saluki-27b", "https://api.example.com/v1", "FakeAuthError", "401", "req_123",
                         "POST https://api.example.com/v1/chat/completions", "Invalid API Key", "authentication_error", "首尾有空白"):
            self.assertIn(expected, text)
        self.assertNotIn("sk-abcdef123456", text)  # 不输出完整 Key
        self.assertIn("sk-a…56", text)

    def test_empty_and_short_keys(self):
        self.assertEqual(llm._key_hint(""), "（空）")
        self.assertIn("太短", llm._key_hint("abc"))

    def test_chat_prints_details_when_failing_over(self):
        eps = [Endpoint("bad", "https://a/v1", "sk-badbadbad", "m1"), Endpoint("good", "https://b/v1", "sk-good", "m2")]
        settings = SimpleNamespace(
            strategy="failover", cooldown=0, retries=0, endpoints=lambda kind: eps, require=lambda: None,
        )

        def backend(messages, *, model):
            if model == "m1":
                raise FakeAuthError()
            return "ok"

        llm.reset_pool_state()
        llm.set_backend(None)
        original_call = llm._call_endpoint

        def fake_call(ep, messages, s, temperature, json_mode, **kw):
            if ep.model == "m1":
                try:
                    raise FakeAuthError()
                except Exception as exc:
                    raise llm.LLMError(f"{type(exc).__name__}: {exc}") from exc
            return "ok"

        llm._call_endpoint = fake_call
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                self.assertEqual(llm.chat([{"role": "user", "content": "hi"}], settings), "ok")
        finally:
            llm._call_endpoint = original_call
            llm.reset_pool_state()
        out = buf.getvalue()
        self.assertIn("改用下一个平台", out)
        self.assertIn("服务商返回：", out)
        self.assertIn("Invalid API Key", out)
        self.assertIn("HTTP 状态码：401", out)


if __name__ == "__main__":
    unittest.main()
