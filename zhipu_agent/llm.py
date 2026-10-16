"""模型调用层：OpenAI 兼容接口（默认指向智谱）。

- chat(messages)            保持原有签名：返回文本
- Conversation              带历史的多轮对话，校验不过时把报错接着发给模型
- extract_json              从回复里取出 JSON（去围栏、容忍前后废话）
- image_content             把本地图片拼成 OpenAI 格式的多模态消息
- set_backend               替换底层调用（离线测试用）
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .config import Settings, load_settings


class LLMError(RuntimeError):
    """模型调用失败。"""


class LLMTruncated(LLMError):
    """输出被长度上限截断。"""


class JSONExtractError(LLMError):
    """回复里找不到合法 JSON。"""


Backend = Callable[..., str]
_BACKEND: Optional[Backend] = None
_clients: dict = {}


def set_backend(fn: Optional[Backend]) -> None:
    """替换底层调用。fn(messages, *, model) -> str。传 None 恢复。"""
    global _BACKEND
    _BACKEND = fn


def _client(settings: Settings):
    key = (settings.base_url, settings.api_key)
    if key not in _clients:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError("缺少依赖 openai，请先运行：pip install -r requirements.txt") from exc
        _clients[key] = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout,
            max_retries=0,  # 重试由本模块自己做，便于打日志
        )
    return _clients[key]


def _retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status in (408, 409, 429) or status >= 500
    return type(exc).__name__ in {
        "APIConnectionError", "APITimeoutError", "Timeout", "ConnectError", "ReadTimeout",
    }


def chat(
    messages: Sequence[dict],
    settings: Optional[Settings] = None,
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    json_mode: Optional[bool] = None,
) -> str:
    """发一轮对话，返回文本。网络类错误自动退避重试。"""
    s = settings or load_settings()
    use_model = model or s.model
    if _BACKEND is not None:
        return _BACKEND(list(messages), model=use_model)
    s.require()

    params: dict = {
        "model": use_model,
        "messages": list(messages),
        "temperature": s.temperature if temperature is None else temperature,
    }
    if s.max_tokens:
        params["max_tokens"] = s.max_tokens
    if s.json_mode if json_mode is None else json_mode:
        params["response_format"] = {"type": "json_object"}

    for attempt in range(s.retries + 1):
        try:
            response = _client(s).chat.completions.create(**params)
        except Exception as exc:  # noqa: BLE001 - 统一包装
            if attempt < s.retries and _retryable(exc):
                time.sleep(min(2 ** (attempt + 1), 30))
                continue
            raise LLMError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc

        choice = response.choices[0]
        text = choice.message.content or ""
        if choice.finish_reason == "length":
            raise LLMTruncated(
                "模型输出被长度上限截断。可以设置环境变量 ZHIPU_MAX_TOKENS 调大上限，"
                "或减小每批处理的集数后重试。"
            )
        if not text.strip():
            if attempt < s.retries:
                time.sleep(2)
                continue
            raise LLMError(f"模型返回了空内容（finish_reason={choice.finish_reason}）")
        return text
    raise LLMError("模型调用失败：超过最大重试次数")  # pragma: no cover


def extract_json(text: str) -> Any:
    """从模型回复里取出第一个合法的 JSON 值。"""
    stripped = text.strip()
    candidates = [stripped]
    for match in re.finditer(r"```(?:json|JSON)?\s*(.*?)```", stripped, re.S):
        candidates.insert(0, match.group(1).strip())
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            pass
    decoder = json.JSONDecoder()
    for candidate in candidates:
        starts = [i for i, ch in enumerate(candidate) if ch in "{["][:60]
        for i in starts:
            try:
                value, _ = decoder.raw_decode(candidate[i:])
            except ValueError:
                continue
            if isinstance(value, (dict, list)):
                return value
    raise JSONExtractError("回复里找不到合法的 JSON：" + stripped[:120].replace("\n", " "))


def image_content(text: str, images: Sequence[Path]) -> list:
    """把文字 + 本地图片拼成多模态 content。"""
    parts: list = [{"type": "text", "text": text}]
    for path in images:
        path = Path(path)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    return parts


class Conversation:
    """带历史的对话。校验失败时，报错作为下一轮 user 消息接在后面。"""

    def __init__(self, system: str, settings: Optional[Settings] = None):
        self.settings = settings or load_settings()
        self.messages: list = [{"role": "system", "content": system}]

    @property
    def has_history(self) -> bool:
        return len(self.messages) > 1

    def ask(self, user: str, images: Sequence[Path] = (), model: Optional[str] = None) -> str:
        content: Any = image_content(user, images) if images else user
        self.messages.append({"role": "user", "content": content})
        try:
            reply = chat(self.messages, self.settings, model=model)
        except Exception:
            self.messages.pop()  # 失败的这一轮不留在历史里
            raise
        self.messages.append({"role": "assistant", "content": reply})
        return reply

    def ask_json(
        self,
        user: str,
        images: Sequence[Path] = (),
        model: Optional[str] = None,
        parse_retries: int = 2,
    ) -> Any:
        reply = self.ask(user, images, model)
        for attempt in range(parse_retries + 1):
            try:
                return extract_json(reply)
            except JSONExtractError:
                if attempt == parse_retries:
                    raise
                reply = self.ask(
                    "上一条回复不是合法 JSON（可能被多余文字包住或括号不匹配）。"
                    "请只输出完整、合法的 JSON，不要解释，不要 markdown 围栏。",
                    model=model,
                )
        raise JSONExtractError("unreachable")  # pragma: no cover


def ask_text(system: str, user: str, settings: Optional[Settings] = None) -> str:
    """一问一答，返回纯文本。"""
    return Conversation(system, settings).ask(user).strip()
