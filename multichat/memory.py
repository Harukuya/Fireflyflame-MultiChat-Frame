"""
记忆（原版 memory.ts）

⭐ 知识库 ≠ 记忆：这里只存他和她之间发生的事。
⭐ 她下线时把这次聊的收成记忆；一直在线的话，3 小时没人说话也整理。
⭐ 低重要性记忆在 3/7/14 天变模糊，30 天后删除；重要记忆保留原来的强化与降精度规则。
⭐ 按字面和按意思各算一个分，换算到同一把尺子上（1 = 刚好够），取高的；模型用不了时只按字面。
"""

from __future__ import annotations

from . import profile

import asyncio
import math
import random
import re

import numpy as np

from . import persona
from .caption import image_note
from .quotes import render_quote
from .events import redact
from .embed import MODEL_ID, VECTOR_FORMAT, Embedder, pack_vec, unpack_vec
from .jsutil import DAY, HOUR, MIN, js_trim, local_dt, message_time, strip_ps, now_ms
from .schedule import state_at
from .text import FUNCTION_CHARS, split_clauses, tokens
from .tools import FACT_CATEGORIES, SAVE_MEMORY, TAG_MEMORIES
from .winext import no_reply_mark, time_block

CORE = ["基本情况", "你们之间"]
QUIET_CLOSE = 3 * HOUR
SESSION_GAP = 3 * HOUR
SESSION_MAX = 150
RETRY_AFTER = 10 * MIN
FACTS_MAX = 40
CORE_SHOWN = 12
THREADS_MAX = 6
THREAD_TTL = 7 * DAY
GAP_MIN = 6 * HOUR
GAP_RHYTHM = DAY
WORDS_PER_RUN = 5
FACTS_PER_RUN = 8
LEVEL_DAYS = [7, 23, 60]
LOW_LEVEL_DAYS = [3, 4, 7]  # 从清晰开始累计 3/7/14 天；回想可回升，但不能延长 30 天硬上限。
LOW_MEMORY_TTL = 30 * DAY
LEVEL_NAME = ["清晰", "大概", "模糊", "很模糊"]
WORKING = 50
ASSOC_MAX = 3
ASSOC_MIN = 1.0
NOISE_CHANCE = 0.1
RECALL_MAX = 3
RECALL_MIN = 0.3
RECALL_COUNT_GAP = 30 * MIN
SEM_ASSOC = 0.40
SEM_ASSOC_FLOOR = 0.32
SEM_RECALL = 0.30
SEM_RECALL_FLOOR = 0.22
MAX_INLINE_EMBED = 12
TAG_BATCH = 20


def empty_memory() -> dict:
    return {"episodes": [], "words": [], "facts": [], "threads": [], "doneMsgId": None, "doneTs": 0,
            "seq": 1, "failedAt": 0, "haze": [], "hazeFailedAt": 0}


def active_facts(m: dict) -> list[dict]:
    return [f for f in m["facts"] if not f.get("retiredAt")]


def thread_expires_at(t: dict) -> int:
    """旧数据也按原 createdAt 起算七天，不因升级／重新读盘获得新期限。"""
    created = _integer(t.get("createdAt")) or 0
    expires = _integer(t.get("expiresAt"))
    return min(created + THREAD_TTL, expires if expires is not None else created + THREAD_TTL)


def open_threads(m: dict, now: int) -> list[dict]:
    return [t for t in m["threads"] if not t.get("closedAt") and now < thread_expires_at(t)][:THREADS_MAX]


def episode_expired(e: dict, now: int) -> bool:
    return e.get("weight", 2) <= 3 and now - e["end"] >= LOW_MEMORY_TTL


def core_facts(m: dict) -> list[dict]:
    return sorted([f for f in active_facts(m) if f["category"] in CORE], key=lambda f: -f["updatedAt"])[:CORE_SHOWN]


def _str(v, n: int) -> str:
    return js_trim(v)[:n] if isinstance(v, str) else ""


