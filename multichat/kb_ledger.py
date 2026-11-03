"""
刚想起的事：轻量的「回看」（原版 kb-ledger.ts）

交给她的以前的事只活一轮，下一轮她就看不到刚才想起过什么。这里记一本小账：
  - 每轮只放一行目录（刚才想起过哪几件，一件一句话）
  - 他明确在说刚才的事，或者他的话碰到了刚才那件事里的内容 → 把那件事当时交的段落再交一次
"""

from __future__ import annotations

import math
import re

from .jsutil import jre, js_trim
from .kb_match import REFERRING, by_story_key
from .text import tokens

LEDGER_MS = 20 * 60_000
LEDGER_MAX = 3
LEDGER_TUNE = {"touchMin": 3.2, "touchStrong": 7}

_POINTING = re.compile("|".join([
    "你刚(刚|才)?(说|讲|提|想起)", "刚(刚|才)(说|讲|提|想起|聊)", "(刚才|刚刚)(那件|那段|那个|那次|的事)",
    "再(说说|讲讲|多说|多讲|说一说|讲一讲)", "(接着|继续|展开|详细)(说|讲|聊)", "多(说|讲)(点|一点|些)",
]))
_FOLLOW_UP = jre(r"^(然后呢|后来呢|还有呢|再后来呢|之后呢|然后)[\s？?。.!！…~～]*$")


def points_back(text: str) -> bool:
    t = js_trim(text)
    return bool(_POINTING.search(t) or _FOLLOW_UP.search(t))


def _words(text: str) -> set[str]:
    return {w for w, v in tokens(text).items() if v == 1 and len(w) == 2}


def build_idf(texts: list[str]) -> dict[str, float]:
    df: dict[str, int] = {}
    for t in texts:
        for w in _words(t):
            df[w] = df.get(w, 0) + 1
    n = max(1, len(texts))
    return {w: math.log(n / d) for w, d in df.items()}


def _hit_text(h: dict) -> str:
    parts = [x for l in h["mine"] for x in [l["text"], *(l.get("said") or [])]]
    parts += [s["text"] for s in h["said"]] + [t["text"] for t in h["told"]]
    return "\n".join(parts)


def _label_of(h: dict) -> str | None:
    first = h["mine"][0]["text"] if h["mine"] else h["told"][0]["text"] if h["told"] else None
    if not first:
        return None
    s = re.split(r"(?<=[。！？])", first)[0]
    return s[:46] + "……" if len(s) > 48 else s


def _uniq(xs: list, key) -> list:
    seen, out = set(), []
    for x in xs:
        k = key(x)
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    return out


def _merge(a: dict, b: dict) -> dict:
    m = _uniq([*a["mine"], *b["mine"]], lambda l: l["text"])
    mine = [x for x in [m[0] if m else None, *m[1:][-4:]] if x]
    unknown = list(dict.fromkeys([*a["unknown"], *b["unknown"]]))
    return {**b, "mine": mine, "said": _uniq([*a["said"], *b["said"]], lambda s: s["text"]),
            "told": _uniq([*a["told"], *b["told"]], lambda t: t["text"]), "unknown": unknown}


def remember(ledger: list[dict], hits: list[dict], now: int) -> list[dict]:
    out = [x for x in ledger if now - x["at"] < LEDGER_MS]
    # 同一轮想起好几件：最贴近他那句话的排最前（「然后呢」接的是它）
    for h in sorted(hits, key=lambda h: h["score"]):
        if not h["mine"] and not h["told"]:
            continue
        old = next((x for x in out if x["m"] == h["m"]), None)
        hit = _merge(old["hit"], h) if old else h
        label = old["label"] if old else _label_of(hit)
        if not label:
            continue
        out = [{"m": h["m"], "at": now, "hit": hit, "label": label, "words": _words(_hit_text(hit))},
               *[x for x in out if x["m"] != h["m"]]]
    return out[:LEDGER_MAX]


def touch_score(item: dict, text: str, idf: dict[str, float]) -> float:
    return sum(idf.get(w, 0) for w in _words(text) if w in item["words"])


def look_back(ledger: list[dict], text: str, fresh: set[int], idf: dict[str, float], now: int) -> list[dict]:
    live = [x for x in ledger if now - x["at"] < LEDGER_MS and x["m"] not in fresh]
    if not live:
        return []
    pointed = points_back(text) and not any(x["m"] in fresh for x in ledger)
    scored = sorted(({"x": x, "s": touch_score(x, text, idf)} for x in live), key=lambda y: -y["s"])
    need = LEDGER_TUNE["touchMin"] if REFERRING.search(text) else LEDGER_TUNE["touchStrong"]
    touched = [y["x"] for y in scored if y["s"] >= need]
    if touched:
        return touched[:2]
    if pointed:
        return [scored[0]["x"] if scored[0]["s"] > 0 else live[0]]
    return []


def ago(at: int, now: int) -> str:
    mins = (now - at) / 60_000
    return "刚才" if mins < 2 else "几分钟前" if mins < 10 else "十几分钟前"


def ledger_step(ledger: list[dict], result: dict, query: str, idf: dict[str, float], now: int) -> dict:
    fresh = {h["m"] for h in result["hits"]}
    back = look_back(ledger, query, fresh, idf, now)
    shown = ({**result, "hits": sorted([*result["hits"], *[x["hit"] for x in back]], key=by_story_key)}
             if back else result)
    out = fresh | {x["m"] for x in back}
    listed = [{"label": x["label"], "ago": ago(x["at"], now)} for x in ledger
              if x["m"] not in out and now - x["at"] < LEDGER_MS]
    return {"shown": shown, "listed": listed, "back": back, "ledger": remember(ledger, result["hits"], now)}
