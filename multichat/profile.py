"""角色配置与可编辑提示词；只保存框架需要的公开字段，不保留服务密钥。"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path

_DEFAULTS = {
    "character_name": "小伴",
    "primary_user_name": "主要联系人",
    "identity": "【身份】\n你是{character_name}，一个喜欢读书和散步的聊天伙伴。这是可替换的虚构样例；不要擅自编造与任何人的共同经历或特殊关系。",
    "voice": "【说话方式】\n语气温和、友善、自然，有自己的判断。认真接住对方正在说的事，不套用客服话术。",
    "boundaries": "【认知和相处边界】\n只按已提供的人设、资料与真实对话说话。不确定就说明，不把样例或别人的话当成自己的经历。不默认任何人是恋人；关系以实际互动和明确设定为准。",
    "schedule": {"mode": "daily", "online_start": 480, "online_end": 1380,
                 "busy_start": None, "busy_end": None},
}
_current = dict(_DEFAULTS)
_context: ContextVar[dict | None] = ContextVar("multichat_profile", default=None)


def _active() -> dict:
    current = _context.get()
    return current if current is not None else _current


def _name(config: dict, key: str) -> str:
    value = config.get(key, _DEFAULTS[key])
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是文本")
    value = value.strip() or _DEFAULTS[key]
    if len(value) > 80 or "\n" in value or "\r" in value:
        raise ValueError(f"{key} 请填写不超过 80 字的单行名称")
    return value


def _minute(value: object, key: str, optional: bool = False) -> int | None:
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError(f"{key} 需要 HH:MM 格式，例如 08:00")
    h, m = map(int, value.split(":"))
    return h * 60 + m


def prepare(config: dict, prompts_dir: str | Path) -> dict:
    """返回独立快照；调用者应视为只读，apply/use 再复制后提交。失败不改当前人设。"""
    result = {key: _name(config, key) for key in ("character_name", "primary_user_name")}
    directory = Path(prompts_dir)
    for key in ("identity", "voice", "boundaries"):
        path = directory / f"{key}.md"
        try:
            content = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"无法读取角色提示词 {path.name}（请使用 UTF-8）") from exc
        if not content:
            raise ValueError(f"角色提示词 {path.name} 不能为空")
        result[key] = content
    mode = config.get("schedule_mode", "daily")
    if mode not in ("daily", "always"):
        raise ValueError("schedule_mode 只能是 daily 或 always")
    schedule = {"mode": mode}
    for key, default in (("online_start", "08:00"), ("online_end", "23:00"),
                         ("busy_start", ""), ("busy_end", "")):
        schedule[key] = _minute(config.get(key, default), key, key.startswith("busy"))
    if schedule["online_start"] == schedule["online_end"]:
        raise ValueError("在线开始、结束时间不能相同；全天在线请选择 always")
    if (schedule["busy_start"] is None) != (schedule["busy_end"] is None):
        raise ValueError("忙碌开始、结束时间必须一起填写或一起留空")
    if schedule["busy_start"] is not None and schedule["busy_start"] == schedule["busy_end"]:
        raise ValueError("忙碌开始、结束时间不能相同")
    result["schedule"] = schedule
    return result


def apply(prepared: dict) -> None:
    """提交 prepare 生成的快照；配置及提示词一起生效。"""
    global _current
    _current = deepcopy(prepared)


def configure(config: dict, prompts_dir: str | Path) -> None:
    apply(prepare(config, prompts_dir))


@contextmanager
def use(prepared: dict):
    """给独立 Runtime 临时绑定 prepare 快照；新 asyncio 任务会继承本上下文。"""
    token = _context.set(deepcopy(prepared))
    try:
        yield
    finally:
        _context.reset(token)


def bot_name() -> str:
    return _active()["character_name"]


def primary_name() -> str:
    return _active()["primary_user_name"]


def prompt(name: str) -> str:
    """只替换两个已知占位符，JSON 等其他花括号原样保留。"""
    return (_active()[name].replace("{character_name}", bot_name())
            .replace("{primary_user_name}", primary_name()))


def schedule_settings() -> dict:
    return dict(_active()["schedule"])


def self_block(default: str | None = None) -> str | None:
    """Runtime 明确写入 None 也有效，不借用另一个实例的自我认知。"""
    return _active().get("self_block", default)
