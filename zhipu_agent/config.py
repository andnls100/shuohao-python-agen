"""运行配置。

全部来自环境变量；项目根目录的 .env（如果存在）会先被读入。
代码里不放任何密钥。变量名以 ZHIPU_ 开头，同时兼容 LLM_ 开头的同名变量，
方便换成其他兼容 OpenAI 接口的平台。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"


class ConfigError(RuntimeError):
    """配置缺失或不合法。"""


def load_dotenv(path: Optional[Path] = None) -> dict:
    """读取简单的 KEY=VALUE 文件。已存在的环境变量不会被覆盖。"""
    path = Path(path) if path else REPO_ROOT / ".env"
    loaded: dict = {}
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def _get(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _int(name: str, default: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数，当前是 {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是数字，当前是 {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    base_url: str
    vision_model: str          # 可选：staging 认图需要能看图的模型
    temperature: float
    max_tokens: Optional[int]  # None = 不传，使用平台默认
    timeout: float
    retries: int
    concurrency: int           # 同时进行的模型调用数
    fix_rounds: int            # 校验不过时，最多回炉几轮
    node_bin: str
    skills_dir: Path
    direct_chars: int          # 原文不超过这个字数就整本直接喂给模型，跳过分卷摘要
    json_mode: bool            # 是否传 response_format=json_object（部分模型不支持，默认关）

    def require(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("ZHIPU_API_KEY")
        if not self.model:
            missing.append("ZHIPU_MODEL")
        if missing:
            raise ConfigError(
                "缺少环境变量：" + "、".join(missing)
                + "。可以复制 .env.example 为 .env 后填写，或直接在终端 export。"
            )


def load_settings(**overrides) -> Settings:
    load_dotenv()
    max_tokens = _get("ZHIPU_MAX_TOKENS", "LLM_MAX_TOKENS")
    settings = Settings(
        api_key=_get("ZHIPU_API_KEY", "LLM_API_KEY"),
        model=_get("ZHIPU_MODEL", "LLM_MODEL"),
        base_url=_get("ZHIPU_BASE_URL", "LLM_BASE_URL", default=DEFAULT_BASE_URL),
        vision_model=_get("ZHIPU_VISION_MODEL", "LLM_VISION_MODEL"),
        temperature=_float("ZHIPU_TEMPERATURE", 0.2),
        max_tokens=int(max_tokens) if max_tokens else None,
        timeout=_float("ZHIPU_TIMEOUT", 300.0),
        retries=_int("ZHIPU_RETRIES", 4),
        concurrency=max(1, _int("AGENT_CONCURRENCY", 3)),
        fix_rounds=max(0, _int("AGENT_FIX_ROUNDS", 3)),
        node_bin=_get("NODE_BIN", default="node"),
        skills_dir=Path(_get("SKILLS_DIR", default=str(REPO_ROOT / "skills"))),
        direct_chars=_int("AGENT_DIRECT_CHARS", 60000),
        json_mode=_bool("ZHIPU_JSON_MODE", False),
    )
    return replace(settings, **overrides) if overrides else settings
