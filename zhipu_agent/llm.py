"""模型调用层：OpenAI 兼容接口（默认指向智谱）。

- chat(messages, kind=)     发一轮对话，返回文本；kind 是 "text"（文本模型池）或 "vision"（视觉模型池）
                            池里有多个平台时按策略轮换，出错自动换下一个
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
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .config import Endpoint, Settings, load_settings


class LLMError(RuntimeError):
    """模型调用失败。"""


class LLMTruncated(LLMError):
    """输出被长度上限截断。"""


class JSONExtractError(LLMError):
    """回复里找不到合法 JSON。"""


Backend = Callable[..., str]
_BACKEND: Optional[Backend] = None
_clients: dict = {}

# 号池状态：轮询计数、出错平台的冷却截止时间。多线程并发调用，所以要加锁
_POOL_LOCK = threading.Lock()
_rr_counter: dict = {}
_cooling: dict = {}


def set_backend(fn: Optional[Backend]) -> None:
    """替换底层调用。fn(messages, *, model) -> str，model 是被选中的那一项的模型名。传 None 恢复。"""
    global _BACKEND
    _BACKEND = fn


def reset_pool_state() -> None:
    """清空轮询计数和冷却记录（测试用）。"""
    with _POOL_LOCK:
        _rr_counter.clear()
        _cooling.clear()


def _client(ep: Endpoint, settings: Settings):
    key = (ep.base_url, ep.api_key)
    if key not in _clients:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError("缺少依赖 openai，请先运行：pip install -r requirements.txt") from exc
        _clients[key] = OpenAI(
            api_key=ep.api_key,
            base_url=ep.base_url,
            timeout=ep.timeout if ep.timeout is not None else settings.timeout,
            max_retries=0,  # 重试由本模块自己做，便于打日志
        )
    return _clients[key]


def _key_hint(api_key: str) -> str:
    """只给诊断用的 Key 指纹：长度、首尾几位、常见的粘贴问题。绝不输出完整 Key。"""
    key = api_key or ""
    if not key:
        return "（空）"
    stripped = key.strip()
    shown = f"{stripped[:4]}…{stripped[-2:]}" if len(stripped) > 8 else "（太短，不显示）"
    notes = []
    if key != key.strip():
        notes.append("首尾有空白或换行")
    if key.strip() and (key.strip()[0] in "\"'" or key.strip()[-1] in "\"'"):
        notes.append("首尾带引号")
    if key.lower().startswith("bearer "):
        notes.append("以 Bearer 开头")
    if any(ord(c) > 127 for c in key):
        notes.append("含非 ASCII 字符")
    return f"{shown}，共 {len(key)} 位" + (f"，注意：{'、'.join(notes)}" if notes else "")


def describe_error(ep: Endpoint, exc: BaseException, limit: int = 4000) -> str:
    """把一次调用失败的完整信息排成多行：平台、模型、地址、状态码、请求 ID、服务商返回的原始响应体、Key 指纹。"""
    orig = exc.__cause__ if isinstance(exc, LLMError) and exc.__cause__ is not None else exc
    response = getattr(orig, "response", None)
    lines = [
        f"平台：{ep.name}　模型：{ep.model}",
        f"地址：{ep.base_url}",
        f"异常：{type(orig).__module__}.{type(orig).__name__}",
    ]
    status = getattr(orig, "status_code", None) or getattr(response, "status_code", None)
    if status is not None:
        lines.append(f"HTTP 状态码：{status}")
    request = getattr(orig, "request", None) or getattr(response, "request", None)
    if request is not None:
        lines.append(f"请求：{getattr(request, 'method', '?')} {getattr(request, 'url', '?')}")
    request_id = getattr(orig, "request_id", None)
    if not request_id and response is not None:
        headers = getattr(response, "headers", None) or {}
        request_id = headers.get("x-request-id") or headers.get("request-id") or headers.get("x-trace-id")
    if request_id:
        lines.append(f"请求 ID：{request_id}")
    body = getattr(orig, "body", None)
    if body is None and response is not None:
        try:
            body = response.text
        except Exception:  # noqa: BLE001
            body = None
    if body is not None:
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
        lines.append("服务商返回：" + (text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字，已截断）"))
    lines.append(f"错误信息：{orig}")
    if status == 401 or "authentication" in type(orig).__name__.lower():
        lines.append(f"Key 指纹：{_key_hint(ep.api_key)}（401 = Key 无效/过期/填错平台，请核对 models.json 或 .env 里这一项的 Key 与 base_url 是否属于同一平台）")
    return "\n".join("    " + line for line in lines)


def _retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status in (408, 409, 429) or status >= 500
    return type(exc).__name__ in {
        "APIConnectionError", "APITimeoutError", "Timeout", "ConnectError", "ReadTimeout",
    }


def _ep_key(ep: Endpoint) -> tuple:
    return (ep.name, ep.base_url, ep.model)


def _order(settings: Settings, kind: str) -> list:
    """这一次调用要依次尝试的平台：按策略排好，最近出过错、还在冷却期的排到最后。"""
    eps = list(settings.endpoints(kind))
    if not eps:
        if kind == "vision":
            raise LLMError("没有配置视觉模型：在 .env 里设置 ZHIPU_VISION_MODEL，或在 models.json 的 vision 里添加")
        raise LLMError("没有配置文本模型：设置 ZHIPU_API_KEY 和 ZHIPU_MODEL，或创建 models.json")
    now = time.time()
    with _POOL_LOCK:
        if settings.strategy == "round_robin" and len(eps) > 1:
            start = _rr_counter.get(kind, 0) % len(eps)
            _rr_counter[kind] = start + 1
            eps = eps[start:] + eps[:start]
        ready = [e for e in eps if _cooling.get(_ep_key(e), 0) <= now]
        cooling = [e for e in eps if _cooling.get(_ep_key(e), 0) > now]
    return ready + cooling


def _cool_down(ep: Endpoint, settings: Settings, exc: Exception) -> None:
    status = getattr(exc, "status_code", None)
    seconds = settings.cooldown * (5 if status in (401, 402, 403) else 1)  # Key 失效或欠费，短时间内不会好
    with _POOL_LOCK:
        _cooling[_ep_key(ep)] = time.time() + seconds


def chat(
    messages: Sequence[dict],
    settings: Optional[Settings] = None,
    *,
    kind: str = "text",
    temperature: Optional[float] = None,
    json_mode: Optional[bool] = None,
    purpose: str = "direct-chat",
    reasoning_effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """发一轮对话，返回文本。

    kind="text" 用文本模型池，kind="vision" 用视觉模型池。池里有多个平台时：
    failover 策略按配置顺序，前一个失败（重试用完仍不行）才用下一个；round_robin 轮流起头。
    每个平台内部遇到网络类错误会先退避重试，再整体换到下一个平台。
    """
    s = settings or load_settings()
    if _BACKEND is None:
        s.require()
    order = _order(s, kind)
    failures: list = []
    for index, ep in enumerate(order):
        last = index == len(order) - 1
        try:
            # 后面还有备用平台时，在当前平台上少试几次就换，别把时间耗在一个坏掉的平台上
            return _call_endpoint(
                ep, messages, s, temperature, json_mode,
                retries=s.retries if last else min(s.retries, 1),
                purpose=purpose, reasoning_effort=reasoning_effort, max_tokens=max_tokens,
            )
        except LLMTruncated:
            raise
        except Exception as exc:  # noqa: BLE001 - 换下一个平台
            failures.append(f"{ep.label()}：{type(exc).__name__}: {str(exc)[:2000]}")
            _cool_down(ep, s, exc)
            _say(f"⚠ {ep.label()} 失败" + ("，改用下一个平台" if not last else "，没有可切换的平台了") + "；详细信息：\n" + describe_error(ep, exc))
    if len(failures) == 1:
        raise LLMError(f"模型调用失败：{failures[0]}")
    raise LLMError(f"所有 {len(failures)} 个平台都调用失败：\n  " + "\n  ".join(failures))


def _call_endpoint(
    ep: Endpoint, messages, s: Settings, temperature, json_mode, *, retries: int,
    purpose: str = "direct-conversation", reasoning_effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> str:
    if _BACKEND is not None:
        started = time.time()
        try:
            return _BACKEND(list(messages), model=ep.model)
        finally:
            _say(f"[llm-metrics] purpose={purpose} provider={ep.name} model={ep.model} "
                 f"input_tokens=n/a output_tokens=n/a reasoning_tokens=n/a "
                 f"elapsed={time.time() - started:.2f}s source=custom-backend")

    use_stream = s.stream if ep.stream is None else ep.stream
    params: dict = {
        "model": ep.model,
        "messages": list(messages),
        "temperature": temperature if temperature is not None
        else (ep.temperature if ep.temperature is not None else s.temperature),
    }
    # Only send an output cap when the caller explicitly requests one.
    # Most stages leave this unset and use the provider/model native limit.
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    effort = reasoning_effort if reasoning_effort is not None else (
        ep.reasoning_effort if ep.reasoning_effort is not None else s.reasoning_effort
    )
    if effort:
        # Deliberately opt-in: unsupported providers are unaffected unless configured.
        params["reasoning_effort"] = effort
    use_json = json_mode if json_mode is not None else (s.json_mode if ep.json_mode is None else ep.json_mode)
    if use_stream and ep.stream_usage is not False:
        # Request usage on stream responses by default; endpoint config can opt out.
        params["stream_options"] = {"include_usage": True}
    if use_json:
        params["response_format"] = {"type": "json_object"}

    for attempt in range(retries + 1):
        started = time.time()
        _say(f"调用 {ep.label()}（用途={purpose}，第 {attempt + 1} 次尝试，{'流式' if use_stream else '非流式'}）…")
        try:
            usage = None
            if use_stream:
                try:
                    text, finish, usage = _stream_call(_client(ep, s), params, started)
                except Exception as stream_exc:
                    message = str(stream_exc).lower()
                    if "stream_options" in params and any(
                        marker in message for marker in ("stream_options", "unknown parameter", "unrecognized parameter", "extra inputs are not permitted")
                    ):
                        # Some OpenAI-compatible endpoints reject include_usage. Retry the same call
                        # without this optional field rather than failing the generation.
                        params.pop("stream_options", None)
                        _say(f"{ep.label()} 不支持 stream_options，关闭流式 usage 后重试")
                        text, finish, usage = _stream_call(_client(ep, s), params, started)
                    else:
                        raise
            else:
                response = _client(ep, s).chat.completions.create(**params)
                choice = response.choices[0]
                text, finish = choice.message.content or "", choice.finish_reason
                usage = getattr(response, "usage", None)
        except Exception as exc:  # noqa: BLE001 - 统一包装
            waited = time.time() - started
            if attempt < retries and _retryable(exc):
                pause = min(2 ** (attempt + 1), 30)
                _say(f"⚠ {type(exc).__name__}（已等 {waited:.0f}s）：{str(exc)[:200]}；{pause}s 后重试")
                time.sleep(pause)
                continue
            _say(f"[llm-metrics] purpose={purpose} provider={ep.name} model={ep.model} "
                 f"input_tokens=n/a output_tokens=n/a reasoning_tokens=n/a "
                 f"elapsed={time.time() - started:.2f}s status=error")
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        elapsed = time.time() - started
        counts = _usage_counts(usage)
        _say(f"返回完毕：{len(text)} 字，用时 {elapsed:.0f}s，finish_reason={finish}")
        _say(f"[llm-metrics] purpose={purpose} provider={ep.name} model={ep.model} "
             f"input_tokens={counts['input_tokens']} output_tokens={counts['output_tokens']} "
             f"reasoning_tokens={counts['reasoning_tokens']} elapsed={elapsed:.2f}s "
             f"finish_reason={finish}")
        if finish == "length":
            raise LLMTruncated(
                "模型输出达到服务商或模型自身的原生长度上限。应用不会设置 max_tokens；"
                "可更换原生输出上限更高的模型，或减小每批处理的集数后重试。"
            )
        if not text.strip():
            if attempt < retries:
                time.sleep(2)
                continue
            raise LLMError(f"模型返回了空内容（finish_reason={finish}）")
        return text
    raise LLMError("超过最大重试次数")  # pragma: no cover


def ping_all(settings: Optional[Settings] = None) -> list:
    """逐个平台各调用一次，返回 [(kind, 名称, 是否成功, 说明, 耗时秒)]。doctor --ping 用。"""
    s = settings or load_settings()
    results = []
    for kind in ("text", "vision"):
        for ep in s.endpoints(kind):
            started = time.time()
            try:
                reply = _call_endpoint(ep, [{"role": "user", "content": "只回复两个字母：OK"}], s, None, None, retries=0, purpose="health-check")
                results.append((kind, ep.label(), True, reply.strip()[:40], time.time() - started))
            except Exception as exc:  # noqa: BLE001
                results.append((kind, ep.label(), False, f"{type(exc).__name__}: {str(exc)[:200]}", time.time() - started))
    return results


def _say(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}]   [llm] {message}", file=sys.stderr, flush=True)


def _usage_counts(usage) -> dict:
    """提取 OpenAI 兼容 usage；服务商未返回时明确记录 n/a，不猜测 Token 数。"""
    if usage is None:
        return {"input_tokens": "n/a", "output_tokens": "n/a", "reasoning_tokens": "n/a"}
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details is not None else None
    if reasoning is None:
        reasoning = getattr(usage, "reasoning_tokens", None)
    return {
        "input_tokens": prompt if prompt is not None else "n/a",
        "output_tokens": completion if completion is not None else "n/a",
        "reasoning_tokens": reasoning if reasoning is not None else "n/a",
    }


def _stream_call(client, params: dict, started: float):
    """流式调用；支持时读取 usage chunk，同时统计 reasoning_content 的字符数用于进度展示。"""
    stream = client.chat.completions.create(stream=True, **params)
    parts: list = []
    thought = 0
    finish = None
    usage = None
    last_report = time.time()
    for chunk in stream:
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage = chunk_usage
        if not getattr(chunk, "choices", None):
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        piece = getattr(delta, "content", None)
        if piece:
            parts.append(piece)
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            thought += len(reasoning)
        if choice.finish_reason:
            finish = choice.finish_reason
        if time.time() - last_report >= 15:
            got = sum(len(x) for x in parts)
            _say(f"生成中…已 {time.time() - started:.0f}s，已收到 {got} 字" + (f"（另有思考 {thought} 字）" if thought else ""))
            last_report = time.time()
    return "".join(parts), finish, usage


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

    def __init__(
        self, system: str, settings: Optional[Settings] = None, *,
        purpose: str = "direct-ask-text", reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ):
        self.settings = settings or load_settings()
        self.purpose = purpose
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self.messages: list = [{"role": "system", "content": system}]

    @property
    def has_history(self) -> bool:
        return len(self.messages) > 1

    def ask(self, user: str, images: Sequence[Path] = (), kind: str = "text") -> str:
        content: Any = image_content(user, images) if images else user
        self.messages.append({"role": "user", "content": content})
        try:
            reply = chat(
                self.messages, self.settings, kind=kind, purpose=self.purpose,
                reasoning_effort=self.reasoning_effort, max_tokens=self.max_tokens,
            )
        except Exception:
            self.messages.pop()  # 失败的这一轮不留在历史里
            raise
        self.messages.append({"role": "assistant", "content": reply})
        return reply

    def ask_json(
        self,
        user: str,
        images: Sequence[Path] = (),
        kind: str = "text",
        parse_retries: int = 2,
    ) -> Any:
        reply = self.ask(user, images, kind)
        for attempt in range(parse_retries + 1):
            try:
                return extract_json(reply)
            except JSONExtractError:
                if attempt == parse_retries:
                    raise
                reply = self.ask(
                    "上一条回复不是合法 JSON（可能被多余文字包住或括号不匹配）。"
                    "请只输出完整、合法的 JSON，不要解释，不要 markdown 围栏。",
                    kind=kind,
                )
        raise JSONExtractError("unreachable")  # pragma: no cover


def ask_text(
    system: str, user: str, settings: Optional[Settings] = None, *,
    purpose: str = "unspecified", reasoning_effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """一问一答，返回纯文本；purpose 会写入每次模型调用的统计日志。"""
    return Conversation(
        system, settings, purpose=purpose, reasoning_effort=reasoning_effort, max_tokens=max_tokens,
    ).ask(user).strip()
