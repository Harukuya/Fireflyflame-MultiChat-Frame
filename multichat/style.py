"""
风格库 · 可编辑的说话样例

⭐ 风格样例：按意思挑 4 条，同一段短信最多 2 条。⚠️ 不设门槛，永远给——她的说话方式一直都在。
⭐ 口吻参考：按意思挑，最多 2 句，够不上 0.35 就不给。
模型用不了时，样例退回按字挑，口吻参考不给。
"""

from __future__ import annotations

import math
import re

import numpy as np

from .embed import MODEL_ID, VECTOR_FORMAT, Embedder, unpack_vec
from .data_bundle import read_json_resource
from .jsutil import strip_p

PER_GROUP = 2
VOICE_MIN = 0.35


def _bigrams(text: str) -> dict[str, int]:
    clean = strip_p(text)
    m: dict[str, int] = {}
    for i in range(len(clean) - 1):
        g = clean[i:i + 2]
        m[g] = m.get(g, 0) + 1
    if not m:
        for ch in clean:
            m[ch] = 1
    return m


def _cosine(a: dict, b: dict) -> float:
    dot = sum(v * b.get(g, 0) for g, v in a.items())
    if dot == 0:
        return 0
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb)


class Style:
    def __init__(self, path: str, embedder: Embedder, *, data=None):
        data = data if data is not None else read_json_resource(path)
        if isinstance(data, list):
            data = {"samples": data}
        self.samples: list[dict] = data.get("samples", [])
        self.voice: list[dict] = data.get("voice", [])
        self.same = data.get("model") == MODEL_ID and data.get("format") == VECTOR_FORMAT
        self.embedder = embedder
        self._svecs = [unpack_vec(s["vec"]) if self.same and s.get("vec") else None for s in self.samples]
        self._vvecs = [unpack_vec(v["vec"]) if self.same and v.get("vec") else None for v in self.voice]
        self.vector_ready = bool(self._svecs and all(v is not None for v in self._svecs)) or any(v is not None for v in self._vvecs)

    def _rank(self, query: str, k: int) -> list[dict]:
        """按字挑（模型用不了时）"""
        q = _bigrams(query)
        scored = []
        for s in self.samples:
            ctx = _bigrams(s["context"] or "".join(s["reply"]))
            all_ = _bigrams(f"{s['context']} {' '.join(s['reply'])}")
            scored.append((s, _cosine(q, ctx) * 0.75 + _cosine(q, all_) * 0.25))
        scored.sort(key=lambda x: -x[1])
        return [s for s, _ in scored[:k]]

    async def _qvec(self, query: str):
        if not self.same or not query.strip():
            return None
        got = await self.embedder.embed([query])
        return got[0] if got else None

    async def samples_for(self, query: str, k: int = 4) -> list[dict]:
        if not self.samples:
            return []
        q = await self._qvec(query) if all(v is not None for v in self._svecs) else None
        if q is None:
            return self._rank(query, k)
        scored = sorted(((s, float(np.dot(q, v)) if v is not None else -1) for s, v in zip(self.samples, self._svecs)),
                        key=lambda x: -x[1])
        out, used = [], {}
        for s, _ in scored:
            g = re.sub(r"-\d+$", "", s["id"])
            if used.get(g, 0) >= PER_GROUP:
                continue
            used[g] = used.get(g, 0) + 1
            out.append(s)
            if len(out) >= k:
                break
        return out

    async def voice_for(self, query: str, k: int = 2) -> list[dict]:
        if not self.voice:
            return []
        q = await self._qvec(query) if any(v is not None for v in self._vvecs) else None
        if q is None:
            return []
        scored = [(v, float(np.dot(q, vec)) if vec is not None else -1) for v, vec in zip(self.voice, self._vvecs)]
        scored = sorted([x for x in scored if x[1] >= VOICE_MIN], key=lambda x: -x[1])
        return [v for v, _ in scored[:k]]
