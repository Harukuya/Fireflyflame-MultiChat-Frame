"""可配置的本地每日作息；状态代码沿用会话引擎协议，文案不绑定角色世界。"""

from __future__ import annotations

from dataclasses import dataclass

from . import profile
from .jsutil import DAY, HOUR, MIN, local_dt, to_i32, to_u32

HOUR_MS = HOUR
DAY_MS = DAY


@dataclass
class HerState:
    online: bool
    location: str  # base=日常；mission=忙碌。兼容现有会话状态机。
    activity: str  # sleep / downtime / night / mission
    her_hour: float
    offset_hours: int = 0
    destination: str | None = None


def hash01(n: int) -> float:
    """保留稳定随机辅助函数接口，不参与默认每日作息。"""
    x = to_i32(to_i32(n) ^ to_i32(0x9E3779B9))
    x = to_i32(x ^ to_i32(x << 13))
    x = to_i32(x ^ (to_u32(x) >> 17))
    x = to_i32(x ^ to_i32(x << 5))
    return (to_u32(x) % 100_000) / 100_000


def _inside(minute: int, start: int, end: int) -> bool:
    return start <= minute < end if start < end else minute >= start or minute < end


def state_at(t: int) -> HerState:
    """毫秒时刻 → 状态。使用运行机器的本地时区，区间开始包含、结束不含。"""
    cfg = profile.schedule_settings()
    dt = local_dt(t)
    minute = dt.hour * 60 + dt.minute
    hour = minute / 60
    busy = cfg["busy_start"] is not None and _inside(minute, cfg["busy_start"], cfg["busy_end"])
    if busy:
        return HerState(False, "mission", "mission", hour)
    online = cfg["mode"] == "always" or _inside(minute, cfg["online_start"], cfg["online_end"])
    activity = ("night" if dt.hour >= 19 or dt.hour < 5 else "downtime") if online else "sleep"
    return HerState(online, "base", activity, hour)


def describe_activity(s: HerState) -> str:
    """仅告诉模型作息事实；没有配置的活动、地点不临时编造。"""
    if s.activity == "mission":
        return "你现在正忙，暂时不能持续聊天；没有提供具体事项，不要编造"
    if not s.online:
        return "你目前在休息，暂时不看消息"
    if s.activity == "night":
        return "现在是本地的夜间，你有空聊天"
    return "你现在有空，可以聊天"


def online_since(now: int) -> int | None:
    """本次连续在线的开始分钟；全天在线且无忙碌区间时没有有限边界。"""
    if not state_at(now).online:
        return None
    minute = now - now % MIN
    for step in range(1, 48 * 60 + 1):
        point = minute - step * MIN
        if not state_at(point).online:
            return point + MIN
    return None


def offline_after(now: int) -> int | None:
    """下一次进入休息/忙碌的精确分钟；全天在线且无忙碌时为 None。"""
    if not state_at(now).online:
        return None
    minute = now - now % MIN
    for step in range(1, 48 * 60 + 1):
        point = minute + step * MIN
        if not state_at(point).online:
            return point
    return None
