"""
亲密度（这边新加的，原版没有）：每个人一个 0～100。

- 基准 60：新认识的从 60 开始。六档：反感 <35、警惕 35～52、一般 53～67、认识 68～79、熟悉 80～89、亲近 ≥90
- 主要联系人固定 90，不打分、不衰减；印象随已有的群聊/私聊记忆整理更新（他那边仍用原版的心情）
- 交给她的是档位的说法，不是数字（数字只在仪表盘上）；还没说上过话的一律按「第一次见，还不熟悉」说
- 每次整理群聊/私聊记忆时，按她那段的感受给每个人 −3～+5，程序限幅：每人每天最多 ±8
- 很久不说话慢慢往回落：高于 60 的每周 −1，落到 60 为止（低于 60 的不回升：不好的印象不会自己消掉）
- 发布版不附带任何人的初始印象；印象只在实际聊天和整理记忆后形成。
"""

from __future__ import annotations

from . import profile

import json
import math
import os
import re

from .jsutil import DAY, day_of, now_ms
from .validation import entity_id

START = 60  # 基准（v0.2 时是 30，旧数据启动时整体 +30）
OLD_START = 30
TRAILBLAZER_VALUE = 90
DAILY_CAP = 8
DELTA_MIN, DELTA_MAX = -3, 5
DECAY_EVERY = 7 * DAY

# 每档后半句：她自己的事（经历、和主要联系人之间的事）对这个人说多少
BAND_TEXT = {
    "反感": "你对{name}印象很不好，不想多搭理：话少、冷淡，不用顺着他的话题走。你自己的事一概不提。",
    "警惕": "你对{name}有点警惕：客气，但保持距离，不多说自己的事，问起就岔开。",
    "一般": "你和{name}说过话，但还不太熟。客气一点，但不用拘谨。你自己的事很少提，问起一笔带过就好。",
    "认识": "你认识{name}，聊过几次，印象不错，还说不上熟。你自己的经历可以说一点，点到为止。",
    "熟悉": "你和{name}挺熟了，说话可以随意些，开开玩笑。你自己的经历可以聊，想说细就说细一点。",
    "亲近": "{name}是你很亲近的朋友，你们彼此信任，说话很放松。你自己的事、心里的事都可以说。",
}
NEVER_TALKED = "你和{name}是第一次见，还不熟悉。客气一点，但不用拘谨。你自己的事先不提。"  # 还没在亲密度里、没说上过话的
TRAILBLAZER_TEXT = "{name}是配置中指定的主要联系人。按角色设定中的关系相处；不要因此编造共同经历或假定恋爱关系。"


def band_of(value: float) -> str:
    if value < 35:
        return "反感"
    if value < 53:
        return "警惕"
    if value < 68:
        return "一般"
    if value < 80:
        return "认识"
    if value < 90:
        return "熟悉"
    return "亲近"


SEED_REASON = "初始印象"


def _impression_text(value) -> str:
    return value.strip()[:80] if isinstance(value, str) else ""


def _trailblazer_placeholder(text: str) -> bool:
    text = "".join(text.split()).replace("（", "(").replace("）", ")").replace("：", ":")
    return text in {"主要联系人:固定90", "主要联系人(固定90)"} or bool(re.fullmatch(r"主要联系人\([^)]*\):固定90", text))


def _valid_time(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value)))


