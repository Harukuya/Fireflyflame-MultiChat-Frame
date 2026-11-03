"""
聊得起不起劲（原版 heat.ts）：只看最近 20 分钟，0～1。

⭐ 越起劲：越可能替她留一个「意犹未尽」的念头；两次多说之间的冷却越短。
情绪很低时不替她自动留念头；这是主动倾向，不表示与某个人疏远。
"""

from __future__ import annotations

from .jsutil import MIN
from .mood import band_of

WINDOW = 20 * MIN


def chat_heat(history: list[dict], meaningful: bool, mood_value: float, now: int) -> float:
    band = band_of(mood_value)
    if band == "distant":
        return 0
    recent = [m for m in history if not m.get("system") and now - m["ts"] < WINDOW]
    mine = [m for m in recent if m["role"] == "user"]
    if not mine:
        return 0
    rounds = min(len(mine), 5) * 0.08
    quick = answered = 0
    for i in range(1, len(recent)):
        if recent[i]["role"] != "user" or recent[i - 1]["role"] != "her":
            continue
        answered += 1
        if recent[i]["ts"] - recent[i - 1]["ts"] < 2 * MIN:
            quick += 1
    fast = (quick / answered) * 0.2 if answered else 0
    avg_len = sum(len(m["text"]) for m in mine) / len(mine)
    long_ = 0.1 if avg_len >= 12 else 0
    deep = 0.2 if meaningful else 0
    mood_adj = 0.1 if band == "warm" else -0.2 if band == "cool" else 0
    return max(0, min(1, rounds + fast + long_ + deep + mood_adj))


def plant_chance(heat: float) -> float:
    return min(0.7, heat * 0.7)


def heat_cooldown_factor(heat: float) -> float:
    return 1 - 0.75 * heat
