"""
念头：她说完话，心里还挂着点什么（原版 thought.ts）

⭐⭐ 真人发完消息不是「排个闹钟」，而是心里留个念头，过一会儿再掂量要不要说。
  留不留、多久以后、到点说不说 → 她决定；时间和上限 → 程序管。
⭐ 他先开口了，念头不作废——带进她的下一次回复。
⭐ 念头只能在回他话的那轮留，掂量那轮不给「留个念头」，链条到这儿自然就断了。
"""

from __future__ import annotations

import math
import uuid

from .jsutil import HOUR, MIN, day_of, js_round, rand_between, strip_ps
from .mood import band_of
from .heat import heat_cooldown_factor
from .energy import energy_cooldown_factor

DELAY = {"soon": (15_000, 60_000), "a_while": (2 * MIN, 10 * MIN), "later": (30 * MIN, 90 * MIN)}
COOLDOWN = 30 * MIN
COOLDOWN_LOW = HOUR
LATER_PER_DAY = 2
SETTLE = (5 * MIN, 20 * MIN)
TTL = 24 * HOUR
MISS_GRACE = 20 * MIN
PLANT_DELAY = {"hot": (40_000, 3 * MIN), "warm": (2 * MIN, 6 * MIN)}
CURIOUS_DELAY = (MIN, 3 * MIN)


def empty_ledger() -> dict:
    return {"lastFollowUpAt": 0, "laterDay": 0, "laterN": 0}


def _thought(topic: str, tier: str, created: int, due: float, **extra) -> dict:
    return {"id": uuid.uuid4().hex[:12], "topic": topic, "tier": tier, "createdAt": created,
            "dueAt": due, "waited": False, **extra}


def cooldown_ms(mood_value: float, heat: float = 0, energy: float = 100) -> float:
    b = band_of(mood_value)
    if b in ("cool", "distant"):
        return COOLDOWN_LOW
    return COOLDOWN * heat_cooldown_factor(heat) * energy_cooldown_factor(energy)


def available_tiers(ledger: dict, mood_value: float, heat: float, energy: float, now: int) -> list[str]:
    """这一轮她能留哪几档念头。空 = 这轮不给「留个念头」。"""
    cooling = now - ledger["lastFollowUpAt"] < cooldown_ms(mood_value, heat, energy)
    later_full = ledger["laterDay"] == day_of(now) and ledger["laterN"] >= LATER_PER_DAY
    out = [] if cooling else ["soon", "a_while"]
    if not later_full:
        out.append("later")
    return out


def plan_thought(ledger: dict, topic: str, tier: str, mood_value: float, now: int) -> dict:
    """她留了个念头"""
    due = now + rand_between(*DELAY[tier])
    # 「晚点」也不能落在冷却期里
    if tier == "later":
        due = max(due, ledger["lastFollowUpAt"] + cooldown_ms(mood_value))
    return _thought(topic, tier, now, due)


def plant_thought(heat: float, now: int) -> dict:
    """聊得起劲，替她留一个「意犹未尽」的念头（没有具体内容）"""
    hot = heat >= 0.6
    return _thought("", "soon" if hot else "a_while", now, now + rand_between(*PLANT_DELAY["hot" if hot else "warm"]),
                    planted=True)


def curious_thought(ledger: dict, topic: str, now: int) -> tuple[dict | None, dict]:
    """她对他说的事好奇：1～3 分钟后掂量要不要追问。同一件事今天只追一次。返回 (念头, 新账本)。"""
    asked = (ledger.get("asked") or []) if ledger.get("askedDay") == day_of(now) else []
    key = strip_ps(topic)
    if key in asked:
        return None, ledger
    new_ledger = {**ledger, "askedDay": day_of(now), "asked": [*asked, key][-20:]}
    return _thought(topic, "a_while", now, now + rand_between(*CURIOUS_DELAY), curious=True), new_ledger


def postpone(t: dict, now: int) -> dict:
    """再等等：马上/一会儿 → 再过 2～10 分钟；晚点 → 再过 30 分钟以后"""
    return {**t, "waited": True, "dueAt": now + rand_between(*(DELAY["later"] if t["tier"] == "later" else DELAY["a_while"]))}


def is_alive(t: dict, now: int) -> bool:
    if now - t["createdAt"] > TTL:
        return False
    if t["tier"] != "later" and now - t["dueAt"] > MISS_GRACE:
        return False
    return True


def due_time(t: dict, online_since_ms: int | None) -> float:
    """「晚点」的念头赶上她刚上线，先等她安顿下来。"""
    if t["tier"] != "later" or online_since_ms is None:
        return t["dueAt"]
    k = abs(math.sin(t["createdAt"]))
    return max(t["dueAt"], online_since_ms + SETTLE[0] + k * (SETTLE[1] - SETTLE[0]))


def user_spoke_since(t: dict, history: list[dict]) -> bool:
    """⭐ 说过就不掂量了——掂量时会告诉她「之后他一直没再说话」，这句必须是真的。"""
    return any(not m.get("system") and m["role"] == "user" and m["ts"] > t["createdAt"] for m in history)


def note_delivered(ledger: dict, tier: str, now: int) -> dict:
    """她真的多发了消息——记一笔，冷却和「晚点」的次数从这里算"""
    today = day_of(now)
    n = ledger["laterN"] if ledger["laterDay"] == today else 0
    return {**ledger, "lastFollowUpAt": now, "laterDay": today, "laterN": n + 1 if tier == "later" else n}


def ago_text(ms: float) -> str:
    """「那是多久以前的事了」"""
    m = ms / MIN
    if m < 1:
        return "不到一分钟"
    if m < 60:
        return f"{js_round(m)}分钟"
    if m < 24 * 60:
        return f"大约{js_round(m / 60)}小时"
    return "一天多"

