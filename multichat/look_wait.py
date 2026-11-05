"""每群独立的平滑随机等待；预算归一化后可随频率／疲劳连续调整。

Gamma(2, 0.5) 的均值为 1。按每小时 rate 扣预算时，等待均值为
1/rate 小时；只在群可看时推进，10 分钟冷却由调用方单独处理。
"""
from __future__ import annotations

import math
import random

from .jsutil import HOUR


def rate_per_ms(per_hour: float, tired: bool) -> float:
    try:
        rate = float(per_hour)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(rate) or rate <= 0:
        return 0.0
    return rate / HOUR * (2 / 3 if tired else 1)


def chance(dt_ms: float, per_hour: float, tired: bool) -> float:
    """整个有效等待时段的 CDF，只供说明／校验；不能反复用累计值抽签。"""
    rate = rate_per_ms(per_hour, tired)
    if not rate:
        return 0.0
    elapsed = max(0.0, float(dt_ms))
    x = 2 * rate * elapsed
    if not math.isfinite(x):
        return 1.0 if x > 0 else 0.0
    return min(1.0, max(0.0, -math.expm1(-x) - x * math.exp(-x)))


def advance(wait: dict | None, dt_ms: float, per_hour: float, tired: bool) -> dict | None:
    """返回可直接存盘的新预算。剩余 0 是已到点，直到真正阅读才消费。"""
    rate = rate_per_ms(per_hour, tired)
    if not rate:
        return wait
    value = wait.get("remaining") if isinstance(wait, dict) else None
    valid = (isinstance(wait, dict) and type(wait.get("v")) is int and wait["v"] == 1
             and type(value) in (int, float) and math.isfinite(value) and value >= 0)
    if not valid:
        value = random.gammavariate(2.0, 0.5)
    elapsed = float(dt_ms)
    elapsed = max(0.0, elapsed) if math.isfinite(elapsed) else 0.0
    return {"v": 1, "remaining": max(0.0, value - rate * elapsed)}
