"""
按字面比的分词：记忆和知识库共用（原版 text.ts）

中文按两字一组（bigram）比，单字只算一点点；太常见的字词不算。
"""

from __future__ import annotations

import re

from .jsutil import js_trim, jre, strip_ps

STOP_BIGRAMS = {
    "我们", "你们", "他们", "什么", "怎么", "没有", "一个", "这个", "那个", "不是", "就是", "还是",
    "可以", "知道", "觉得", "现在", "时候", "一下", "真的", "有点", "然后", "因为", "所以", "但是",
    "如果", "这样", "那样", "自己", "已经", "还有", "一点", "应该", "记得", "还记", "得吗", "的事",
    "说过", "你说", "我说", "他说", "以前", "之前", "后来", "今天", "明天", "昨天", "最近", "一直",
    "总是", "特别", "非常", "开始", "正在", "还在", "好像", "其实", "感觉", "一起", "起来", "出来",
    "东西", "事情", "不过", "而且", "有没", "怎样", "为什", "那边", "这边", "一样", "时间",
    "第一",
}
# 虚字：自己不成意思
FUNCTION_CHARS = set("的了是我你他她它们这那就也都还又在有和与吗呢吧啊呀哦嗯么个一不没很太好说会要想去来到上下里着过把被给让对跟得地吧呗啦嘛")
# 不单独拿来比的字
STOP_CHARS = FUNCTION_CHARS | set("小大天人点些多少看做用能再才只最更真而从向将比等自己以之其为所如然后前时年月日今明昨次回")


def tokens(text: str, uni: float = 0.3) -> dict[str, float]:
    """两字一组算 1，单字算 uni（联想 0.3；回想时她只写了一个字给 0.5）。保持插入顺序，和原版的 Map 一样。"""
    clean = strip_ps(text)
    t: dict[str, float] = {}
    for i in range(len(clean) - 1):
        g = clean[i:i + 2]
        if g in STOP_BIGRAMS or (g[0] in STOP_CHARS and g[1] in STOP_CHARS):
            continue
        t[g] = 1
    for ch in clean:
        if ch not in STOP_CHARS and ch not in t:
            t[ch] = uni
    return t


_CLAUSE_SPLIT = re.compile(r"[。！？!?；;…\n]+|[「」“”\"『』]")
_CLAUSE_EDGE = jre(r"^[\s，,、~～]+|[\s，,、~～]+$")


def split_clauses(text: str) -> list[str]:
    """长消息拆成几句，按意思比时每句单独比、取最像的那句。返回 [整段, 各句…]。"""
    full = js_trim(text)
    if len(full) <= 15:
        return [full]
    parts = [_CLAUSE_EDGE.sub("", x) for x in _CLAUSE_SPLIT.split(full)]
    parts = [x for x in parts if len(strip_ps(x)) >= 4]
    if len(parts) <= 1:
        return [full]
    return [full, *parts[:5]]
