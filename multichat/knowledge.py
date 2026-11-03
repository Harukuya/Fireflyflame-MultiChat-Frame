"""
知识库 · 读取可编辑资料并接进对话

⭐ 怎么找、交什么在 kb_match.py；这里只管：读 kb.json、接上向量、记住刚聊过哪件事。
⭐ 两条路：自动（他说了话、有线索就去找）；回想（她用「回想」工具自己去想）。
"""

from __future__ import annotations

import numpy as np

from . import persona
from .data_bundle import read_json_resource
from .embed import MODEL_ID, VECTOR_FORMAT, Embedder, unpack_vec
from .kb_ledger import build_idf, ledger_step
from .kb_match import Prepared, match_knowledge, prepare, split_clauses, strip_call, strip_lead

RECENT_MS = 10 * 60_000


class Knowledge:
    def __init__(self, kb_path: str, embedder: Embedder, logger, *, data=None):
        self.log = logger
        self.embedder = embedder
        kb = dict(data if data is not None else read_json_resource(kb_path))
        for field in ("keys", "missions", "anchors", "self"):
            kb.setdefault(field, [])
        same = kb.get("model") == MODEL_ID and kb.get("format") == VECTOR_FORMAT
        vecs = None
        if same and kb["keys"] and all(k.get("vec") for k in kb["keys"]):
            try:
                vectors = [unpack_vec(k["vec"]) for k in kb["keys"]]
                vecs = np.stack(vectors)
                if vecs.shape[1] == 0 or not np.isfinite(vecs).all():
                    raise ValueError("empty or non-finite vectors")
            except Exception:  # 自填资料的向量不完整时，字面检索仍然可用。
                vecs = None
                logger.warning("[MultiChat·知识库] 向量格式无效，改用字面检索")
        elif any(k.get("vec") for k in kb["keys"]):
            logger.warning("[MultiChat·知识库] 向量缺失或模型、格式不匹配，改用字面检索")
        self.p: Prepared = prepare(kb, vecs)
        self.vector_ready = vecs is not None
        self.idf = build_idf([t for m in kb["missions"]
                              for t in [*[r["text"] for r in (m.get("recall") or [])], *[l["text"] for l in m["her"]]]])
        # 刚想起的事、刚聊过的那几件——只在内存里，重启就清
        self.ledger: list[dict] = []
        self.recent: list[dict] = []
        logger.info(f"[MultiChat·知识库] {len(kb['missions'])} 件事 · {len(kb['keys'])} 把钥匙 · {'向量＋字面' if vecs is not None else '字面检索'}")

    async def _parts(self, texts: list[str]) -> list[dict]:
        out = []
        for text in texts:
            vec = None
            if self.p.vecs is not None:
                v = await self.embedder.embed([strip_lead(text)])
                vec = v[0] if v else None
            out.append({"text": text, "vec": vec})
        return out

    async def lookup(self, query: str, now: int) -> str | None:
        """自动：拿他刚说的话去找以前的事。返回交给她的那一块。"""
        text, commit = await self.lookup_deferred(query, now)
        commit()
        return text

    async def lookup_deferred(self, query: str, now: int):
        """（这边新加的）先找、先不记「刚聊过哪件」：她真开口了再调 commit()。
        群里判断要不要接话时用——没接，就当没想起过。返回 (交给她的那一块, commit)"""
        if not query.strip() or not self.p.kb["keys"]:
            return None, lambda: None
        self.recent = [x for x in self.recent if now - x["at"] < RECENT_MS]
        parts = await self._parts(split_clauses(strip_call(query)))
        result = match_knowledge(self.p, parts, "assoc", {x["m"] for x in self.recent})
        before = self.ledger
        step = ledger_step(before, result, query, self.idf, now)
        text = "\n\n".join(x for x in [persona.build_knowledge_context(step["shown"]),
                                       persona.build_recently_recalled_note(step["listed"])] if x) or None
        if step["back"]:
            self.log.info(f"[MultiChat·刚想起的事] 回看 {[x['hit']['mission'] for x in step['back']]}")
        if text:
            self.log.info(f"[MultiChat·以前的事] {result['cue'] or '无线索'} {[(h['mission'], h['score']) for h in result['hits']]}"
                          + (f" 自己 {len(result['self'])} 条" if result["self"] else ""))

        def commit() -> None:
            for h in result["hits"]:
                self.recent = [{"m": h["m"], "at": now}, *[x for x in self.recent if x["m"] != h["m"]]][:4]
            # 中间别处（比如私聊）也找过：在它的基础上再记一次
            self.ledger = step["ledger"] if self.ledger is before else ledger_step(self.ledger, result, query, self.idf, now)["ledger"]

        return text, commit

    async def recall(self, about: str) -> str | None:
        """回想：她自己写了想回想什么。只返回以前的事那部分。"""
        if not about.strip() or not self.p.kb["keys"]:
            return None
        result = match_knowledge(self.p, await self._parts([about]), "recall")
        self.log.info(f"[MultiChat·回想·以前的事] {about} → {[h['mission'] for h in result['hits']] or '没有'}")
        return persona.knowledge_recall_lines(result)
