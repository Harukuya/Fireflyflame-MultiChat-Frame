"""
消息节奏（原版 pacing.ts）

⭐ 不用流式输出。先完整生成 → 计算表演时间表 → 播放。用户看到的永远是「她的节奏」。
"""

from __future__ import annotations

import random

from .jsutil import js_round

FALSE_START_CHANCE = 0.06


def jitter(base: float, ratio: float = 0.18) -> int:
    d = base * ratio
    return js_round(base + (random.random() * 2 - 1) * d)


def seen_delay(online: bool) -> int:
    """她「看到消息」之前的空白期"""
    return jitter(2600, 0.5) if online else 0


def typing_duration(text: str) -> int:
    return jitter(min(520 + len(text) * 95, 4200))


def _gap() -> int:
    return jitter(680, 0.4)


def schedule(messages: list[str]) -> list[tuple[str, object]]:
    """把一组消息编排成节拍：("false-start", ms) / ("wait", ms) / ("typing", ms) / ("message", text)"""
    beats: list[tuple[str, object]] = []
    if random.random() < FALSE_START_CHANCE:
        beats.append(("false-start", jitter(1900, 0.35)))
        beats.append(("wait", jitter(900, 0.4)))
    for i, text in enumerate(messages):
        if i > 0:
            beats.append(("wait", _gap()))
        beats.append(("typing", typing_duration(text)))
        beats.append(("message", text))
    return beats
