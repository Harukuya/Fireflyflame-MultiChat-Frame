"""
角色认识的人物 · 可编辑样例

⭐ 对话提到谁（名字或别名），就交【你眼里的某某】：人物资料 + 几句代表性的话。
⭐ 原话按意思挑；close 亲近人物最多 5 句，其他人物 3 句。一轮最多 2 个人。
"""

from __future__ import annotations

import re

import numpy as np

from . import persona
from .data_bundle import read_json_resource
from .embed import MODEL_ID, VECTOR_FORMAT, Embedder, unpack_vec

MAX_PEOPLE = 2


class People:
    def __init__(self, path: str, embedder: Embedder, *, data=None):
        data = data if data is not None else read_json_resource(path)
        self.people: list[dict] = data.get("people", [])
        self.same = data.get("model") == MODEL_ID and data.get("format") == VECTOR_FORMAT
        self.embedder = embedder
        self._not = {p["name"]: re.compile(p["not"]) for p in self.people if p.get("not")}
        self._vecs = {p["name"]: [unpack_vec(q["vec"]) if self.same and q.get("vec") else None for q in p["quotes"]] for p in self.people}
        self.vector_ready = any(vs and all(v is not None for v in vs) for vs in self._vecs.values())

    def mentioned(self, text: str) -> list[dict]:
        """他这几句话里提到了谁，按先提到的排"""
        found = []
        for p in self.people:
            r = self._not.get(p["name"])
            # 排除的说法换成同样长的全角空格，位置不变
            t = r.sub(lambda m: "　" * len(m.group(0)), text) if r else text
            at = min((t.index(n) for n in p["names"] if n in t), default=None)
            if at is not None:
                found.append((at, p))
        found.sort(key=lambda x: x[0])
        return [p for _, p in found[:MAX_PEOPLE]]

    async def _pick_quotes(self, p: dict, query: str) -> list[dict]:
        k = 5 if p["tier"] == "close" else 3
        quotes = p["quotes"]
        if len(quotes) <= k:
            return quotes
        vecs = self._vecs[p["name"]]
        q = None
        if self.same and all(v is not None for v in vecs):
            got = await self.embedder.embed([query])
            q = got[0] if got else None
        if q is None:
            return quotes[:k]
        scored = sorted(((i, float(np.dot(q, vecs[i]))) for i in range(len(quotes))), key=lambda x: -x[1])[:k]
        return [quotes[i] for i, _ in sorted(scored, key=lambda x: x[0])]

    async def lookup(self, text: str) -> str | None:
        ps = self.mentioned(text)
        if not ps:
            return None
        cards = [persona.build_person_card(p, await self._pick_quotes(p, text)) for p in ps]
        return "\n\n".join(cards)
