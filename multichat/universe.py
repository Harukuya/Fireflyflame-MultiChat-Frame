"""Read-only projections for the memory-universe page.

Only already loaded records are copied. Nothing here starts a conversation,
reads a file, calls a model, strengthens recall or performs expiry mutations.
The host owns authentication and returns these business envelopes as JSON.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from .jsutil import now_ms
from .memory import LEVEL_NAME, LOW_MEMORY_TTL, THREADS_MAX, THREAD_TTL, _episode_line, thread_expires_at

SCHEMA_VERSION = 1
KINDS = ("episode", "pack", "fact", "word", "thread", "haze")
LIMITS = {"defaultPageSize": 100, "maxPageSize": 200, "maxQueryChars": 80}
_MEMORY_KEYS = ("episodes", "facts", "words", "threads", "haze")
_PACK_KEYS = ("id", "state", "start", "end", "createdAt", "summary", "people", "topics", "anchors", "unresolved", "cold")
_PARAMS = {"scope", "kinds", "q", "topicId", "personId", "from", "to", "includeUndated", "limit", "cursor"}
_MAX_TIME = 253402214400000  # Keep conversions within datetime's supported range.


class _QueryError(ValueError):
    def __init__(self, code="INVALID_QUERY", message="筛选参数不合法。", retryable=False):
        self.code, self.message, self.retryable = code, message, retryable


def _error(exc):
    return {"ok": False, "schemaVersion": SCHEMA_VERSION,
            "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}}


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value
    if not math.isfinite(value):
        return None
    return int(value) if int(value) == value else None


def _stamp(value):
    n = _integer(value)
    return n if n is not None and 0 < n < _MAX_TIME else None


def _raw_id(value):
    if isinstance(value, str) and value:
        return value
    n = _integer(value)
    return str(n) if n is not None and n >= 0 else None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _id(*parts):
    return "mu1_" + _digest(parts)


def _norm(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _topics(raw):
    out, seen = [], set()
    for value in raw if isinstance(raw, list) else []:
        label = _text(value)
        key = _norm(label)
        if key and key not in seen:
            out.append({"id": _id("topic", key), "label": label})
            seen.add(key)
    return out


def _person(namespace, account, label, basis):
    account = _raw_id(account)
    if account is None:
        return None
    return {"id": _id("person", namespace, account), "accountId": account,
            "label": _text(label) or account, "basis": basis}


def _rows(value):
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _capture(p):
    """No await/getter with effects; this is one event-loop read transaction."""
    memories = getattr(p, "memories", None)
    if not isinstance(memories, Mapping) or getattr(p, "_stopped", False) or getattr(p, "_closed", False):
        raise _QueryError("NOT_READY", "插件的记忆尚未准备好。", True)
    sessions = getattr(p, "sessions", None)
    if sessions is None:
        raise _QueryError("NOT_READY", "插件的会话尚未准备好。", True)
    books = {"p": getattr(sessions, "private", {}), "g": getattr(sessions, "group", {})}
    chats = getattr(getattr(p, "hub", None), "chats", {})
    chats = chats if isinstance(chats, Mapping) else {}
    namespace = _text(getattr(p, "universe_account_namespace", "")) or "account"
    captured = []
    for scope, mem in list(memories.items()):
        if not isinstance(scope, str) or not re.fullmatch(r"[pg]:.+", scope):
            continue
        prefix, sid = scope.split(":", 1)
        book = books[prefix] if isinstance(books[prefix], Mapping) else {}
        entry = book.get(sid, {})
        entry = entry if isinstance(entry, Mapping) else {}
        name = _text(entry.get("name")) or sid
        source = {"scope": scope, "kind": "private" if prefix == "p" else "group",
                  "label": ("私聊·" if prefix == "p" else "群·") + name,
                  "enabled": bool(entry.get("enabled", False) and entry.get("listed", True)),
                  "memoryEnabled": bool(entry.get("memory", False)), "pendingCount": None}
        # Memory.m is a property; use the loaded storage directly and never mem.enabled().
        stored = getattr(getattr(mem, "store", None), "memory", {})
        stored = stored if isinstance(stored, Mapping) else {}
        memory = copy.deepcopy({key: stored.get(key, []) for key in _MEMORY_KEYS})
        records = getattr(getattr(chats.get(sid), "round_memory", None), "_records", []) if prefix == "g" else []
        packs = copy.deepcopy([{key: r.get(key) for key in _PACK_KEYS} for r in _rows(records)])
        captured.append((source, memory, packs))
    return captured, namespace


def _time(basis, start, end=None, label=""):
    start, end = _stamp(start), _stamp(end)
    if start is None:
        return {"basis": "unknown", "start": None, "end": None, "precision": "unknown", "label": label or "时间不详"}
    if end is not None and end < start:
        end = None
    return {"basis": basis, "start": start, "end": end, "precision": "minute", "label": label}


def _haze_time(haze, offset):
    time = _time("coverage", haze.get("start"), haze.get("end"), "印象涵盖时间")
    if time["basis"] != "unknown":
        return time
    month = _text(haze.get("month"))
    match = re.fullmatch(r"(\d{4})-(\d{2})", month)
    if match:
        try:
            y, m = map(int, match.groups())
            tz = timezone(timedelta(minutes=offset))
            start = datetime(y, m, 1, tzinfo=tz)
            end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=tz)
            return {"basis": "coverage", "start": int(start.timestamp() * 1000), "end": int(end.timestamp() * 1000),
                    "precision": "month", "label": "印象月份"}
        except (ValueError, OverflowError, OSError):
            pass
    return time


def _attributes():
    return {"category": None, "about": None, "threadKind": None, "reason": None,
            "createdAt": None, "updatedAt": None, "anchors": [], "unresolved": []}


def _strings(value):
    return [_text(x) for x in value if _text(x)] if isinstance(value, list) else []


def _build(captured, namespace, now, offset):
    details = {}
    for source, memory, packs in captured:
        scope = source["scope"]
        episodes = _rows(memory.get("episodes"))
        expired_ids, tainted_haze = set(), set()
        for e in episodes:
            end, weight = _stamp(e.get("end")), _integer(e.get("weight"))
            if end is not None and (weight if weight is not None else 2) <= 3 and now >= end + LOW_MEMORY_TTL:
                expired_ids.add(_raw_id(e.get("id")))
                if _raw_id(e.get("hazeId")) is not None:
                    tainted_haze.add(_raw_id(e["hazeId"]))
        alive = [e for e in episodes if _raw_id(e.get("id")) not in expired_ids]
        linked_haze = {_raw_id(e.get("hazeId")) for e in alive} - tainted_haze
        hazes = {_raw_id(h.get("id")): h for h in _rows(memory.get("haze"))
                 if _raw_id(h.get("id")) is not None and _raw_id(h.get("id")) not in tainted_haze
                 and (not expired_ids or _raw_id(h.get("id")) in linked_haze)}
        links = []

        def add(kind, raw, body, time, *, remembered=None, people=None, importance=None, clarity=None, expires=None):
            rid = _raw_id(raw.get("id"))
            if rid is None:
                return None
            node_id = _id(scope, kind, rid)
            if node_id in details:
                return None  # Corrupt duplicate IDs never produce duplicate UI keys.
            title = " ".join(body.split())[:40] or "未命名记忆"
            node = {"id": node_id, "kind": kind, "source": source, "title": title, "titleBasis": "excerpt",
                    "preview": body[:120], "time": time, "topics": _topics(raw.get("topics")), "people": people or [],
                    "importance": importance, "clarity": clarity, "expiresAt": expires,
                    "status": "cold" if kind == "pack" and raw.get("cold") else "active"}
            detail = {"node": node, "rememberedText": body if remembered is None else remembered,
                      "original": None, "recall": None, "attributes": _attributes(), "links": []}
            # Search only the currently remembered content, never original or arbitrary stored fields.
            details[node_id] = (detail, body)
            return detail

        for e in alive:
            level = _integer(e.get("level"))
            level = min(3, max(0, level if level is not None else 0))
            weight = _integer(e.get("weight"))
            weight = min(5, max(1, weight if weight is not None else 2))
            end = _stamp(e.get("end"))
            clean = {"summary": _text(e.get("summary")), "gist": _text(e.get("gist")),
                     "feeling": _text(e.get("feeling")), "level": level,
                     "start": _stamp(e.get("start")), "end": end}
            haze = hazes.get(_raw_id(e.get("hazeId"))) if level >= 3 else None
            if haze:
                haze = {**haze, "text": _text(haze.get("text"))}
            body = (clean["summary"] if level <= 1 else haze["text"] if haze else clean["gist"] if level >= 3
                    else clean["gist"] + ("，那时候" + clean["feeling"] if clean["feeling"] else ""))
            d = add("episode", e, body, _time("conversation", clean["start"], end, "交谈时间"),
                    remembered=_episode_line(clean, now, haze), importance=weight,
                    clarity={"level": level, "label": LEVEL_NAME[level]},
                    expires=end + LOW_MEMORY_TTL if end is not None and weight <= 3 else None)
            if d:
                d["original"] = {"label": "原始整理记录", **{k: clean[k] for k in ("summary", "gist", "feeling")}}
                d["recall"] = {"count": max(0, _integer(e.get("recalls")) or 0), "lastCountedAt": _stamp(e.get("lastRecalledAt"))}
                if haze:
                    links.append((d, "faded_into", "haze", _raw_id(e.get("hazeId"))))
        for raw in _rows(memory.get("facts")):
            if raw.get("retiredAt"):
                continue
            person = _person(namespace, raw.get("subject"), raw.get("subjectName"), "fact_subject")
            d = add("fact", raw, _text(raw.get("content")), _time("updated", raw.get("updatedAt"), label="事实更新时间"),
                    people=[person] if person else [])
            if d:
                d["attributes"].update(category=_text(raw.get("category")) or None, createdAt=_stamp(raw.get("createdAt")),
                                       updatedAt=_stamp(raw.get("updatedAt")))
        for raw in _rows(memory.get("words")):
            d = add("word", raw, _text(raw.get("text")), _time("utterance", raw.get("ts"), label="发言时间"))
            if d:
                d["attributes"]["about"] = _text(raw.get("about")) or None
                if _raw_id(raw.get("episode")) not in expired_ids:
                    links.append((d, "word_of_episode", "episode", _raw_id(raw.get("episode"))))
        thread_count = 0
        for raw in _rows(memory.get("threads")):
            if raw.get("closedAt"):
                continue
            # Unknown creation dates cannot establish an unexpired, seven-day concern.
            if _stamp(raw.get("createdAt")) is None:
                continue
            expires = thread_expires_at(raw)
            if now >= expires or thread_count >= THREADS_MAX:
                continue
            d = add("thread", raw, _text(raw.get("content")), _time("recorded", raw.get("createdAt"), label="记下时间"), expires=expires)
            if d:
                thread_count += 1
                d["attributes"].update(threadKind=_text(raw.get("kind")) or None, reason=_text(raw.get("reason")) or None,
                                       createdAt=_stamp(raw.get("createdAt")))
        for raw in hazes.values():
            add("haze", raw, _text(raw.get("text")), _haze_time(raw, offset))
        for raw in packs:
            if raw.get("state") != "done" or not _text(raw.get("summary")):
                continue
            people = []
            if isinstance(raw.get("people"), dict):
                for uid, name in raw["people"].items():
                    person = _person(namespace, uid, name, "pack_participant")
                    if person:
                        people.append(person)
            d = add("pack", raw, _text(raw.get("summary")), _time("conversation", raw.get("start"), raw.get("end"), "交谈时间"), people=people)
            if d:
                d["attributes"].update(createdAt=_stamp(raw.get("createdAt")), anchors=_strings(raw.get("anchors")),
                                       unresolved=_strings(raw.get("unresolved")))
        for d, relation, kind, rid in links:
            target = details.get(_id(scope, kind, rid)) if rid is not None else None
            if target:
                n = target[0]["node"]
                d["links"].append({"type": relation, "target": {"id": n["id"], "kind": kind, "title": n["title"]}, "basis": "stored"})
    return details


def _param(params, name, limit=256):
    value = params.get(name)
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _QueryError()
    value = str(value)
    if len(value) > limit:
        raise _QueryError()
    return value


def _query(params, sources):
    if set(params) - _PARAMS:
        raise _QueryError()
    scope = _param(params, "scope")
    if scope and scope not in {s["scope"] for s in sources}:
        raise _QueryError(message="来源不在已加载的会话中。")
    raw_kinds = _param(params, "kinds")
    kinds = raw_kinds.split(",") if raw_kinds else list(KINDS)
    if not kinds or any(k not in KINDS for k in kinds):
        raise _QueryError()
    q = _param(params, "q", LIMITS["maxQueryChars"])
    topic, person = _param(params, "topicId", 160), _param(params, "personId", 160)
    bounds = []
    for name in ("from", "to"):
        raw = _param(params, name, 20)
        if raw and (not re.fullmatch(r"\d+", raw) or not 0 <= int(raw) < _MAX_TIME):
            raise _QueryError()
        bounds.append(int(raw) if raw else None)
    lower, upper = bounds
    if lower is not None and upper is not None and lower >= upper:
        raise _QueryError(message="起始时间必须早于结束时间。")
    undated = _param(params, "includeUndated", 5) or "false"
    if undated not in ("true", "false"):
        raise _QueryError()
    limit = _param(params, "limit", 3) or str(LIMITS["defaultPageSize"])
    if not re.fullmatch(r"\d+", limit) or not 1 <= int(limit) <= LIMITS["maxPageSize"]:
        raise _QueryError()
    return {"scope": scope, "kinds": sorted(set(kinds)), "q": _norm(q), "topicId": topic, "personId": person,
            "from": lower, "to": upper, "includeUndated": undated == "true", "limit": int(limit)}


def _matches(detail, body, query):
    n = detail["node"]
    if query["scope"] and n["source"]["scope"] != query["scope"] or n["kind"] not in query["kinds"]:
        return False
    if query["topicId"] and not any(x["id"] == query["topicId"] for x in n["topics"]):
        return False
    if query["personId"] and not any(x["id"] == query["personId"] for x in n["people"]):
        return False
    if query["q"] and query["q"] not in _norm(" ".join([body, *[t["label"] for t in n["topics"]]])):
        return False
    lower, upper = query["from"], query["to"]
    if lower is None and upper is None:
        return True
    t = n["time"]
    start, end = t["start"], t["end"]
    if start is None:
        return query["includeUndated"]
    # A month is a half-open coverage interval; message end times are included.
    stop = end if end is not None else start
    if lower is not None and (stop <= lower if t["precision"] == "month" else stop < lower):
        return False
    return upper is None or start < upper


def _cursor(raw, query_hash, revision, count):
    try:
        value = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True))
        if not isinstance(value, dict) or set(value) != {"v", "q", "r", "o"} or _integer(value["v"]) != 1:
            raise ValueError()
        if value["q"] != query_hash:
            raise _QueryError(message="翻页必须保持同一组筛选条件。")
        if value["r"] != revision:
            raise _QueryError("STALE_CURSOR", "记忆内容已更新，请重新读取第一页。", True)
        offset = _integer(value["o"])
        if offset is None or not 0 <= offset <= count:
            raise ValueError()
        return offset
    except _QueryError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise _QueryError(message="分页游标不合法。") from None


def handle(p, operation, params=None, *, now=None):
    """Return a schema-v1 envelope; hosts should not unwrap or mutate it."""
    try:
        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            raise _QueryError()
        if operation not in ("meta", "items", "item"):
            raise _QueryError(message="不支持的记忆查询。")
        if operation == "meta" and params or operation == "item" and set(params) != {"id"}:
            raise _QueryError()
        now = now_ms() if now is None else _stamp(now)
        if now is None:
            raise _QueryError()
        offset = int(datetime.fromtimestamp(now / 1000).astimezone().utcoffset().total_seconds() // 60)
        captured, namespace = _capture(p)
        sources = sorted((s for s, _, _ in captured), key=lambda s: s["scope"])
        if operation == "meta":
            data = {"sources": sources, "kinds": list(KINDS), "capabilities": {"readOnly": True, "rawChat": False,
                    "requestHistory": False, "mutations": False, "personImpression": False}, "limits": dict(LIMITS)}
        else:
            query = _query(params, sources) if operation == "items" else None
            records = _build(captured, namespace, now, offset)
            if operation == "item":
                node_id = _param(params, "id", 160)
                if not node_id:
                    raise _QueryError()
                if node_id not in records:
                    raise _QueryError("ITEM_NOT_FOUND", "这条记忆已不存在，请重新读取星图。")
                data = records[node_id][0]
            else:
                matched = [d for d, body in records.values() if _matches(d, body, query)]
                matched.sort(key=lambda d: (-(d["node"]["time"]["end"] or d["node"]["time"]["start"] or -1), d["node"]["id"]))
                # Relative prose changes with the clock, but is not a mutation of the stored record.
                revision = _digest([{k: v for k, v in d.items() if k != "rememberedText"} for d in matched])
                query_hash = _digest(query)
                raw_cursor = _param(params, "cursor", 1024)
                start = _cursor(raw_cursor, query_hash, revision, len(matched)) if raw_cursor else 0
                chosen = matched[start:start + query["limit"]]
                stop = start + len(chosen)
                cursor = None
                if stop < len(matched):
                    cursor = base64.urlsafe_b64encode(_json({"v": 1, "q": query_hash, "r": revision, "o": stop}).encode()).decode().rstrip("=")
                data = {"items": [d["node"] for d in chosen], "page": {"matched": len(matched), "returned": len(chosen), "nextCursor": cursor}}
        return {"ok": True, "schemaVersion": SCHEMA_VERSION, "generatedAt": now, "utcOffsetMinutes": offset, "data": data}
    except _QueryError as exc:
        return _error(exc)
