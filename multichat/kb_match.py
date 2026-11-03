"""
知识库 · 检索与知情边界（纯函数）

规则（D91、D93）：
  ① 别名、话题只是钥匙，永远不进她的提示词
  ② 交给她的只有她知道的版本
  ③ 亲历 → 她那一侧；她自己的事、别处知道的 → 「她的版本」带上怎么知道的；
     可能知道 → 标成听说；不知道 → 只一句「你不在场／你当时没察觉」，不带任何细节
TUNE 保留默认检索阈值；自备资料和向量应根据实际查询验证命中效果。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np

from . import profile
from .jsutil import js_fixed, js_trim, strip_ps
from .text import FUNCTION_CHARS, split_clauses, tokens  # noqa: F401  (split_clauses 给调用方用)

INF = float("inf")


def by_story_key(h: dict) -> float:
    """按她经历的先后；和先后无关的放最后（稳定排序，打平的保持原来的顺序）"""
    at = h.get("at")
    return INF if at is None else at


PAST = ["还记得", "记不记得", "记得吗", "那天", "那次", "那时", "当时", "以前", "之前", "上次", "第一次",
        "那会儿", "曾经", "我们一起", "你说过", "你跟我说", "你告诉我", "当初", "那晚", "那一次", "那几天", "那阵子"]
REFERRING = re.compile(r"那(个|张|位|次|段|句|家|只|首|件|天|晚|时|些|里|边)|后来|然后呢|还有呢|刚(刚|才)|你(说|讲|提)的")
_POINTING_ONLY = re.compile(r"那(个|张|位|次|段|句|家|只|首|件|天|晚|时|些|里|边)|后来|然后|还有|刚(刚|才)|你(说|讲|提)的|再(说说|讲讲|多说|多讲|说一说|讲一讲)|(接着|继续|展开|详细)(说|讲|聊)|多(说|讲)(点|一点|些)?|事|呢|吧|吗|啊")
_QUOTING = re.compile(r"你(当时|那时)?(说|讲|问)(过)?[「“\"『‘]")


def has_content(text: str) -> bool:
    return any(v == 1 for v in tokens(_POINTING_ONLY.sub(" ", text)).values())


TUNE = {
    "lexUnit": 1.5, "semUnit": 0.45, "semFloor": 0.33, "lexNeedsSem": 0.30,
    "recallLexUnit": 1.0, "recallSemUnit": 0.40, "recallSemFloor": 0.28,
    "strongMine": 2, "strongOther": 2.5, "sticky": 0.6, "self": 1.0,
    "maxMissions": 3, "compactFrom": 2, "compactLines": 3, "maxLines": 6, "maxTold": 3, "maxSaid": 3,
    "maxRecall": 3, "nearSame": 0.1, "segLex": 0.01, "layerWeight": 0.85,
    "importantSem": 0.03, "importantLex": 0.1, "maxSelf": 2, "rel": 0.65, "tie": 0.01, "journeyName": 1,
    "mineRel": 0.5, "absentStrict": True, "absentSpread": 8, "absentRel": 0.75,
}


def _is_layer_key(k: dict) -> bool:
    return k["kind"] in ("recall", "fact")


@dataclass
class Prepared:
    kb: dict
    docs: list[dict]
    df: dict
    vecs: np.ndarray | None
    recall_keys: dict
    n_base: int
    _her_text: dict | None = None
    _spread: dict | None = None
    _anchor_re: dict | None = None


def prepare(kb: dict, vecs: np.ndarray | None) -> Prepared:
    keys, missions = kb["keys"], kb["missions"]
    docs = [tokens(k["text"]) for k in keys]
    # ⭐ 字词的稀有度只按稳定的那批钥匙算（不算回忆层、旅程、剧情外的）
    df: dict[str, int] = {}
    n_base = 0
    for j, k in enumerate(keys):
        mis = missions[k["m"]] if 0 <= k["m"] < len(missions) else None
        if _is_layer_key(k) or (mis and mis.get("journey")) or (mis and mis["key"].startswith("外/")):
            continue
        n_base += 1
        for g in docs[j]:
            df[g] = df.get(g, 0) + 1
    recall_keys: dict[int, list[tuple[int, int]]] = {}
    for j, k in enumerate(keys):
        if _is_layer_key(k):
            recall_keys.setdefault(k["m"], []).append((j, k["i"]))
        elif k["kind"] == "words":
            segs = (missions[k["m"]].get("recall") or []) if 0 <= k["m"] < len(missions) else []
            seg = next((i for i, s in enumerate(segs)
                        if any(t in k["text"] or k["text"] in t for t in (s.get("said") or []))), -1)
            if seg >= 0:
                recall_keys.setdefault(k["m"], []).append((j, seg))
    return Prepared(kb, docs, df, vecs, recall_keys, n_base, {}, None, {})


def _has_anchor(p: Prepared, text: str, a: str) -> bool:
    if "(" in a:
        r = p._anchor_re.get(a)
        if r is None:
            r = p._anchor_re[a] = re.compile(a)
        return bool(r.search(text))
    return a in text


def _her_text(p: Prepared, m: int) -> str:
    c = p._her_text
    if m not in c:
        mis = p.kb["missions"][m]
        c[m] = "\n".join([*[r["text"] for r in (mis.get("recall") or [])], *[l["text"] for l in mis["her"]]])
    return c[m]


def _anchor_spread(p: Prepared) -> dict:
    if p._spread is None:
        missions = p.kb["missions"]
        mine = [m for m in range(len(missions)) if missions[m]["her"] or missions[m].get("recall")]
        p._spread = {a: sum(1 for m in mine if _has_anchor(p, _her_text(p, m), a)) for a in p.kb["anchors"]}
    return p._spread


def cue_of(p: Prepared, text: str) -> str | None:
    if any(_has_anchor(p, text, a) for a in p.kb["anchors"]):
        return "专名"
    if any(a in text for a in PAST) or _QUOTING.search(text):
        return "往事"
    return None


_LEAD = re.compile(r"^(他的|他|我的|我|你的|你)")


def strip_lead(t: str) -> str:
    """按意思比之前去掉开头的「我／你／他」——和打包时一样"""
    return _LEAD.sub("", js_trim(t), count=1)


def strip_call(text: str) -> str:
    """去掉带停顿的当前角色称呼；句子中实际谈论这个名字时仍然保留。"""
    t = js_trim(text)
    name = re.escape(profile.bot_name())
    t = re.sub(rf"^{name}[\s，,。.!！?？~～、…]+", "", t, count=1)
    t = re.sub(rf"[\s，,。.!！?？~～、…]+{name}[\s。.!！?？~～…]*$", "", t, count=1)
    return js_trim(t) or js_trim(text)


def _new_acc() -> dict:
    return {"best": 0.0, "sem": -1.0, "her": 0.0, "told": [], "unknown": [], "words": []}


def match_knowledge(p: Prepared, parts: list[dict], mode: str, recent: set[int] | None = None) -> dict:
    """
    parts: [{"text", "vec"}]，第一个是整段话（按字面比、看线索都用它），后面是拆开的各句——按意思比取最像的那句。
    mode: "assoc" 他说了话自动去找（要线索） / "recall" 她自己去回想（不要线索，门槛按关键词）
    """
    is_recall = mode == "recall"
    keys, missions = p.kb["keys"], p.kb["missions"]
    empty = {"hits": [], "self": [], "cue": None}
    query = parts[0]["text"] if parts else ""
    if not js_trim(query):
        return empty
    qvecs = [x["vec"] for x in parts if x.get("vec") is not None]

    uni = 0.3
    if is_recall:
        meaningful = [ch for ch in strip_ps(query) if ch not in FUNCTION_CHARS]
        uni = 0.5 if len(meaningful) <= 1 else 0.25
    q = tokens(query, uni)
    # 自填资料可能只有回忆层，或者完全为空；不能把 log(1 + 0) 当分母。
    n = max(1, p.n_base)
    unit = math.log(1 + n)
    lex_unit = TUNE["recallLexUnit"] if is_recall else TUNE["lexUnit"]
    sem_unit = TUNE["recallSemUnit"] if is_recall else TUNE["semUnit"]
    floor = TUNE["recallSemFloor"] if is_recall else TUNE["semFloor"]
    use_sem = len(qvecs) > 0 and p.vecs is not None

    sims = None
    if use_sem:
        sims = np.max(np.stack([p.vecs @ v for v in qvecs]), axis=0)

    nk = len(keys)
    sim_only = [-1.0] * nk
    lex_only = [0.0] * nk
    u = [0.0] * nk
    rarity = {g: 0.4 + 0.6 * math.log(1 + n / p.df.get(g, 1)) / unit for g in q}
    for j in range(nk):
        doc = p.docs[j]
        l = 0.0
        for g, w in q.items():
            if g in doc:
                l += w * rarity[g]
        lex_only[j] = min(2, l / lex_unit)
        if not use_sem:
            u[j] = l / lex_unit
            continue
        sim = float(sims[j])
        sim_only[j] = sim
        if not is_recall and sim < TUNE["lexNeedsSem"]:
            l *= 0.5
        u[j] = max(l / lex_unit, (sim - floor) / (sem_unit - floor))

    cue = None if is_recall else cue_of(p, query)
    referring = bool(REFERRING.search(query)) and has_content(query)

    def need(k: dict) -> float:
        if k["kind"] == "self":
            return 1 if is_recall else TUNE["self"]
        if is_recall:
            return 1
        if recent and k["m"] in recent and (cue or referring):
            return TUNE["sticky"]
        if k["cls"] == "mine":
            return 1 if cue else TUNE["strongMine"]
        return 1 if cue == "专名" else TUNE["strongOther"]

    by_m: dict[int, dict] = {}
    selfs: list[tuple[int, float]] = []
    for j, k in enumerate(keys):
        uj = u[j] * TUNE["layerWeight"] if _is_layer_key(k) else u[j]
        if uj < need(k):
            continue
        if k["kind"] == "self":
            selfs.append((k["i"], u[j]))
            continue
        a = by_m.get(k["m"])
        if a is None:
            a = by_m[k["m"]] = _new_acc()
        a["best"] = max(a["best"], uj)
        a["sem"] = max(a["sem"], sim_only[j])
        if k["kind"] == "words":
            a["her"] = max(a["her"], u[j])
            a["words"].append(j)
        elif _is_layer_key(k):
            a["her"] = max(a["her"], uj)
        elif k["cls"] == "mine" and (k["kind"] == "her" or k.get("how") == "亲历"):
            a["her"] = max(a["her"], u[j])
        elif k["cls"] == "unknown":
            a["unknown"].append(j)
        else:
            a["told"].append(j)

    # 不在场的背景段落：提到对应的人名地名时给出角色可知的部分。
    if cue == "专名":
        for name, ms in (p.kb.get("journeyNames") or {}).items():
            if name not in query:
                continue
            for m in ms:
                a = by_m.get(m)
                if a is None:
                    a = by_m[m] = _new_acc()
                a["best"] = max(a["best"], TUNE["journeyName"])

    ranked = sorted(by_m.items(), key=lambda x: -x[1]["best"])
    top = ranked[0][1]["best"] if ranked else 0
    # 分数差不到 0.01 的算打平，意思更像的排前面
    i = 0
    while i < len(ranked):
        j = i + 1
        while j < len(ranked) and ranked[i][1]["best"] - ranked[j][1]["best"] < TUNE["tie"]:
            j += 1
        if j - i > 1:
            ranked[i:j] = sorted(ranked[i:j], key=lambda x: -x[1]["sem"])
        i = j
    chosen = [x for x in ranked if x[1]["best"] >= top * TUNE["rel"]][:TUNE["maxMissions"]]

    def has_mine(a: dict) -> bool:
        return a["her"] > 0 or any(keys[j]["cls"] == "mine" for j in a["told"])

    def wholly(m: int) -> bool:
        return not missions[m]["her"] and not missions[m].get("recall")

    absent_top = TUNE["absentStrict"] and len(ranked) > 0 and wholly(ranked[0][0]) and not has_mine(ranked[0][1])
    if absent_top:
        spread = _anchor_spread(p)
        named = [a for a in p.kb["anchors"] if _has_anchor(p, query, a) and spread.get(a, 0) < TUNE["absentSpread"]]

        def mentions(m: int) -> bool:
            return any(_has_anchor(p, _her_text(p, m), a) for a in named)

        chosen = [(m, a) for m, a in chosen
                  if wholly(m) or mentions(m) or ((not has_mine(a) or len(a["unknown"]) > 0) and a["best"] >= top * TUNE["absentRel"])]
    if not absent_top and not any(has_mine(a) for _, a in chosen):
        def mine_score(a: dict) -> float:
            return max([a["her"], *[u[j] for j in a["told"] if keys[j]["cls"] == "mine"]])
        mine = next((x for x in ranked if has_mine(x[1]) and mine_score(x[1]) >= top * TUNE["mineRel"]), None)
        if mine:
            chosen.insert(min(1, len(chosen)), mine)
            chosen = chosen[:TUNE["maxMissions"]]

    hits = []
    for rank, (m, a) in enumerate(chosen):
        mis = missions[m]
        if mis.get("journey"):
            hits.append({"mission": mis["key"], "m": m, "score": js_fixed(a["best"]), "mine": [], "said": [],
                         "told": [{"know": "知道", "how": "她自己", "text": t} for t in mis["journey"]],
                         "unknown": ["不在场"] if mis.get("away") else [], "at": mis.get("at"), "period": mis.get("period")})
            continue
        compact = rank >= TUNE["compactFrom"]

        def close(j: int, a=a) -> bool:
            return u[j] >= a["best"] * TUNE["rel"]

        mine_lines: list[dict] = []
        her_ok = a["her"] > 0 and (a["her"] >= a["best"] * TUNE["rel"] or a["her"] >= top * TUNE["mineRel"])
        recall_segs = mis.get("recall") or []
        if her_ok and recall_segs:
            ov = next((i for i, r in enumerate(recall_segs) if r.get("overview")), -1)
            seg: dict[int, float] = {}
            for j, i in p.recall_keys.get(m, []):
                if i == ov:
                    continue
                base = sim_only[j] + TUNE["segLex"] * lex_only[j] if use_sem else u[j]
                boost = (TUNE["importantSem"] if use_sem else u[j] * TUNE["importantLex"]) if recall_segs[i].get("important") else 0
                seg[i] = max(seg.get(i, -INF), base + boost)
            order = sorted(seg.items(), key=lambda x: -x[1])
            if order:
                best, best_u = order[0]
                near = next((x for x in order if abs(x[0] - best) == 1), None)
                details = [best, *([near[0]] if near else [])]
                other = next((x for x in order if x[0] not in details
                              and x[1] >= best_u - (TUNE["nearSame"] if use_sem else best_u * 0.1)), None)
                if other and len(details) < TUNE["maxRecall"]:
                    details.append(other[0])
                if not any(recall_segs[i].get("important") for i in details):
                    imp = next((x for x in order if recall_segs[x[0]].get("important")), None)
                    if imp:
                        details.append(imp[0])
                picked = [ov if ov >= 0 else best] if compact else [*([ov] if ov >= 0 else []), *sorted(details)]
            else:
                picked = [ov] if ov >= 0 else []
            for i in picked:
                r = recall_segs[i]
                mine_lines.append({"text": r["text"], "said": r["said"]} if r.get("said") and not compact else {"text": r["text"]})
        elif her_ok and mis["her"]:
            idx = list(range(len(mis["her"])))
            cap = TUNE["compactLines"] if compact else TUNE["maxLines"]
            if len(idx) > cap:
                score: dict[int, float] = {}
                for j, k in enumerate(keys):
                    if k["kind"] == "her" and k["m"] == m:
                        score[k["i"]] = u[j]
                idx = sorted(sorted(idx, key=lambda x: -score.get(x, 0))[:cap])
            mine_lines = [mis["her"][i] for i in idx]
        # 查询指向角色不在场的那部分：如果设有可知概要，仍交出概要。
        if not mine_lines and not compact and any(close(j) for j in a["unknown"]):
            ov_seg = next((r for r in recall_segs if r.get("overview")), None)
            if ov_seg:
                mine_lines = [{"text": ov_seg["text"]}]
        told: list[dict] = []
        for j in sorted([j for j in a["told"] if close(j)], key=lambda j: -u[j]):
            k = keys[j]
            if not k.get("gist") or any(t["text"] == k["gist"] for t in told):
                continue
            told.append({"know": k.get("know") or "知道", "how": k.get("how") or "", "text": k["gist"]})
            if len(told) >= (1 if compact else TUNE["maxTold"]):
                break
        w_best = max([0, *[u[j] for j in a["words"]]])
        carried = [s for l in mine_lines for s in (l.get("said") or [])]
        said: list[dict] = []
        if mine_lines and not compact:
            words = mis.get("words") or []
            for j in sorted([j for j in a["words"] if u[j] >= w_best * TUNE["rel"]], key=lambda j: -u[j]):
                w = words[keys[j]["i"]]
                if any(c in w["text"] or w["text"] in c for c in carried):
                    continue
                said.append(w)
            said = said[:TUNE["maxSaid"]]
        unknown: list[str] = []
        for j in a["unknown"]:
            if not close(j):
                continue
            kind = "没察觉" if keys[j].get("then") == "在场" else "不在场"
            if kind not in unknown:
                unknown.append(kind)
        if mine_lines or told or unknown:
            hits.append({"mission": mis["key"], "m": m, "score": js_fixed(a["best"]), "mine": mine_lines, "said": said,
                         "told": told, "unknown": unknown, "at": mis.get("at"), "period": mis.get("period")})
    hits.sort(key=by_story_key)

    self_top = max([0, *[s[1] for s in selfs]])
    picked_self = sorted([s for s in selfs if s[1] >= self_top * TUNE["rel"]], key=lambda s: -s[1])[:TUNE["maxSelf"]]
    return {"hits": hits, "self": [p.kb["self"][s[0]]["text"] for s in picked_self], "cue": cue}