class Intimacy:
    def __init__(self, root: str, trailblazer: str = ""):
        self._path = os.path.join(root, "intimacy.json")
        self.trailblazer = entity_id(trailblazer) or ""
        try:
            with open(self._path, encoding="utf-8") as f:
                self.people: dict[str, dict] = json.load(f)
        except (OSError, ValueError):
            self.people = {}
        self.meta: dict = self.people.pop("_meta", {}) if isinstance(self.people.get("_meta"), dict) else {}
        if self.meta.get("scale") != 2:
            # v0.2 的起点是 30：旧记录整体抬 30，落到同一个刻度上
            for p in self.people.values():
                p["value"] = max(0, min(100, p["value"] + (START - OLD_START)))
            self.meta["scale"] = 2
            if self.people:
                self.save()
        if self.trailblazer:
            self._fix_trailblazer()

    def is_trailblazer(self, qq: str) -> bool:
        return bool(self.trailblazer and qq == self.trailblazer)

    def save(self) -> None:
        """只写真打过交道的：说上过话、打过分、有印象的（只在【刚才在说话的人】里出现过的不落盘）"""
        keep = {q: p for q, p in self.people.items()
                if self.is_trailblazer(q) or p.get("talks") or p.get("scored") or p.get("lastDelta") or p.get("impression")}
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"_meta": self.meta, **keep}, f, ensure_ascii=False)
        os.replace(tmp, self._path)

    def _fix_trailblazer(self) -> None:
        p = self.get(self.trailblazer, "" if self.trailblazer in self.people else profile.primary_name())
        if p["value"] != TRAILBLAZER_VALUE or not p.get("talks"):
            p.update(value=TRAILBLAZER_VALUE, talks=max(1, p.get("talks", 0)), lastReason="主要联系人（固定 90）")
            self.save()

    def get(self, qq: str, name: str = "", group: str | None = None) -> dict:
        """⭐ 新账号就在这里建：第一次碰到（在场的人那一行、说上话、整理记忆）时从基准 60 开始；
        说上话（touch）或整理记忆（apply）时才写进 intimacy.json"""
        now = now_ms()
        p = self.people.get(qq)
        if p is None:
            p = self.people[qq] = {"value": START, "name": name or qq, "groups": [], "talkedAt": now, "decayedAt": now,
                                   "day": 0, "dayGain": 0, "lastDelta": 0, "lastReason": "", "talks": 0}
        if name:
            p["name"] = name
        if group and group not in p["groups"]:
            p["groups"].append(group)
        if self._decay(qq, p, now):
            self.save()
        return p

    def _decay(self, qq: str, p: dict, now: int) -> bool:
        if self.is_trailblazer(qq):
            return False
        base = max(p["talkedAt"], p["decayedAt"])
        weeks = math.floor((now - base) / DECAY_EVERY)
        if weeks > 0 and p["value"] > START:
            p["value"] = max(START, p["value"] - weeks)
            p["decayedAt"] = base + weeks * DECAY_EVERY
            return True
        return False

    def touch(self, qq: str, name: str = "", group: str | None = None) -> None:
        """她和这个人说上话了（不改数值，只记时间）"""
        p = self.get(qq, name, group)
        p["talkedAt"] = now_ms()
        p["talks"] = p.get("talks", 0) + 1
        self.save()

    def apply(self, qq: str, delta, reason: str, name: str = "", impression: str = "", *,
              observed_at: int | None = None) -> float:
        """整理记忆时给的变化：限幅到 −3～+5，每人每天最多 ±8。主要联系人只更新印象，数值固定 90。
        impression：她现在对这个人的印象（有变化才给），替换掉原来的。
        observed_at：这段对话结束的时间；较早会话晚整理完，不能覆盖较新的印象。"""
        now = now_ms()
        p = self.get(qq)
        observed = observed_at if _valid_time(observed_at) else now
        previous_at = p.get("impressionAt", 0)
        latest = observed >= (previous_at if _valid_time(previous_at) else 0)
        if name and latest:
            p["name"] = name
        text = _impression_text(impression)
        if self.is_trailblazer(qq) and _trailblazer_placeholder(text):
            text = ""
        updated = bool(text and latest)
        if updated:
            p["impression"] = text
            p["impressionAt"] = observed
        if self.is_trailblazer(qq):
            fixed = p["value"] != TRAILBLAZER_VALUE
            p["value"] = TRAILBLAZER_VALUE
            if updated or fixed:
                self.save()
            return 0  # 不记 scored、日增量、打分理由或衰减时间
        if updated:
            self.save()
        if not isinstance(delta, (int, float)) or isinstance(delta, bool) or (isinstance(delta, float) and not math.isfinite(delta)):
            return 0
        p["scored"] = True  # 打过分（这次打 0 也算）：之后一直写进 intimacy.json
        d = max(DELTA_MIN, min(DELTA_MAX, round(delta)))
        today = day_of(now)
        if p["day"] != today:
            p["day"], p["dayGain"] = today, 0
        room_up, room_down = DAILY_CAP - p["dayGain"], DAILY_CAP + p["dayGain"]
        d = max(-room_down, min(room_up, d))
        p["value"] = max(0, min(100, p["value"] + d))
        p["dayGain"] += d
        p["lastDelta"], p["lastReason"], p["talkedAt"] = d, str(reason or "")[:60], now
        self.save()
        return d

    def describe(self, qq: str, name: str) -> str:
        """还没说上过话的（不在亲密度里的新账号），一律是「第一次见，还不熟悉」（基准 60 是「一般」那一档，但她其实还不认识）"""
        p = self.get(qq, name)
        if self.is_trailblazer(qq):
            return TRAILBLAZER_TEXT.format(name=name or p["name"])
        if not p.get("talks", 0):
            return NEVER_TALKED.format(name=name or p["name"])
        return BAND_TEXT[band_of(p["value"])].format(name=name or p["name"])

    def cognition(self, qq: str, name: str) -> str:
        """她对这个人的认知：档位那句 + 印象（回话时的【刚才在说话的人】、群友私聊、看手机的判断都用这个）"""
        p = self.get(qq, name)
        imp = self.impression(qq) if p.get("talks", 0) else ""
        label = "此前形成的印象" if self.is_trailblazer(qq) else f"你对{name or p['name']}的印象"
        return self.describe(qq, name) + (f"{label}：{imp}" if imp else "")

    def impression(self, qq: str) -> str:
        """同一个 账号 的当前印象；群聊和私聊共用，不把旧主要联系人数值占位交给模型。"""
        text = _impression_text(self.people.get(qq, {}).get("impression"))
        return "" if self.is_trailblazer(qq) and _trailblazer_placeholder(text) else text

    def trailblazer_note(self) -> str:
        """主要联系人的关系与当前印象，不含亲密度分数；尚无印象正文时留空。"""
        if not self.trailblazer or not self.impression(self.trailblazer):
            return ""
        p = self.get(self.trailblazer)
        return ("【你对主要联系人的印象】\n用于理解相处方式，不代表其中的事刚刚发生，也不是逐字原话。\n"
                + self.cognition(self.trailblazer, p["name"]))

    def brief(self, qq: str, name: str) -> str:
        """短的：「一般；印象：友善」（接话窗口的判断用）"""
        p = self.get(qq, name)
        if self.is_trailblazer(qq):
            imp = self.impression(qq)
            return "主要联系人；关系以角色设定和实际互动为准" + (f"；此前形成的印象：{imp}" if imp else "")
        if not p.get("talks", 0):
            return "第一次见"
        imp = p.get("impression")
        return band_of(p["value"]) + (f"；印象：{imp}" if imp else "")

    def roster_line(self, qq: str, name: str) -> str:
        """整理记忆时名单上的一行：现在的关系、印象、上次变化的理由——让模型判断「这次比原来近了还是远了」"""
        p = self.get(qq, name)
        if self.is_trailblazer(qq):
            return self.cognition(qq, name)
        last = p.get("lastReason") if p.get("lastReason") not in ("", SEED_REASON) else ""
        return self.cognition(qq, name) + (f"上次：{last}" if last else "")
