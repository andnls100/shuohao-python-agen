"""运行配置。

两种配置方式：
  1. 简单：环境变量 / 项目根目录的 .env。一个平台、一个文本模型，可选一个视觉模型
     （视觉模型默认和文本模型用同一个平台，也可以单独指定地址和 Key）。
  2. 多模型 / 多平台（号池）：项目根目录的 models.json（见 models.example.json）。
     文本模型和视觉模型各是一个列表，每一项有自己的 base_url、api_key、model；
     调用时按策略轮换，某一项出错会自动换下一项。存在 models.json 时以它为准。
代码里不放任何密钥。变量名以 ZHIPU_ 开头，同时兼容 LLM_ 开头的同名变量，
方便换成其他兼容 OpenAI 接口的平台。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
class Endpoint:
    """号池里的一项：一个平台上的一个模型。None 的参数沿用全局设置。"""
    name: str
    base_url: str
    api_key: str
    model: str
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout: Optional[float] = None
    stream: Optional[bool] = None
    json_mode: Optional[bool] = None

    def label(self) -> str:
        return f"{self.name}（{self.model}）"


STRATEGIES = ("failover", "round_robin")


@dataclass(frozen=True)
class Settings:
    api_key: str               # 单平台配置：文本模型的 Key（用了 models.json 时取第一项，仅供显示）
    model: str
    base_url: str
    vision_model: str          # 可选：staging 认图需要能看图的模型
    temperature: float
    max_tokens: Optional[int]  # None = 不传，使用平台默认
    timeout: float
    retries: int               # 每个平台上遇到网络类错误时重试几次，再换下一个平台
    concurrency: int           # 同时进行的模型调用数
    fix_rounds: int            # 校验不过时，最多回炉几轮
    node_bin: str
    skills_dir: Path
    direct_chars: int          # 原文不超过这个字数就整本直接喂给模型，跳过分卷摘要
    stream: bool               # 流式调用：边生成边收，能看到进度，也避免长时间无响应被断开
    json_mode: bool            # 是否传 response_format=json_object（部分模型不支持，默认关）
    vision_base_url: str = ""  # 视觉模型单独的地址；空 = 和文本模型同一个平台
    vision_api_key: str = ""   # 视觉模型单独的 Key；空 = 和文本模型同一个
    text_pool: Tuple[Endpoint, ...] = ()    # 来自 models.json；空 = 用上面的单平台字段
    vision_pool: Tuple[Endpoint, ...] = ()
    strategy: str = "failover"              # failover：按顺序，前面的出错才用后面的；round_robin：轮流
    cooldown: float = 120.0                 # 出错的平台，这么多秒内排到最后再试
    models_file: str = ""                   # 实际读取的 models.json 路径（没有则空）

    # -- 号池：统一从这里取，调用方不用关心配置来自哪种方式
    def text_endpoints(self) -> Tuple[Endpoint, ...]:
        if self.text_pool:
            return self.text_pool
        if self.api_key and self.model:
            return (Endpoint("default", self.base_url, self.api_key, self.model),)
        return ()

    def vision_endpoints(self) -> Tuple[Endpoint, ...]:
        if self.vision_pool:
            return self.vision_pool
        if self.vision_model:
            key = self.vision_api_key or self.api_key
            if key:
                return (Endpoint("default-vision", self.vision_base_url or self.base_url, key, self.vision_model),)
        return ()

    def endpoints(self, kind: str) -> Tuple[Endpoint, ...]:
        if kind == "vision":
            return self.vision_endpoints()
        if kind == "text":
            return self.text_endpoints()
        raise ValueError(f"未知的模型类别：{kind!r}（应为 text 或 vision）")

    def require(self) -> None:
        if self.text_pool:
            return
        missing = []
        if not self.api_key:
            missing.append("ZHIPU_API_KEY")
        if not self.model:
            missing.append("ZHIPU_MODEL")
        if missing:
            raise ConfigError(
                "缺少环境变量：" + "、".join(missing)
                + "。可以复制 .env.example 为 .env 后填写，或直接在终端 export；"
                "要配置多个模型 / 多个平台，复制 models.example.json 为 models.json。"
            )


# ---------------------------------------------------------------- models.json

def models_file_path() -> Path:
    custom = _get("AGENT_MODELS_FILE")
    if custom:
        return Path(custom)
    primary = REPO_ROOT / "models.json"
    alias = REPO_ROOT / "model.json"  # 常见的少写一个 s；models.json 不存在时才认它
    return alias if (not primary.is_file() and alias.is_file()) else primary


def _keys_from(entry: Dict[str, Any], label: str, name: str) -> List[str]:
    """一项里的所有 Key：api_key（单个）、api_keys（列表）、api_key_env（环境变量名）、
    api_keys_env（环境变量里逗号分隔的多个 Key）。按这个顺序合并，去掉重复。"""
    keys: List[str] = []
    single = str(entry.get("api_key") or "").strip()
    if single:
        keys.append(single)
    many = entry.get("api_keys")
    if many is not None:
        if not isinstance(many, list) or not all(isinstance(k, str) for k in many):
            raise ConfigError(f"{label}（{name}）的 api_keys 必须是字符串列表，例如 [\"Key1\", \"Key2\"]")
        keys.extend(k.strip() for k in many if k.strip())
    for field, split in (("api_key_env", False), ("api_keys_env", True)):
        env_name = str(entry.get(field) or "").strip()
        if not env_name:
            continue
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise ConfigError(f"{label}（{name}）的 {field} 指向环境变量 {env_name}，但它没有设置")
        keys.extend([k.strip() for k in value.split(",") if k.strip()] if split else [value])
    unique: List[str] = []
    for k in keys:
        if k not in unique:
            unique.append(k)
    return unique


def _endpoints_from(entry: Any, where: str, index: int) -> List[Endpoint]:
    """一项配置展开成一个或多个 Endpoint：同一个平台、同一个模型，每个 Key 一项（互为备份，也各自限流）。"""
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}[{index}] 应当是一个对象，当前是 {type(entry).__name__}")
    label = f"{where}[{index}]"
    name = str(entry.get("name") or f"{where}-{index + 1}")
    base_url = str(entry.get("base_url") or "").strip()
    model = str(entry.get("model") or "").strip()
    keys = _keys_from(entry, label, name)
    problems = [f for f, v in (("base_url", base_url), ("model", model),
                               ("api_key / api_keys / api_key_env", keys)) if not v]
    if problems:
        raise ConfigError(f"{label}（{name}）缺少：{'、'.join(problems)}")

    def num(field: str, kind):
        value = entry.get(field)
        if value is None:
            return None
        try:
            return kind(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{label}（{name}）的 {field} 不合法：{value!r}") from exc

    shared = dict(
        base_url=base_url, model=model,
        temperature=num("temperature", float), max_tokens=num("max_tokens", int),
        timeout=num("timeout", float), stream=num("stream", bool), json_mode=num("json_mode", bool),
    )
    if len(keys) == 1:
        return [Endpoint(name=name, api_key=keys[0], **shared)]
    return [Endpoint(name=f"{name}#{i + 1}", api_key=k, **shared) for i, k in enumerate(keys)]


def load_models_file(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """读取 models.json。文件不存在返回 None；存在但写错了就报清楚是哪一项。"""
    path = Path(path) if path else models_file_path()
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ConfigError(f"{path} 不是合法的 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} 顶层应当是对象，包含 text / vision 两个列表")
    text = raw.get("text")
    if not isinstance(text, list) or not text:
        raise ConfigError(f"{path} 里的 text 必须是至少一项的列表")
    vision = raw.get("vision", [])
    if not isinstance(vision, list):
        raise ConfigError(f"{path} 里的 vision 必须是列表（不需要视觉模型就删掉这一项或写 []）")
    strategy = str(raw.get("strategy", "failover"))
    if strategy not in STRATEGIES:
        raise ConfigError(f"{path} 里的 strategy 只能是 {' 或 '.join(STRATEGIES)}，当前是 {strategy!r}")
    try:
        cooldown = float(raw.get("cooldown", 120))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path} 里的 cooldown 必须是数字（秒）") from exc
    pools: Dict[str, Tuple[Endpoint, ...]] = {}
    for kind, entries in (("text", text), ("vision", vision)):
        seen = set()
        built: List[Endpoint] = []
        for i, entry in enumerate(entries):
            group = _endpoints_from(entry, kind, i)
            base_name = str(entry.get("name") or f"{kind}-{i + 1}") if isinstance(entry, dict) else ""
            if base_name in seen:
                raise ConfigError(f"{path} 里 {kind} 有重名的项：{base_name}（name 不能重复）")
            seen.add(base_name)
            built.extend(group)
        pools[kind] = tuple(built)
    return {"text": pools["text"], "vision": pools["vision"], "strategy": strategy,
            "cooldown": cooldown, "path": str(path)}


def load_settings(**overrides) -> Settings:
    load_dotenv()
    max_tokens = _get("ZHIPU_MAX_TOKENS", "LLM_MAX_TOKENS")
    pool = load_models_file()
    settings = Settings(
        api_key=_get("ZHIPU_API_KEY", "LLM_API_KEY"),
        model=_get("ZHIPU_MODEL", "LLM_MODEL"),
        base_url=_get("ZHIPU_BASE_URL", "LLM_BASE_URL", default=DEFAULT_BASE_URL),
        vision_model=_get("ZHIPU_VISION_MODEL", "LLM_VISION_MODEL"),
        vision_base_url=_get("ZHIPU_VISION_BASE_URL", "LLM_VISION_BASE_URL"),
        vision_api_key=_get("ZHIPU_VISION_API_KEY", "LLM_VISION_API_KEY"),
        temperature=_float("ZHIPU_TEMPERATURE", 0.2),
        max_tokens=int(max_tokens) if max_tokens else None,
        timeout=_float("ZHIPU_TIMEOUT", 300.0),
        retries=_int("ZHIPU_RETRIES", 4),
        concurrency=max(1, _int("AGENT_CONCURRENCY", 3)),
        fix_rounds=max(0, _int("AGENT_FIX_ROUNDS", 1)),
        node_bin=_get("NODE_BIN", default="node"),
        skills_dir=Path(_get("SKILLS_DIR", default=str(REPO_ROOT / "skills"))),
        direct_chars=_int("AGENT_DIRECT_CHARS", 60000),
        stream=_bool("ZHIPU_STREAM", True),
        json_mode=_bool("ZHIPU_JSON_MODE", False),
        text_pool=pool["text"] if pool else (),
        vision_pool=pool["vision"] if pool else (),
        strategy=pool["strategy"] if pool else _get("AGENT_POOL_STRATEGY", default="failover"),
        cooldown=pool["cooldown"] if pool else 120.0,
        models_file=pool["path"] if pool else "",
    )
    if settings.strategy not in STRATEGIES:
        raise ConfigError(f"AGENT_POOL_STRATEGY 只能是 {' 或 '.join(STRATEGIES)}")
    return replace(settings, **overrides) if overrides else settings