def _int(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return math.nan
    return math.floor(v + 0.5)


def _arr(v) -> list:
    return v if isinstance(v, list) else []


def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def _integer(v) -> int | None:
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return int(v)
    return None


def _thread_key(content: str) -> str:
    return re.sub(r"[\s，,。.!！?？；;：:]+", "", content).casefold()


def _thread_version(t: dict) -> tuple:
    return tuple(t.get(k) for k in ("content", "createdAt", "expiresAt", "kind", "reason", "closedAt"))


def _episode_version(e: dict, m: dict) -> tuple:
    haze = next((h.get("text") for h in m["haze"] if h["id"] == e.get("hazeId")), None)
    return (e.get("summary"), e.get("gist"), e.get("feeling"), e.get("level"), e.get("hazeId"),
            tuple(e.get("topics") or []), haze)


def _apply_threads(m: dict, inp: dict, before: dict, learned_at: int, next_id) -> None:
    """同次整理中的明确关闭优先；等待时被修改/关闭的旧事项不覆盖、不复活。"""
    for raw_id in _arr(inp.get("closed_threads")):
        tid = _integer(raw_id)
        old = before.get(tid)
        t = next((x for x in m["threads"] if x["id"] == tid), None)
        if old is not None and t and _thread_version(t) == old:
            t["closedAt"] = learned_at
    for raw in _arr(inp.get("threads"))[:THREADS_MAX]:
        item = _obj(raw)
        content, reason = _str(item.get("content"), 80), _str(item.get("reason"), 120)
        days = _integer(item.get("keep_days"))
        kind = item.get("kind")
        if not content or not reason or days is None or kind not in ("约定", "承诺", "重要待定"):
            continue
        expires = learned_at + min(7, max(1, days)) * DAY
        key = _thread_key(content)
        if not key or not _thread_key(reason):
            continue
        if "id" in item:
            tid = _integer(item.get("id"))
            t = next((x for x in open_threads(m, learned_at) if x["id"] == tid), None)
            if not t or before.get(tid) != _thread_version(t):
                continue
            if _thread_key(t["content"]) == key:
                continue  # 重复或仅标点变动，绝不续期。
            if any(x["id"] != tid and _thread_key(x["content"]) == key for x in open_threads(m, learned_at)):
                continue
            t.update(content=content, kind=kind, reason=reason,
                     expiresAt=min(thread_expires_at(t), expires))
            continue
        if any(_thread_key(x["content"]) == key for x in m["threads"]):
            continue
        # 这一请求开始时还挂着、后来被关掉的同一件事，不能换成新 id 复活。
        if any(_thread_key(old[0]) == key and not any(
                x["id"] == tid and not x.get("closedAt") for x in m["threads"])
               for tid, old in before.items()):
            continue
        if len(open_threads(m, learned_at)) >= THREADS_MAX:
            continue  # 新事项不自动挤掉旧事项；模型需要有依据地关闭旧项。
        m["threads"].append({"id": next_id(), "content": content, "createdAt": learned_at,
                             "expiresAt": expires, "kind": kind, "reason": reason})


def _person(name: str, uid: str) -> str:
    """名字可能重复、也会改；账号 是记忆里一直跟着这个人的身份。"""
    return f"{name or uid}（账号 {uid}）" if uid else (name or "他")


def _fact_version(f: dict) -> tuple:
    return f.get("content"), f.get("updatedAt"), f.get("retiredAt"), f.get("subject")


def _visible_history(history: list[dict]) -> list[dict]:
    """原话也遵守群里的可见性；私聊没有 read 字段，保持原来的原文范围。"""
    return [x for x in history if x.get("role") != "gap" and not x.get("skipped")
            and (x.get("role") != "user" or x.get("read") is not False)]


_QUOTE_EDGE = re.compile(r"^[「“\"『']+|[」”\"』']+$")


def _unquote(t: str) -> str:
    return js_trim(_QUOTE_EDGE.sub("", t))


def _topics_of(v) -> list[str]:
    raw = re.split(r"[、,，\s]+", v) if isinstance(v, str) else _arr(v)
    out = []
    for x in raw:
        t = _unquote(_str(x, 12))
        if t and t not in out:
            out.append(t)
    return out[:6]


def _line_text(x: dict) -> str:
    a = x.get("attachment")
    note = "" if not a else image_note(a) if a.get("kind") == "image" else f"［发了一个文件：{a.get('name') or '文件'}］"
    if x.get("sticker"):
        note += f"［发了一个表情：{x['sticker']}］"
    return f"{render_quote(x)}{note}{x['text']}".replace("\n", " ")


_PRONOUN = re.compile(r"^(他的|他|我的|我)")


def _strip_pronoun(t: str) -> str:
    return _PRONOUN.sub("", js_trim(t), count=1)


def _fp(t: str) -> str:
    """文字的指纹——带上模型名和算法，换了旧向量就全部作废（原版的 FNV-1a，按 UTF-16 算）"""
    t = f"{MODEL_ID}|{VECTOR_FORMAT}|{t}"
    h = 2166136261
    for unit in _utf16_units(t):
        h = ((h ^ unit) * 16777619) & 0xFFFFFFFF
    return _base36(h)


def _utf16_units(s: str):
    for ch in s:
        c = ord(ch)
        if c > 0xFFFF:
            c -= 0x10000
            yield 0xD800 + (c >> 10)
            yield 0xDC00 + (c & 0x3FF)
        else:
            yield c


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def _when_text(ts: int, now: int) -> str:
    d = (now - ts) / DAY
    if d < 0:
        return "稍后"
    if d < 0.75:
        return "不久前"
    if d < 2:
        days = (local_dt(now).date() - local_dt(ts).date()).days
        return "今天稍早" if days == 0 else "昨天" if days == 1 else "前天"
    if d < 7:
        return "前几天"
    if d < 30:
        return "前阵子"
    if d < 60:
        return "上个月"
    if d < 180:
        return "几个月前"
    return "很久以前"


def _memory_when(ts: int | None, now: int, end: int | None = None, *, label: str = "交谈时间") -> str:
    """提示里的记录时间，沿用本机时区；不是内容中提到的计划／事件发生时间。"""
    stamp = _integer(ts)
    if stamp is None or stamp <= 0:
        return f"（{label}不详）"
    try:
        start = local_dt(stamp)
        absolute = start.strftime("%Y-%m-%d %H:%M")
        finish = _integer(end)
        if finish is not None and finish > stamp:
            try:
                stop = local_dt(finish)
            except (ValueError, OverflowError, OSError):
                stop = None
            if stop is not None and stop.strftime("%Y-%m-%d %H:%M") != absolute:
                absolute += "～" + stop.strftime("%H:%M" if start.date() == stop.date() else "%Y-%m-%d %H:%M")
        return f"{_when_text(stamp, now)}（{label}：{absolute}）"
    except (ValueError, OverflowError, OSError):
        return f"（{label}不详）"


def _fact_time(f: dict, now: int) -> str:
    return _memory_when(f.get("updatedAt") or f.get("createdAt"), now, label="获知／更新时间")


def _episode_line(e: dict, now: int, haze: dict | None = None) -> str:
    """只格式化当前可记得的程度；不强化、降级或改写记忆。"""
    when = _memory_when(e.get("start"), now, e.get("end"))
    if e["level"] <= 0:
        return f"[清晰] {when}：{e['summary']}"
    if e["level"] == 1:
        return f"[大概] {when}：{e['summary']}"
    if haze:
        when = _memory_when(haze.get("start"), now, haze.get("end"), label="印象涵盖的交谈时间")
        return f"[很模糊] {when}：{haze['text']}"
    if e["level"] >= 3:
        return f"[很模糊] {when}：{e['gist']}"
    vague = f"{e['gist']}{'，那时候' + e['feeling'] if e['feeling'] else ''}"
    return f"[模糊] {when}：{vague}"


def thread_line(t: dict, now: int) -> str:
    return f"{t['content']}〔{_memory_when(t.get('createdAt'), now, label='记下时间')}〕"


def _gap_ago(ms: float) -> str:
    h = ms / HOUR
    if h < 24:
        return f"大约{math.floor(h + 0.5)}小时"
    d = ms / DAY
    if d < 2:
        return "一天多"
    if d < 14:
        return f"大约{math.floor(d + 0.5)}天"
    if d < 30:
        names = ["", "一", "两", "三", "四"]
        w = math.floor(d / 7)
        return f"{names[w] if w < len(names) else '几'}个多星期"
    mo = math.floor(d / 30)
    months = ["", "一", "两", "三", "四", "五", "六", "七", "八", "九", "十", "十一"]
    return f"{months[mo]}个多月" if mo < 12 else "很久"


def _whereabouts(frm: int, to: int) -> str:
    step = max(HOUR, (to - frm) / 336)
    away = total = 0
    t = frm + step / 2
    while t < to:
        total += 1
        if state_at(int(t)).location == "mission":
            away += 1
        t += step
    return "在外面出任务" if total > 0 and away / total >= 0.5 else "在基地"


def _day_key(ts: int) -> str:
    d = local_dt(ts)
    return f"{d.year}-{d.month - 1}-{d.day}"


def _month_key(ts: int) -> str:
    d = local_dt(ts)
    return f"{d.year}-{d.month:02d}"


def _floor_level(e: dict) -> int:
    return 0 if e["weight"] >= 5 else 1 if e["weight"] == 4 else 3


class Memory:
    """记忆本体（store.memory）和向量缓存（store.memory_vec）都在 store 里，改完由 store 存盘。"""

    def __init__(self, store, embedder: Embedder, logger, enabled=None, others=None):
        self.store = store
        self.embedder = embedder
        self.log = logger
        self._last_fade = 0
        self._refreshing = False
        # 这边新加的：这个会话整不整理记忆（关着：不整理，聊天原文只带最近 50 条）；
        # 别的会话的记忆：[(Memory, 来源的名字, 跟在后面的提醒)]，联想和回想时一起找、写明来源
        self.enabled = enabled or (lambda: True)
        self.others = others or (lambda: [])

    @property
    def m(self) -> dict:
        return self.store.memory

    def expire_low(self, now: int) -> int:
        """真正清掉满 30 天的低重要性事件；原话和独立人物事实不跟着误删。"""
        m = self.m
        expired = [e for e in m["episodes"] if episode_expired(e, now)]
        if not expired:
            return 0
        gone = {e["id"] for e in expired}
        # 旧版月度文本可能混有这些事件，不能让已删除内容藏在合并文本里继续回想。
        tainted_haze = {e["hazeId"] for e in expired if e.get("hazeId") is not None}
        m["episodes"] = [e for e in m["episodes"] if e["id"] not in gone]
        for e in m["episodes"]:
            if e.get("hazeId") in tainted_haze:
                e.pop("hazeId", None)
        linked_haze = {e["hazeId"] for e in m["episodes"] if e.get("hazeId") is not None}
        m["haze"] = [h for h in m["haze"] if h["id"] in linked_haze and h["id"] not in tainted_haze]
        for word in m["words"]:
            if word.get("episode") in gone:
                word["episode"] = None
        cache = self.store.memory_vec
        for key in list(cache):
            if key in {f"episode:{eid}" for eid in gone} or key.startswith("haze:"):
                del cache[key]
        self.store.save_memory()
        self.store.save_memory_vec()
        return len(gone)

    def _candidate_alive(self, c: dict, now: int | None = None) -> bool:
        now = max(now if now is not None else c.get("_asOf", 0), now_ms())
        owner = c.get("_mem") or self
        if c["kind"] == "episode":
            return any(e["id"] == c["id"] and not episode_expired(e, now)
                       and c.get("_version", _episode_version(e, owner.m)) == _episode_version(e, owner.m)
                       for e in owner.m["episodes"])
        if c["kind"] == "thread":
            return any(t["id"] == c["id"] and c.get("_version", _thread_version(t)) == _thread_version(t)
                       for t in open_threads(owner.m, now))
        if c["kind"] == "fact":
            return any(f["id"] == c["id"] and c.get("_version", _fact_version(f)) == _fact_version(f)
                       for f in active_facts(owner.m))
        return True

    # ---------- 该整理哪一段 ----------

    def unconsolidated(self, history: list[dict]) -> list[dict]:
        m = self.m
        start = 0
        if m["doneMsgId"]:
            i = next((k for k, x in enumerate(history) if x["id"] == m["doneMsgId"]), -1)
            start = i + 1 if i >= 0 else next((k for k, x in enumerate(history) if x["ts"] > m["doneTs"]), -1)
            if start < 0:
                return []
        out = []
        for x in history[start:]:
            if x.get("undelivered"):
                break
            if not x.get("system"):
                out.append(x)
        return out

    def pending_session(self, history: list[dict], online: bool, now: int) -> list[dict] | None:
        rest = self.unconsolidated(history)
        if not rest:
            return None
        end = 1
        while end < len(rest) and end < SESSION_MAX and rest[end]["ts"] - rest[end - 1]["ts"] < SESSION_GAP:
            end += 1
        session = rest[:end]
        is_last = end == len(rest)
        still_talking = online and now - session[-1]["ts"] < QUIET_CLOSE
        if is_last and end < SESSION_MAX and still_talking:
            return None
        return session

    def can_try(self, now: int) -> bool:
        return now - self.m["failedAt"] >= RETRY_AFTER

    # ---------- 整理 ----------

    async def consolidate(self, chat, session: list[dict], now: int, *,
                          trailblazer_impression: str = "", on_trailblazer_impression=None) -> str:
        """把一段对话收成记忆。⚠️ 失败了不前进——下次连同这段一起再试。
        chat 是 llm.chat_with_retry 的偏函数；同次整理可更新主要联系人印象，存好记忆后才回调(text, observed_at)。
        """
        self.expire_low(now)
        m = self.m
        last = session[-1]

        def advance(mm: dict) -> None:
            if last["ts"] >= mm["doneTs"]:
                mm["doneMsgId"] = last["id"]
                mm["doneTs"] = last["ts"]

        if not any(x["role"] == "user" for x in session):
            advance(m)
            self.store.save_memory()
            return "skipped"

        known = [f"{f['id']}. ［{f['category']}］{f['content']}〔{_fact_time(f, now)}〕" for f in active_facts(m)]
        open_ = [f"{t['id']}. {thread_line(t, now)}" for t in open_threads(m, last["ts"])]
        versions = {f["id"]: _fact_version(f) for f in active_facts(m)}
        open_before = {t["id"]: _thread_version(t) for t in open_threads(m, last["ts"])}
        dialogue = [f"#{i + 1} {message_time(x.get('ts'))} {'他' if x['role'] == 'user' else '我'}：{_line_text(x)}"
                    + no_reply_mark(x) for i, x in enumerate(session)]
        current_impression = _str(trailblazer_impression, 80)
        system = persona.build_consolidate_system(known, open_) + "\n" + "\n".join([
            "【你现在对主要联系人的印象】",
            current_impression or "还没有单独记下的印象。",
            "整理这段记忆时，也看看对他的印象有没有变化。只根据这段里他本人说的话、他和你的实际互动，"
            "不要把他转述的别人或别人的评价当成他本人，也不要推测。",
            "主要联系人的亲密度固定，不打分；印象确有变化时，在同一次「记下来」里用 trailblazer_impression 写现在的印象，"
            "80 字以内。没变化或没有依据就省略，不需要另做一次整理。",
        ])
        system += "\n\n" + time_block(now)
        messages = [{"role": "user", "content": f"【刚才的对话】\n" + "\n".join(dialogue)}]

        r = await chat(system, messages, tools=[SAVE_MEMORY], tool_choice="required")
        # required 是插件的交付要求；Provider 兼容与一次补交由 Llm 统一处理。
        call = next((c for c in r.calls if c["name"] == SAVE_MEMORY["name"]), None) if r.ok else None
        if not call:
            if not r.ok:
                kind = "缺少必需工具结果" if r.kind == "missing-tool" else r.kind
                self.log.warning(f"[MultiChat·记忆] 整理失败：{kind} {redact(r.detail or '')}")
            else:
                self.log.warning("[MultiChat·记忆] 她没有用「记下来」交结果")
            self.m["failedAt"] = now
            self.store.save_memory()
            return "failed"

        mm = self.m
        learned_at = last["ts"]

        def next_id() -> int:
            v = mm["seq"]
            mm["seq"] += 1
            return v

        inp = _obj(call["input"])
        n = len(session)
        eps = []
        for raw in _arr(inp.get("episodes"))[:3]:
            e = _obj(raw)
            frm, to = _int(e.get("from")), _int(e.get("to"))
            if math.isnan(frm):
                frm = 1
            if math.isnan(to):
                to = n
            frm = min(max(frm, 1), n)
            to = min(max(to, 1), n)
            if frm > to:
                frm, to = to, frm
            summary = _str(e.get("summary"), 240)
            if not summary:
                continue
            w = _int(e.get("weight"))
            eps.append({
                "id": next_id(), "fromMsg": session[frm - 1]["id"], "toMsg": session[to - 1]["id"],
                "start": session[frm - 1]["ts"], "end": session[to - 1]["ts"], "summary": summary,
                "gist": _str(e.get("gist"), 80) or summary[:40], "feeling": _str(e.get("feeling"), 24),
                "weight": 2 if math.isnan(w) else min(max(w, 1), 5), "level": 0, "recalls": 0, "lastRecalledAt": 0,
                "topics": _topics_of(e.get("topics")),
            })
        eps.sort(key=lambda e: e["start"])
        mm["episodes"].extend(eps)

        # ② 她说过的话——⭐ 必须在她这次的原话里一字不差地找得到
        mine = [(i, x) for i, x in enumerate(session) if x["role"] == "her"]
        kept = 0
        for raw in _arr(inp.get("my_words")):
            if kept >= WORDS_PER_RUN:
                break
            w = _obj(raw)
            text = _unquote(_str(w.get("text"), 120))
            if len(text) < 2:
                continue
            hit = next(((i, x) for i, x in mine if text in x["text"]), None)
            if not hit:
                self.log.info(f"[MultiChat·记忆] 原话对不上，丢掉：{text}")
                continue
            if any(o["text"] == text for o in mm["words"]):
                continue
            ep = None
            for e in eps:
                a = next((k for k, s in enumerate(session) if s["id"] == e["fromMsg"]), -1)
                b = next((k for k, s in enumerate(session) if s["id"] == e["toMsg"]), -1)
                if a <= hit[0] <= b:
                    ep = e
                    break
            mm["words"].append({"id": next_id(), "text": text, "about": _str(w.get("about"), 30), "ts": hit[1]["ts"],
                                "episode": ep["id"] if ep else None})
            kept += 1

        # ③ 关于他的事：有变化的先改，再加新的
        for raw in _arr(inp.get("fact_changes")):
            c = _obj(raw)
            cid = _int(c.get("id"))
            f = next((x for x in mm["facts"] if x["id"] == cid and not x.get("retiredAt")), None)
            content = _str(c.get("content"), 80)
            if not f or not content or versions.get(f["id"]) != _fact_version(f):
                continue
            if content.startswith("作废"):
                f["retiredAt"] = learned_at
            else:
                f["content"] = content
                f["updatedAt"] = learned_at
        added = 0
        for raw in _arr(inp.get("facts")):
            if added >= FACTS_PER_RUN:
                break
            f = _obj(raw)
            category = f.get("category") if f.get("category") in FACT_CATEGORIES else None
            content = _str(f.get("content"), 80)
            if not category or not content:
                continue
            if any(old[0] == content and ((current := next((x for x in mm["facts"] if x["id"] == fid), None)) is None
                   or _fact_version(current) != old) for fid, old in versions.items()):
                continue
            if any(x["content"] == content for x in active_facts(mm)):
                continue
            mm["facts"].append({"id": next_id(), "category": category, "content": content, "createdAt": learned_at,
                                "updatedAt": learned_at, "topics": _topics_of(f.get("topics"))})
            added += 1
        act = active_facts(mm)
        if len(act) > FACTS_MAX:
            def fresh(f: dict) -> int:
                return max(f["updatedAt"], f.get("usedAt") or 0)
            order = sorted(act, key=lambda f: (int(f["category"] in CORE), fresh(f)))
            for f in order[:len(act) - FACTS_MAX]:
                f["retiredAt"] = learned_at

        # ④ 悬着的事：先关后加，结构化筛选、模型选择期限，旧项不能被续期复活。
        _apply_threads(mm, inp, open_before, learned_at, next_id)

        advance(mm)
        mm["failedAt"] = 0
        self.expire_low(max(now, now_ms()))
        self.store.save_memory()
        impression = _str(inp.get("trailblazer_impression"), 80)
        if impression and impression != current_impression and on_trailblazer_impression:
            on_trailblazer_impression(impression, learned_at)
        self.log.info(f"[MultiChat·记忆] 整理了 {n} 条：{len(eps)} 段、{kept} 句她的话、{added} 件关于他的事")
        return "done"

    async def consolidate_peer(self, chat, session: list[dict], now: int, where: str, trailblazer: str,
                               about=None, default_person: tuple[str, str] | None = None,
                               summary_dialogue: str | None = None, allow_read_only: bool = False) -> list[dict] | None:
        """
        群聊、群友私聊的整理（这边新加的）。和原版 consolidate 一样的把关，多了：
        谁说的（facts 记在那个人名下）、和每个人亲近了还是疏远了（返回给调用方去改亲密度）。
        about(qq, name)：名单上写的「你们现在的关系、你对他的印象、上次的理由」（亲密度那边给）。
        default_person：群友私聊里没记是谁说的消息（旧记录），就是这个人 (账号 ID, 名字)。
        summary_dialogue：本批原文对应的每轮记事；仅替代展示，索引/原话校验仍以 session 为准。
        allow_read_only：群里只看没说也可记事、更新主要联系人印象，但不据此打分；私聊保持原来行为。
        返回 None = 失败（不前进）；返回 [] 或亲密度变化的列表 = 成功。
        """
        from .winext import SAVE_GROUP_MEMORY, group_consolidate_system

        self.expire_low(now)
        m = self.m
        last = session[-1]
        replied = any(x["role"] == "her" for x in session)
        if not any(x["role"] == "user" for x in session) or (not replied and not allow_read_only):
            # 只有她自己在说、或者她一句都没说（只是看了看）：不算一次对话
            m["doneMsgId"], m["doneTs"] = last["id"], last["ts"]
            self.store.save_memory()
            return []
        order: dict[str, str] = {}
        for x in session:
            if x["role"] != "user":
                continue
            uid = x.get("uid") or (default_person[0] if default_person else "")
            name = x.get("name") or (default_person[1] if default_person else "")
            if uid:
                order[uid] = name or order.get(uid, "") or uid
        people: list[tuple[str, str]] = list(order.items())
        names = dict(people)
        numbers = {uid: i + 1 for i, (uid, _) in enumerate(people)}
        roster = [f"{i + 1}. {_person(name, uid)}" + ("（主要联系人）" if uid == trailblazer else "")
                  + (f"：{about(uid, name)}" if about else "") for i, (uid, name) in enumerate(people)]
        trailblazer_present = bool(trailblazer) and any(
            x.get("role") == "user" and not x.get("system") and not x.get("skipped")
            and not x.get("undelivered") and x.get("read") is not False
            and (x.get("uid") or (default_person[0] if default_person else "")) == trailblazer
            for x in session)
        known = [f"{f['id']}. ［{f['category']}{'·' + _person(names.get(f.get('subject'), f.get('subjectName', '')), f['subject']) if f.get('subject') else ''}］{f['content']}〔{_fact_time(f, now)}〕"
                 for f in active_facts(m)]
        versions = {f["id"]: _fact_version(f) for f in active_facts(m)}
        open_before = {t["id"]: _thread_version(t) for t in open_threads(m, last["ts"])}
        open_ = [f"{t['id']}. {thread_line(t, now)}" for t in open_threads(m, last["ts"])]
        def speaker(x: dict) -> str:
            if x["role"] != "user":
                return "我"
            uid = x.get("uid") or (default_person[0] if default_person else "")
            name = x.get("name") or names.get(uid, "")
            return f"人物{numbers[uid]}·{_person(name, uid)}" if uid in numbers else (name or "他")

        dialogue = [f"#{i + 1} {message_time(x.get('ts'))} {speaker(x)}：{_line_text(x)}"
                    + no_reply_mark(x) for i, x in enumerate(session)]
        system = group_consolidate_system(where, known, open_) + "\n人物编号和名单一一对应；同名、改名时以 账号 区分。记忆里提到同名者，要保留账号 ID，不能合成一个人。"
        if allow_read_only and not replied:
            system += "\n这一批我只是看了，没有参与对话。可以记下看到的事实和事件，不给任何人打分。closeness 只能交本批主要联系人本人发言带来的印象变化，delta 写 0；没有这种变化就留空。"
        if summary_dialogue:
            system += "\n对话里的每轮记事是我当时留下的概述；#编号仍指原始消息，事件范围按标明的范围填写。记事不是逐字引语，不能当作我的原话存进 my_words。"
        system += "\n\n" + time_block(now, group=default_person is None)
        messages = [{"role": "user", "content": "【刚才的人】\n" + "\n".join(roster) + "\n\n【刚才的对话】\n" + (summary_dialogue or "\n".join(dialogue))}]
        r = await chat(system, messages, tools=[SAVE_GROUP_MEMORY], tool_choice="required")
        call = next((c for c in r.calls if c["name"] == "save_memory"), None) if r.ok else None
        m = self.m  # 模型回来后合并当前记录，不能写回等待期间已被替换的旧对象。
        if not call:
            reason = ("缺少必需工具结果" if r.kind == "missing-tool" else r.kind) if not r.ok else '没用「记下来」交结果'
            self.log.warning(f"[MultiChat·记忆] {where} 整理失败：{reason}")
            m["failedAt"] = now
            self.store.save_memory()
            return None

        inp, n, learned_at = _obj(call["input"]), len(session), last["ts"]

        def next_id() -> int:
            v = m["seq"]
            m["seq"] += 1
            return v

        def who(v) -> tuple[str, str] | None:
            # 人物编号不能四舍五入：0.6 / 1.4 等坏值不能落到另一个人的名下。
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None
            if isinstance(v, float) and (not math.isfinite(v) or not v.is_integer()):
                return None
            i = int(v)
            return people[i - 1] if 1 <= i <= len(people) else None

        eps = []
        for raw in _arr(inp.get("episodes"))[:3]:
            e = _obj(raw)
            frm, to = _int(e.get("from")), _int(e.get("to"))
            frm = 1 if math.isnan(frm) else min(max(frm, 1), n)
            to = n if math.isnan(to) else min(max(to, 1), n)
            if frm > to:
                frm, to = to, frm
            summary = _str(e.get("summary"), 240)
            if not summary:
                continue
            w = _int(e.get("weight"))
            eps.append({"id": next_id(), "fromMsg": session[frm - 1]["id"], "toMsg": session[to - 1]["id"],
                        "start": session[frm - 1]["ts"], "end": session[to - 1]["ts"], "summary": summary,
                        "gist": _str(e.get("gist"), 80) or summary[:40], "feeling": _str(e.get("feeling"), 24),
                        "weight": 2 if math.isnan(w) else min(max(w, 1), 5), "level": 0, "recalls": 0, "lastRecalledAt": 0,
                        "topics": _topics_of(e.get("topics"))})
        eps.sort(key=lambda e: e["start"])
        m["episodes"].extend(eps)
        mine = [(i, x) for i, x in enumerate(session) if x["role"] == "her"]
        kept = 0
        for raw in _arr(inp.get("my_words")):
            if kept >= WORDS_PER_RUN:
                break
            w = _obj(raw)
            text = _unquote(_str(w.get("text"), 120))
            hit = next(((i, x) for i, x in mine if len(text) >= 2 and text in x["text"]), None)
            if not hit or any(o["text"] == text for o in m["words"]):
                continue
            m["words"].append({"id": next_id(), "text": text, "about": _str(w.get("about"), 30), "ts": hit[1]["ts"], "episode": None})
            kept += 1
        for raw in _arr(inp.get("fact_changes")):
            c = _obj(raw)
            f = next((x for x in m["facts"] if x["id"] == _int(c.get("id")) and not x.get("retiredAt")), None)
            content = _str(c.get("content"), 80)
            if f and content and versions.get(f["id"]) == _fact_version(f):
                if content.startswith("作废"):
                    f["retiredAt"] = learned_at
                else:
                    f["content"], f["updatedAt"] = content, learned_at
        added = 0
        for raw in _arr(inp.get("facts")):
            if added >= FACTS_PER_RUN:
                break
            f = _obj(raw)
            p = who(f.get("who"))
            category = f.get("category") if f.get("category") in FACT_CATEGORIES else None
            content = _str(f.get("content"), 80)
            if not p or not category or not content or any(x["content"] == content and x.get("subject", "") == p[0]
                                                            for x in active_facts(m)):
                continue
            if any(old[0] == content and old[3] == p[0]
                   and ((current := next((x for x in m["facts"] if x["id"] == fid), None)) is None
                        or _fact_version(current) != old) for fid, old in versions.items()):
                continue
            m["facts"].append({"id": next_id(), "category": category, "content": content, "createdAt": learned_at,
                               "updatedAt": learned_at, "topics": _topics_of(f.get("topics")),
                               "subject": p[0], "subjectName": p[1]})
            added += 1
        act = active_facts(m)
        if len(act) > FACTS_MAX * 3:
            for f in sorted(act, key=lambda f: max(f["updatedAt"], f.get("usedAt") or 0))[:len(act) - FACTS_MAX * 3]:
                f["retiredAt"] = learned_at
        _apply_threads(m, inp, open_before, learned_at, next_id)
        closeness = []
        for raw in _arr(inp.get("closeness")):
            c = _obj(raw)
            p = who(c.get("who"))
            if not p or any(x["qq"] == p[0] for x in closeness):
                continue
            if p[0] == trailblazer:
                impression = _str(c.get("impression"), 80)
                if trailblazer_present and impression:
                    closeness.append({"qq": p[0], "name": p[1], "delta": 0, "reason": "",
                                      "impression": impression, "observedAt": learned_at})
            elif replied:
                closeness.append({"qq": p[0], "name": p[1], "delta": c.get("delta"), "reason": _str(c.get("reason"), 60),
                                  "impression": _str(c.get("impression"), 80), "observedAt": learned_at})
        if last["ts"] >= m["doneTs"]:
            m["doneMsgId"], m["doneTs"] = last["id"], last["ts"]
        m["failedAt"] = 0
        self.expire_low(max(now, now_ms()))
        self.store.save_memory()
        self.log.info(f"[MultiChat·记忆] {where} 整理了 {n} 条：{len(eps)} 段、{kept} 句她的话、{added} 件关于别人的事")
        return closeness

    def facts_about(self, qq: str, limit: int = 4) -> list[str]:
        """群聊、群友私聊里记下的、关于某个人的事（给「在场的人」那一块用）"""
        fs = sorted([f for f in active_facts(self.m) if f.get("subject") == qq], key=lambda f: -f["updatedAt"])
        now = now_ms()
        return [f"{f['content']}〔{_fact_time(f, now)}〕" for f in fs[:limit]]

    # ---------- 交给她：每次都带着的事、隔了一阵 ----------

    def facts_note(self) -> str | None:
        now = now_ms()
        return persona.build_facts_context([
            (f"[{_person(f.get('subjectName', ''), f['subject'])}] " if f.get("subject") else "")
            + f"{f['content']}〔{_fact_time(f, now)}〕"
            for f in core_facts(self.m)])

    def gap_note(self, history: list[dict], now: int) -> str | None:
        self.expire_low(now)
        real = [x for x in history if not x.get("system")]
        j = len(real) - 1
        if j < 1 or real[j]["role"] != "user":
            return None
        while j - 1 >= 0 and real[j - 1]["role"] == "user" and real[j]["ts"] - real[j - 1]["ts"] < GAP_MIN:
            j -= 1
        if j == 0:
            return None
        prev, first = real[j - 1], real[j]
        gap = first["ts"] - prev["ts"]
        if gap < GAP_MIN:
            return None
        m = self.m
        eps = sorted([e for e in m["episodes"] if e["end"] <= prev["ts"] + 1000 and prev["ts"] - e["end"] < SESSION_GAP],
                     key=lambda e: -e["end"])
        ep = eps[0] if eps else None
        days_talked = None
        if gap >= GAP_RHYTHM:
            since = now - 14 * DAY
            days_talked = len({_day_key(x["ts"]) for x in real[:j] if x["role"] == "user" and x["ts"] >= since})
        return persona.build_gap_context(
            ago=_gap_ago(gap), last_gist=f"{_memory_when(ep['start'], now, ep.get('end'))}：{ep['gist']}" if ep else None,
            last_feeling=ep["feeling"] if ep else None,
            whereabouts=_whereabouts(prev["ts"], first["ts"]), days_talked=days_talked,
            threads=[thread_line(t, now) for t in open_threads(m, now)],
        )

    # ---------- 带给她的原文、自动联想、回想 ----------

    def working_set(self, history: list[dict], *, private: bool = False, keep_ids=()) -> list[dict]:
        """默认仍给最近 50 条加未整理原文；私聊的已整理原文退出请求。
        关闭整理时最多带最近 50 条；私聊明确在回的旧目标可在游标前单独保留。"""
        m = self.m
        if private:
            done_idx = -1
            if m["doneMsgId"] or m["doneTs"]:
                done_idx = next((k for k, x in enumerate(history) if x["id"] == m["doneMsgId"]), -1)
                if done_idx < 0:
                    done_idx = next((k for k in range(len(history) - 1, -1, -1)
                                     if history[k]["ts"] <= m["doneTs"]), -1)
            cut = done_idx + 1
            if not self.enabled():
                count = 0
                for k in range(len(history) - 1, cut - 1, -1):
                    if not history[k].get("system"):
                        count += 1
                    if count >= WORKING:
                        cut = k
                        break
            keep = set(keep_ids)
            # 留言也是游标之后的原文，不能用遇到 undelivered 就停止的 unconsolidated。
            # keep_ids 只救游标之前的本轮目标，不能绕过无游标时的 50 条限制。
            return [x for k, x in enumerate(history) if k >= cut or
                    (k <= done_idx and x["id"] in keep)]
        if not self.enabled():
            count, cut = 0, len(history)
            for k in range(len(history) - 1, -1, -1):
                if not history[k].get("system"):
                    count += 1
                cut = k
                if count >= WORKING:
                    break
            return history[cut:]
        if not m["doneMsgId"] and not m["doneTs"]:
            return history
        done_idx = next((k for k, x in enumerate(history) if x["id"] == m["doneMsgId"]), -1)
        if done_idx < 0:
            done_idx = -1
            for k, x in enumerate(history):
                if x["ts"] <= m["doneTs"]:
                    done_idx = k
        count = 0
        cut = len(history)
        for k in range(len(history) - 1, -1, -1):
            if not history[k].get("system"):
                count += 1
            cut = k
            if count >= WORKING:
                break
        return history[min(cut, done_idx + 1):]

    def recent_private_context(self, now: int) -> tuple[str | None, set[int]]:
        """最近写入的本私聊事件，纯读取；不是一次回想，不改遗忘和使用次数。"""
        m = self.m
        expired = [e for e in m["episodes"] if _integer(e.get("end")) is not None and episode_expired(e, now)]
        expired_ids = {e["id"] for e in expired}
        # 尚未跑清理时也不能借月度印象重新带回已经到期的琐事。
        tainted_haze = {e["hazeId"] for e in expired if e.get("hazeId") is not None}
        available = [e for e in m["episodes"] if e["id"] not in expired_ids
                     and _integer(e.get("id")) is not None and (e.get("summary") or e.get("gist"))]
        picked = sorted(available, key=lambda e: _integer(e.get("id")), reverse=True)[:3]
        if not picked:
            return None, set()
        ids = {e["id"] for e in picked}
        lines, shown_haze = [], set()
        for e in sorted(picked, key=lambda e: (_integer(e.get("start")) or 0,
                                               _integer(e.get("end")) or 0, _integer(e.get("id")))):
            haze = next((h for h in m["haze"] if h["id"] == e.get("hazeId")), None) \
                if e["level"] >= 3 and e.get("hazeId") and e.get("hazeId") not in tainted_haze else None
            if haze:
                if haze["id"] in shown_haze:
                    continue
                shown_haze.add(haze["id"])
            lines.append("· " + _episode_line(e, now, haze))
        return ("【本私聊最近留下的记忆】\n"
                "以下是以前留下的背景，不是本轮新消息，也不是发言风格范例。"
                "交谈时间是当时聊到这些内容的时间，不等于其中约定或事件的发生时间。\n"
                + "\n".join(lines), ids)

    def _candidates(self, now: int, with_threads: bool) -> list[dict]:
        """只查已整理的记忆；不再按事件范围翻聊天档案。words 是整理时核验、留下的本人原句。"""
        self.expire_low(now)
        m = self.m
        out = []
        for e in m["episodes"]:
            haze = next((h for h in m["haze"] if h["id"] == e.get("hazeId")), None) if e["level"] >= 3 and e.get("hazeId") else None
            line = _episode_line(e, now, haze)
            tags = "、".join(e["topics"]) if e.get("topics") else ""
            text = (f"{e['summary']} {e['gist']} {e['feeling']}" if e["level"] <= 1 else
                    f"{e['gist']} {e['feeling']}" if e["level"] == 2 else
                    f"{e['gist']}{' ' + haze['text'] if haze else ''}") + f" {tags}"
            sem = (e["summary"] if e["level"] <= 1 else f"{e['gist']}{'，' + e['feeling'] if e['feeling'] else ''}") \
                + (f"（{tags}）" if tags else "")
            if e["level"] >= 3:
                sem = e["gist"] + (f"（{tags}）" if tags else "")
            out.append({"kind": "episode", "id": e["id"], "text": text, "sem": sem, "line": line, "ts": e["end"],
                        "_version": _episode_version(e, m)})
        for w in m["words"]:
            out.append({"kind": "words", "id": w["id"], "text": f"{w['text']} {w['about']}", "sem": f"{w['text']}（{w['about']}）",
                        "line": f"[你说过的原话] {_memory_when(w.get('ts'), now, label='原话时间')}：「{w['text']}」{'（那次在聊' + w['about'] + '）' if w['about'] else ''}", "ts": w["ts"]})
        for f in active_facts(m):
            tags = "、".join(f["topics"]) if f.get("topics") else ""
            content = f"[{_person(f.get('subjectName', ''), f['subject'])}] {f['content']}" if f.get("subject") else f["content"]
            out.append({"kind": "fact", "id": f["id"], "text": f"{content} {tags}",
                        "sem": content + (f"（{tags}）" if tags else ""), "line": f"{content}〔{_fact_time(f, now)}〕", "ts": f["updatedAt"],
                        "_version": _fact_version(f)})
        if with_threads:
            for t in open_threads(m, now):
                out.append({"kind": "thread", "id": t["id"], "text": t["content"], "sem": t["content"],
                            "line": f"[还悬着] {thread_line(t, now)}", "ts": t["createdAt"], "_version": _thread_version(t)})
        for candidate in out:
            candidate["_asOf"] = now
        return out

    @staticmethod
    def _score(query: str, cands: list[dict], uni: float = 0.3) -> list[float]:
        q = tokens(query, uni)
        docs = [tokens(c["text"]) for c in cands]
        n = len(docs)
        unit = math.log(1 + n)
        df: dict[str, int] = {}
        for d in docs:
            for g in d:
                df[g] = df.get(g, 0) + 1
        out = []
        for i in range(n):
            s = 0.0
            for g, w in q.items():
                if g not in docs[i]:
                    continue
                rarity = math.log(1 + n / df.get(g, 1)) / unit if unit > 0 else 1
                s += w * (0.4 + 0.6 * rarity)
            out.append(s)
        return out

    async def _semantic(self, query: str, cands: list[dict], allow_slow: bool = False) -> list[float] | None:
        """每条候选和这句话「意思上」有多近。模型用不了时返回 None。缺的向量现算现存。"""
        if not self.embedder.ready:
            return None
        cache = self.store.memory_vec

        def key_of(c: dict) -> str:
            return f"{c['kind']}:{c['id']}"

        missing = [c for c in cands if (cache.get(key_of(c)) or [None])[0] != _fp(c["sem"])]
        if len(missing) > MAX_INLINE_EMBED and not allow_slow:
            self.log.info(f"[MultiChat·向量] 还有 {len(missing)} 条记忆没算，这一轮先按字面，后台去补")
            asyncio.get_running_loop().create_task(self.refresh_vectors())
            return None
        if missing:
            vs = await self.embedder.embed([_strip_pronoun(c["sem"]) for c in missing])
            if vs is None:
                return None
            cache = self.store.memory_vec
            for c, v in zip(missing, vs):
                if self._candidate_alive(c):
                    cache[key_of(c)] = [_fp(c["sem"]), pack_vec(v)]
        q = await self.embedder.embed([_strip_pronoun(query)])
        if not q:
            return None
        cache = self.store.memory_vec
        alive = {key_of(c) for c in cands if self._candidate_alive(c)}
        stamp = max([now_ms(), *(c.get("_asOf", 0) for c in cands)])
        current = {key_of(c): _fp(c["sem"]) for c in self._candidates(stamp, True)}
        for k in list(cache):
            if k not in current or (cache.get(k) or [None])[0] != current[k]:
                del cache[k]
        self.store.save_memory_vec()
        return [float(np.dot(q[0], unpack_vec(cache[key_of(c)][1])))
                if key_of(c) in alive and key_of(c) in cache else 0.0 for c in cands]

    async def refresh_vectors(self) -> None:
        """把还没算的向量补上（启动时、整理完各一次），第一次联想就不用等"""
        if self._refreshing or not self.embedder.ready:
            return
        self._refreshing = True
        try:
            from .jsutil import now_ms
            await self._semantic("预热", self._candidates(now_ms(), True), allow_slow=True)
        finally:
            self._refreshing = False

    def _mark_recalled(self, picked: list[dict], now: int) -> None:
        if not picked:
            return
        self.expire_low(now)
        m = self.m
        for c in picked:
            if c["kind"] == "episode":
                e = next((x for x in m["episodes"] if x["id"] == c["id"]), None)
                if e and now - e["lastRecalledAt"] >= RECALL_COUNT_GAP:
                    e["recalls"] += 1
                    e["lastRecalledAt"] = now
                    if e["level"] > 0:
                        e["level"] -= 1
                    e["levelSince"] = now
                    if e["level"] < 3:
                        e.pop("hazeId", None)
            elif c["kind"] == "fact":
                f = next((x for x in m["facts"] if x["id"] == c["id"]), None)
                if f and not f.get("retiredAt") and c.get("_version", _fact_version(f)) == _fact_version(f):
                    f["usedAt"] = max(now, f.get("usedAt") or 0)
        self.store.save_memory()

    async def associate(self, query: str, history: list[dict], now: int, *, private: bool = False) -> str | None:
        """自动联想：够不上门槛就不想起；最多 3 条；大约一成的时候混进一条不太相关的。"""
        text, commit = await self.associate_deferred(query, history, now, private=private)
        commit()
        return text

    async def associate_deferred(self, query: str, history: list[dict], now: int, *, private: bool = False):
        """（这边新加的）先想、先不记「想起过」：她真开口了再调 commit()。
        群里判断要不要接话时用——没接，就当没想起过。返回 (交给她的那一块, commit)"""
        if not query.strip():
            return None, lambda: None
        m = self.m
        ws = self.working_set(history, private=private)
        window_start = next((x["ts"] for x in ws if not x.get("system")), math.inf)
        core = {f["id"] for f in core_facts(m)}
        shown = self.recent_private_context(now)[1] if private else set()

        def eligible(c: dict) -> bool:
            if c["kind"] == "episode" and c["id"] in shown:
                return False
            return c["id"] not in core if c["kind"] == "fact" else c["ts"] < window_start

        own = self._candidates(now, False)
        groups = [(self, own), *self._other_candidates(now, False)]
        cands = [c for _, cs in groups for c in cs]
        lex = self._score(query, cands)
        sem = None
        for q in split_clauses(query):
            s = await self._semantic_all(q, groups)
            if s is None:
                sem = None
                break
            sem = s if sem is None else [max(a, b) for a, b in zip(sem, s)]
        ranked = []
        for i, c in enumerate(cands):
            if not self._candidate_alive(c, now):
                continue
            sc = max(lex[i] / ASSOC_MIN, (sem[i] - SEM_ASSOC_FLOOR) / (SEM_ASSOC - SEM_ASSOC_FLOOR) if sem else 0)
            if c.get("_mem") is not None or eligible(c):
                ranked.append({"c": c, "score": sc})
        ranked.sort(key=lambda x: -x["score"])
        picked = [x for x in ranked if x["score"] >= 1][:ASSOC_MAX]
        if any(x["c"]["kind"] == "episode" for x in picked):
            picked = [x for x in picked if x["c"]["kind"] != "fact"]
        if len(picked) < ASSOC_MAX and random.random() < NOISE_CHANCE:
            band = [x for x in ranked if 0.4 <= x["score"] < 1]
            if band:
                picked.append({**band[math.floor(random.random() * len(band))], "noise": True})
        if not picked:
            return None, lambda: None
        self.log.info("[MultiChat·联想] " + "、".join(
            f"{'（突然想起）' if x.get('noise') else ''}{x['c']['line'][:30]} {x['score']:.2f}" for x in picked))
        return (persona.build_recollection_context(list(dict.fromkeys(x["c"]["line"] for x in picked))),
                self._commit_once([x["c"] for x in picked], now))

    # ---------- 别的会话的记忆（这边新加的） ----------

    def _other_candidates(self, now: int, with_threads: bool) -> list[tuple["Memory", list[dict]]]:
        out = []
        for mem, label, note in self.others():
            cs = []
            for c in mem._candidates(now, with_threads):
                c = dict(c)
                c["line"] = f"[{label}] {c['line']}{note or ''}"
                c["_mem"] = mem
                cs.append(c)
            if cs:
                out.append((mem, cs))
        return out

    async def _semantic_all(self, query: str, groups) -> list[float] | None:
        out: list[float] = []
        for mem, cs in groups:
            s = await mem._semantic(query, cs)
            if s is None:
                return None
            out += s
        return out

    def _mark_all(self, picked: list[dict], now: int) -> None:
        by_mem: dict[int, tuple] = {}
        for c in picked:
            mem = c.get("_mem") or self
            by_mem.setdefault(id(mem), (mem, []))[1].append(c)
        for mem, cs in by_mem.values():
            mem._mark_recalled(cs, now)

    def _commit_once(self, picked: list[dict], now: int):
        done = False

        def commit() -> None:
            nonlocal done
            if not done:
                done = True
                self._mark_all(picked, max(now, now_ms()))

        return commit

    async def recall(self, about: str, history: list[dict], now: int) -> str:
        """回想已整理的概述、事实、约定和本人原句；不再翻聊天档案，history 仅保留调用兼容。"""
        text, commit = await self.recall_deferred(about, history, now)
        commit()
        return text

    async def recall_deferred(self, about: str, history: list[dict], now: int):
        """群里先取回记忆，确实开口后才提交使用；主要联系人仍走即时 recall。"""
        meaningful = [ch for ch in strip_ps(about) if ch not in FUNCTION_CHARS]
        uni = 0.5 if len(meaningful) <= 1 else 0.25
        own = self._candidates(now, True)
        groups = [(self, own), *self._other_candidates(now, True)]
        cands = [c for _, cs in groups for c in cs]
        sem = await self._semantic_all(about, groups)
        lex = self._score(about, cands, uni)
        all_ = sorted(({"c": c, "score": max(lex[i] / RECALL_MIN,
                                              (sem[i] - SEM_RECALL_FLOOR) / (SEM_RECALL - SEM_RECALL_FLOOR) if sem else 0)}
                       for i, c in enumerate(cands) if self._candidate_alive(c, now)), key=lambda x: -x["score"])
        cutoff = max(1, (all_[0]["score"] if all_ else 0) * 0.4)
        ranked = [x for x in all_ if x["score"] >= cutoff][:RECALL_MAX]
        self.log.info(f"[MultiChat·回想] {about} → " + ("、".join(f"{x['c']['line'][:30]} {x['score']:.2f}" for x in ranked)
                                                 if ranked else "想不起来"))
        if not ranked:
            return "想不起来。", lambda: None
        return (persona.build_recollection_context(list(dict.fromkeys(x["c"]["line"] for x in ranked))),
                self._commit_once([x["c"] for x in ranked], now))

    # ---------- 变模糊、并成底噪 ----------

    def fade(self, now: int) -> int:
        """低重要性逐级变模糊并在 30 天清掉；删除不受十分钟降级节流影响。"""
        expired = self.expire_low(now)
        if now - self._last_fade < 10 * MIN:
            return expired
        self._last_fade = now
        changed = 0
        for e in self.m["episodes"]:
            since = e.get("levelSince") or e["end"]
            level = e["level"]
            days = LOW_LEVEL_DAYS if e["weight"] <= 3 else LEVEL_DAYS
            if e["weight"] <= 3 and not e.get("lastRecalledAt") and not e.get("recalls"):
                # 旧版 levelSince 可能从第 7 天起步；未被强化过的旧记录按新累计边界重算。
                since, level = e["end"], 0
            while level < _floor_level(e) and now - since >= days[level] * DAY:
                since += days[level] * DAY
                level += 1
            level = min(level, _floor_level(e))
            if level != e["level"]:
                e["level"] = level
                e["levelSince"] = since
                changed += 1
        if changed:
            self.store.save_memory()
            self.log.info(f"[MultiChat·记忆] {changed} 段变模糊了")
        return changed + expired

    def pending_haze(self) -> dict | None:
        # 低重要性事件十四天就很模糊，但不得被月度文本永久留下；三十天直接删除。
        loose = sorted([e for e in self.m["episodes"] if e["weight"] > 3 and e["level"] >= 3
                        and not e.get("hazeId")], key=lambda e: e["start"])
        if not loose:
            return None
        month = _month_key(loose[0]["start"])
        return {"month": month, "episodes": [e for e in loose if _month_key(e["start"]) == month]}

    def can_try_haze(self, now: int) -> bool:
        return now - self.m["hazeFailedAt"] >= RETRY_AFTER

    async def merge_haze(self, chat, now: int, *, identity: str | None = None) -> bool:
        p = self.pending_haze()
        if not p:
            return False
        m = self.m
        existing = next((h for h in m["haze"] if h["month"] == p["month"]), None)
        previous = dict(existing) if existing else None
        episode_versions = {e["id"]: (e["gist"], e["feeling"], e["start"], e["end"]) for e in p["episodes"]}
        items = [f"· {e['gist']}{'（' + e['feeling'] + '）' if e['feeling'] else ''}" for e in p["episodes"]]
        content = "\n".join(["【那阵子聊过的事】", *items, *(["", "【你原来对那阵子的印象】", existing["text"]] if existing else [])])
        system = persona.build_haze_system(bool(existing))
        if identity:
            system = "\n".join([
                profile.prompt("identity") + "\n" + profile.prompt("voice") + "\n" + identity,
                "下面是你在这段关系里很久以前聊过的一些事，细节你已经记不清了。",
                "用一两句话写下那阵子留给你的印象：常聊什么、各人那时候大概是什么状态。",
                "不要写具体的细节和日子，就像过了很久再回想时那样。用「我」指自己，别人保留名字，不能把不同的人混在一起。",
                *(["把原来的印象和这些揉在一起，还是一两句话。"] if existing else []),
                "只写这一两句话，别的什么都不要写。",
            ])
        r = await chat(system, [{"role": "user", "content": content}])
        m = self.m
        current = next((h for h in m["haze"] if h["month"] == p["month"]), None)
        ids = {e["id"] for e in p["episodes"]}
        live = [e for e in m["episodes"] if e["id"] in ids and e["level"] >= 3 and not e.get("hazeId")]
        # 旧印象改过、记忆被忘掉或刚被想起：丢掉旧快照的答案，下次再按当前记录整理。
        if current != previous or len(live) != len(p["episodes"]) or any(
                episode_versions[e["id"]] != (e["gist"], e["feeling"], e["start"], e["end"]) for e in live):
            return False
        text = ""
        if r.ok:
            text = "".join(js_trim(l) for l in re.split(r"\r?\n", r.text) if js_trim(l))
            text = re.sub(r"^[「“\"]+|[」”\"]+$", "", text)[:100]
        if not text:
            if not r.ok:
                self.log.warning(f"[MultiChat·底噪] {r.kind} {redact(r.detail or '')}")
            m["hazeFailedAt"] = now
            self.store.save_memory()
            return False
        start = min([*[e["start"] for e in live], current["start"] if current else math.inf])
        end = max([*[e["end"] for e in live], current["end"] if current else 0])
        h = next((x for x in m["haze"] if x["month"] == p["month"]), None)
        if h:
            h.update(text=text, start=start, end=end)
        else:
            h = {"id": m["seq"], "month": p["month"], "text": text, "start": start, "end": end}
            m["seq"] += 1
            m["haze"].append(h)
        for e in m["episodes"]:
            if e["id"] in ids and e["level"] >= 3:
                e["hazeId"] = h["id"]
        m["hazeFailedAt"] = 0
        self.store.save_memory()
        self.log.info(f"[MultiChat·底噪] {p['month']}：{text}")
        return True

    # ---------- 给旧记忆补话题 ----------

    def pending_tags(self) -> list[dict]:
        out = [{"kind": "episode", "id": e["id"], "text": f"［聊过的事］{e['summary']}"} for e in self.m["episodes"] if e.get("topics") is None]
        out += [{"kind": "fact", "id": f["id"],
                 "text": f"［关于{_person(f.get('subjectName', ''), f['subject']) if f.get('subject') else '他'}］{f['content']}"}
                for f in active_facts(self.m) if f.get("topics") is None]
        return out[:TAG_BATCH]

    def can_try_tags(self, now: int) -> bool:
        return now - (self.m.get("tagFailedAt") or 0) >= RETRY_AFTER

    async def tag_old(self, chat, now: int) -> bool:
        batch = self.pending_tags()
        if not batch:
            return False
        content = "\n".join(f"{b['id']}. {b['text']}" for b in batch)
        system = persona.build_tag_system()
        msgs = [{"role": "user", "content": content}]
        r = await chat(system, msgs, tools=[TAG_MEMORIES], tool_choice="required")
        call = next((c for c in r.calls if c["name"] == TAG_MEMORIES["name"]), None) if r.ok else None
        m = self.m
        if not call:
            reason = ("缺少必需工具结果" if r.kind == "missing-tool" else r.kind) if not r.ok else '没用工具交结果'
            self.log.warning(f"[MultiChat·话题] 补话题失败：{reason}")
            m["tagFailedAt"] = now
            self.store.save_memory()
            return False
        got = {}
        for raw in _arr(_obj(call["input"]).get("items")):
            it = _obj(raw)
            got[_int(it.get("id"))] = _topics_of(it.get("topics"))
        still_pending = {(b["kind"], b["id"]): b["text"] for b in self.pending_tags()}
        for b in batch:
            target = next((x for x in (m["episodes"] if b["kind"] == "episode" else m["facts"]) if x["id"] == b["id"]), None)
            if target is not None and still_pending.get((b["kind"], b["id"])) == b["text"]:
                target["topics"] = got.get(b["id"], [])
        m["tagFailedAt"] = 0
        self.store.save_memory()
        self.log.info(f"[MultiChat·话题] 补了 {len(batch)} 件旧记忆")
        return True
