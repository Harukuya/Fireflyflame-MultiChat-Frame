"""
精力：她自己今天还剩多少劲（原版 energy.ts）

⭐ 和心情分开：心情是她自己此刻的情绪，精力是她还剩多少劲（几十分钟～几小时）。
⭐ 起点跟作息走（夜里最足）；每聊一轮按她自己报的感受变；不聊时每 30 分钟往起点回 10。
⭐ 起点比原版各高 10（用户 2026-10-31 定：原版 夜里 85、闲着／睡觉 70、值勤 55、出任务 45）。
⚠️ 数字不给她看，只给她状态的描述。
"""

from __future__ import annotations

from .jsutil import MIN, day_of
from .schedule import state_at

BASE = {"night": 95, "downtime": 80, "duty": 65, "mission": 55, "sleep": 80}
DELTA = {"up": 4, "ok": -3, "drain": -7}
SESSION_GAP = 30 * MIN
RECOVER_PER_30MIN = 10
TIRED = 50
LOW = 70
RECOVERED = 70
MIN_ROUNDS_TO_SAY = 3
TOO_TIRED_TO_ASK = 20


def _clamp(v: float) -> float:
    return max(0, min(100, v))


def _base_at(now: int) -> int:
    return BASE[state_at(now).activity]


def fresh_energy(now: int) -> dict:
    return {"value": _base_at(now), "at": now, "rounds": 0, "lastRoundAt": 0, "tiredDay": 0, "toldTired": False}


def _settle(e: dict, now: int) -> dict:
    base = _base_at(now)
    if day_of(now) != day_of(e["at"]):
        return {**e, "value": base, "at": now, "rounds": 0, "toldTired": False}
    steps = (now - e["at"]) / (30 * MIN) * RECOVER_PER_30MIN
    value = min(base, e["value"] + steps) if e["value"] < base else max(base, e["value"] - steps)
    out = {**e, "value": value, "at": now}
    if now - e["lastRoundAt"] >= SESSION_GAP:
        out.update(rounds=0, toldTired=False)
    return out


def current_energy(e: dict | None, now: int) -> dict:
    """此刻的精力。不改存档。"""
    return _settle(e or fresh_energy(now), now)


def energy_band(value: float) -> str:
    return "fresh" if value >= LOW else "low" if value >= TIRED else "tired"


def think_steps(value: float) -> int:
    """思考强度（2026-10-29，作者定）：这一轮「回想」「想一想」最多走几步。想不想由她自己决定，这里只管上限。
    有精神 3 步、有点乏 2 步、累了 1 步。"""
    b = energy_band(value)
    return 3 if b == "fresh" else 2 if b == "low" else 1


def energy_factor(value: float) -> float:
    """「意犹未尽」的机会按精力打折"""
    b = energy_band(value)
    return 1 if b == "fresh" else 0.6 if b == "low" else 0.2


def energy_cooldown_factor(value: float) -> float:
    """累了，两次多说之间的冷却拉长一半"""
    return 1.5 if energy_band(value) == "tired" else 1


def take_energy_note(e: dict | None, now: int) -> tuple[str | None, dict]:
    """回他话之前调用：这一轮要不要告诉她累了 / 缓过来了。返回 (要告诉她的, 新的精力)。"""
    e = dict(_settle(e or fresh_energy(now), now))
    note = None
    if e["toldTired"]:
        if e["value"] >= RECOVERED:
            note = "recovered"
            e["toldTired"] = False
        else:
            note = "tired-still"
    elif e["value"] < TIRED and e["rounds"] >= MIN_ROUNDS_TO_SAY:
        note = "tired-again" if e["tiredDay"] == day_of(now) else "tired-first"
        e["tiredDay"] = day_of(now)
        e["toldTired"] = True
    return note, e


def apply_round(e: dict | None, feel: str | None, now: int, deltas: dict | None = None, bonus: float = 0) -> dict:
    """回完一轮：按她报的感受结算（没报就当「还行」）
    deltas、bonus 是这边新加的：和群友聊消耗多些（PEER_DELTA），聊得实在补一点"""
    s = _settle(e or fresh_energy(now), now)
    d = (deltas or DELTA)[feel or "ok"] + bonus
    return {**s, "value": _clamp(s["value"] + d), "rounds": s["rounds"] + 1, "lastRoundAt": now, "at": now}


# 和群友说话（群聊、群友私聊）：更来劲了 +2、还行 −5、有点耗神 −9；聊得实在再 +3
PEER_DELTA = {"up": 2, "ok": -5, "drain": -9}
PEER_REAL_BONUS = 3
