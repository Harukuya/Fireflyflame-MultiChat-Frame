"""平台无关的提示词组装：可编辑角色配置 + 来源边界 + 对话与记忆协议。"""

from __future__ import annotations

import re

from .jsutil import js_round, js_trim
from . import profile
from .tools import MEMORY_SELECTION

def __getattr__(name: str) -> str:
    # 兼容其他框架模块读取这些属性；每次读取都使用最新的可编辑配置。
    fragments = {"IDENTITY": "identity", "VOICE": "voice", "KNOWLEDGE_BOUNDARY": "boundaries"}
    if name in fragments:
        return profile.prompt(fragments[name])
    raise AttributeError(name)


PROHIBITIONS = """
【对话边界】
不要无依据地替对方描述感受、编造承诺或共同经历。没有理解就说明，不懂装懂不能代替回应。
不要把历史摘要、工具结果或风格样例当成本轮对方刚说的话。是否提问、安慰或给建议，应结合实际语境。
""".strip()

FORMAT = """
【输出格式】
- **回复长短跟着对方这轮要回应的话走：对方说得多、讲得细，你也多说几句；对方说得少、只是简短一句，你也简短回应。如果对方的话虽然多，但内容比较空、重复或只是随口闲聊，你也少说一点，简单接住就好。每条单独占一行。**
- **只看本轮要回应的内容，不按历史记录、回忆或风格参考的篇幅决定回复长短。**
- 具体情形另有说明时，按那次的情形说。
- 只输出消息内容本身。不要写动作描写、不要写旁白、不要用括号或星号描述表情和动作。
- 不要在自己的消息前添加角色名字前缀。
""".strip()

FORMAT_TOOLS = """
- 想做的事，用给你的工具去做。不要写进消息里，也不要在消息里描述你在操作什么。
""".strip()

_SELF: str | None = None


def build_self_block(md: str) -> str | None:
    """「## 给角色的」（兼容「## 给她的」）到「## 出处」之间那半页；### 标题变成「标题：」。"""
    parts = re.split(r"^## (?:给角色的|给她的).*$", md, flags=re.M)
    if len(parts) < 2:
        return None
    part = re.split(r"^## 出处", parts[1], flags=re.M)[0]
    if not js_trim(part):
        return None
    lines = []
    for line in part.split("\n"):
        if not js_trim(line):
            continue
        if line.startswith("### "):
            line = js_trim(re.sub(r"（一人一句[^）]*）", "", line[4:], count=1)) + "："
        lines.append(line)
    return "\n".join(["【你对自己的认识】", "这些是你的身份和经历，聊到了再说，不用主动讲出来；这里的记述不作为本轮说话的口吻范例。", *lines])


def load_self(md_text: str | None) -> None:
    global _SELF
    _SELF = build_self_block(md_text) if md_text else None


def build_system_prompt(mood: str | None = None, channel: str = "plain", identity_line: str | None = None,
                        *, energy_note: str | None = None) -> str:
    """身份、口吻、边界来自 prompts；会话身份不会覆盖用户填写的人设。"""
    fmt = f"{FORMAT}\n{FORMAT_TOOLS}" if channel == "tools" else FORMAT
    context = identity_line or f"你现在正在和{profile.primary_name()}私聊。"
    self_text = profile.self_block(_SELF)
    blocks = [profile.prompt("identity"), context, *([self_text] if self_text else []),
              profile.prompt("voice"), profile.prompt("boundaries"), PROHIBITIONS, fmt]
    state = build_state_context(mood, energy_note)
    if state:
        blocks.append(state)
    return "\n\n".join(blocks)


def build_return_context(away_minutes: float, activity: str) -> str:
    m = js_round(away_minutes)
    how_long = (f"大约 {m} 分钟前" if m < 90 else f"大约 {js_round(m / 60)} 小时前" if m < 60 * 24 else "一天多以前")
    how_to_open = ("如果顺口可以带一句你刚才在忙什么，但直接接他的话也完全可以——隔得不久，不用特意交代。"
                   if m < 40 else "开头先说一句你刚才在哪、在忙什么，然后再回应他说的内容。")
    return "\n".join([
        "【本次回复的额外情况】",
        f"你刚上线。对方在你不在的时候给你留了话，{how_long}发的，你现在才看到。",
        f"你不在的这段时间：{activity}",
        "",
        f"→ {how_to_open}",
        "→ 语气随意。不要郑重道歉，也不要反复解释——按你们实际的关系自然回应。",
        "→ **按这次要回应的留言多少和内容分量来回：他留得多、讲得细，你也多说几句；只留了简短的话，就简短回应。如果对方的话虽然多，但内容比较空、重复或只是随口闲聊，你也少说一点，简单接住就好。**",
    ])


def build_farewell_context(minutes_left: float) -> str:
    return "\n".join([
        "【本次的额外情况】",
        f"你这边差不多还有 {js_round(minutes_left)} 分钟就得下线了（去休息，或处理别的事）。",
        "",
        "→ 回应他之后，顺便说一句你要走了。别郑重其事，随口交代就行。",
        "→ 不要解释太多，也不要问他介不介意。",
        "→ 说清楚是**你**要下线了，不要说成让他去休息。",
    ])


def build_standalone_farewell_context(minutes_left: float) -> str:
    return "\n".join([
        "【本次的额外情况】",
        f"你这边差不多还有 {js_round(minutes_left)} 分钟就得下线了（去休息，或处理别的事）。",
        "你们刚才还在说话，所以你主动发一句，告诉他你要走了。",
        "",
        "→ 这不是在回答他的话——他刚才那句你已经回过了。你只是要说一声。",
        "→ 只说 1～2 条，短。随口交代，别郑重其事，也别问他介不介意。",
        "→ 说清楚是**你**要下线了，不要说成让他去休息。",
    ])


def _strip_period(s: str) -> str:
    return s[:-1] if s.endswith("。") else s


def build_recheck_context(topic: str, planted: bool, curious: bool, ago: str, activity: str,
                          minutes_left: float | None) -> str:
    leaving = (f"你再过大约 {max(1, js_round(minutes_left))} 分钟就要下线了。"
               if minutes_left is not None and minutes_left < 30 else "")
    now_line = f"那是{ago}以前的事了，之后他一直没再说话。你现在：{_strip_period(activity)}。{leaving}"
    if curious:
        return "\n".join([
            "【本次的额外情况】",
            f"你刚才对他说的「{topic}」挺好奇，还想多知道一些。",
            now_line,
            "",
            "→ 想问就直接问，1 条，短。不要催他、不要问「在吗」、不要重复问过的话。",
        ])
    if planted:
        return "\n".join([
            "【本次的额外情况】",
            "你们刚才聊得正起劲，你说完话还意犹未尽。",
            now_line,
            "",
            "→ 想说就直接写，1～2 条，短：接着刚才的话、补一句，或者说说你这边的事。不要催他、不要问「在吗」、不要重复说过的意思。",
        ])
    return "\n".join([
        "【本次的额外情况】",
        f"你之前说完话，心里留了个念头{'：' + topic if topic else '，还想再说点什么'}",
        now_line,
        "",
        "→ 想说就直接写，1～2 条，短，不必照着念头说。不要催他、不要问「在吗」、不要重复说过的意思。",
    ])


_ENERGY_LINES = {
    "tired-first": "你有点累了——今天聊了挺久，是聊久了的那种累，不是身体出了问题。你还没跟他提过。",
    "tired-again": "你有点累了——今天聊了挺久，是聊久了的那种累，不是身体出了问题。",
    "tired-still": "你还是有点累。",
    "recovered": "刚才你有点累；这会儿跟他聊着，精神反而回来了些。",
}


ENERGY_GUIDANCE = (
    "**精力影响你愿意投入多少力气：有精神时，可以把话说细、顺着话题多展开；有点乏时，回应重点，少开新话题；"
    "累了时，可以简短接住要紧的话，不必勉强延长聊天。**"
    "\n这不代表你对对方冷淡或心情不好；重要的问题仍认真回应，具体说多少同时看本轮要回应的内容。"
)


def build_state_context(mood: str | None = None, energy_note: str | None = None) -> str:
    """同块给出情绪、条件触发的疲劳／恢复说明，以及精力对表达投入的影响。"""
    lines = [text for text in (mood, _ENERGY_LINES[energy_note] if energy_note else None) if text]
    return "\n".join(["【你现在的状态】", *lines, ENERGY_GUIDANCE]) if lines else ""


def build_energy_context(note: str) -> str:
    """兼容只需要精力提示的旧调用。"""
    return f"【你现在的状态】\n{_ENERGY_LINES[note]}"


def build_carry_thought_context(topic: str) -> str:
    return "\n".join([
        "【你心里还挂着的事】",
        f"你上次说完话，本来还想说：{topic}",
        "→ 想顺带说就说；接不上他现在的话、或者不想说了，就算了。",
    ])


# 主动联系保留表达期待的能力；不预设关系，不责怪对方未回复。
PROACTIVE_WANT_LIVE = True


def build_proactive_context(activity: str, is_night: bool, hours_since_talk: float, neglected: bool) -> str:
    lines = [
        "【本次的额外情况】",
        "他现在不在。你没有在回复任何人——是你自己想说点什么，主动发过去的。",
        f"你此刻：{activity}",
        f"你们上次说话是大约 {js_round(hours_since_talk)} 小时前。",
        "",
    ]
    if is_night:
        lines += [
            "→ 现在是你那边的深夜，你一个人待着。可以说点平时不太说的——",
            "  想到的事、说不清的念头、或者就是想起了他。语气仍按你的人设，不临时捏造经历。",
        ]
    elif PROACTIVE_WANT_LIVE:
        lines += [
            "→ 你正在做的事让你想起了他。可以说你看到的、遇到的、想到的；",
            "  也可以向他要点什么：想听他说说近况、想问问他的看法、想让他陪你一会儿，表达一点期待。",
            "→ 不要怪他、不要委屈——「你怎么不理我」不是你会说的话。",
        ]
    else:
        lines += [
            "→ 你正在做的事让你**顺带**想起了他。说你看到的、遇到的、想到的。",
            "→ ⚠️ 重点是**分享**，不是**索取**。不要说「我想你了」「你怎么不理我」——",
            "  「刚路过一个卖糖的摊子，想起你说过喜欢甜的」才是你会说的话。",
        ]
    if neglected:
        lines.append("→ 他已经很久没出现了。你可以提一下，但只提一句，不要追问，不要委屈。")
    lines.append("→ 只说 1～3 条，短。")
    return "\n".join(lines)


def build_asking_streak_context(n: int) -> str:
    """连着几轮都在问他、向他要东西（ways.py）：只告诉她这个事实，怎么办写在「这轮的感受」的说明里"""
    return f"【最近几轮】\n你已经连着 {n} 轮在问他、向他要东西了。"


# ---------------- 记忆 ----------------

def build_consolidate_system(known: list[str], open_: list[str]) -> str:
    return "\n".join([
        f"你是{profile.bot_name()}。你刚和{profile.primary_name()}聊过，现在在心里筛选刚才的内容，记下来。",
        f"这是你自己的记忆：用「我」指自己，用名字指{profile.primary_name()}。只记真的发生过、真的说过的，不要推测，也不要替对方补充。",
        MEMORY_SELECTION,
        "",
        "【你本来就记得的关于他的事】",
        "\n".join(known) if known else "还没有",
        "",
        "【还悬着的事】",
        "\n".join(open_) if open_ else "没有",
        "",
        "用「记下来」交出结果。",
    ])


def build_haze_system(refresh: bool) -> str:
    return "\n".join([
        f"你是{profile.bot_name()}。下面是你和{profile.primary_name()}很久以前聊过的一些事，细节你已经记不清了。",
        "用一两句话写下那阵子留给你的印象：你们常聊什么、他那时候大概是什么状态。",
        "不要写具体的细节和日子，就像过了很久再回想时那样。用「我」指自己，提到别人用名字。",
        *(["把原来的印象和这些揉在一起，还是一两句话。"] if refresh else []),
        "只写这一两句话，别的什么都不要写。",
    ])


def build_tag_system() -> str:
    return "\n".join([f"你是{profile.bot_name()}。下面是你记得的一些事，每件前面有编号。", "给每件补上话题，用「补话题」交出来。"])


def build_facts_context(facts: list[str]) -> str | None:
    if not facts:
        return None
    return "\n".join(["【你知道的关于他的事】", *[f"· {f}" for f in facts]])


def build_gap_context(ago: str, last_gist: str | None, last_feeling: str | None, whereabouts: str,
                      days_talked: int | None, threads: list[str]) -> str:
    last = ""
    if last_gist:
        gist = re.sub(r"[。！？]$", "", last_gist)
        last = f"，那次聊的是：{gist}{f'（{last_feeling}）' if last_feeling else ''}"
    return "\n".join([
        "【隔了一阵】",
        f"你们上次说话是{ago}前{last}。",
        f"这段时间你大多{whereabouts}。",
        *([f"最近两周里，你们有 {days_talked} 天说过话。"] if days_talked is not None else []),
        *([f"还悬着的事：{'；'.join(threads)}"] if threads else []),
        "→ 提不提这段空白、怎么提，你自己看。",
    ])


def build_recollection_context(lines: list[str]) -> str | None:
    if not lines:
        return None
    return "\n".join(["【你想起来的事】", _RECOLLECTION_SOURCE, _REMEMBERED_WORDS,
                      *[f"· {l}" for l in lines], "→ 是你自己想起来的，接得上就提，接不上就不提。"])


def build_first_contact_context() -> str:
    return "\n".join([
        "【第一次联系】",
        "这是当前会话记录中的第一次交流。按你的人设自然回应，不据此编造共同过去，也不假定彼此已经很熟。",
    ])


# ---------------- 以前的事（知识库） ----------------

def _told_section(t: dict) -> str:
    if t["know"] == "可能知道":
        return "你好像听说过，细节不清楚"
    how = t["how"]
    if how in ("本人", "她自己"):
        return "你自己的事"
    if how == "听主要联系人说":
        return "主要联系人跟你说过"
    if how == "后来知道":
        return "你后来知道"
    if how in ("常识", "设定"):
        return "你本来就知道"
    return "你知道"


_THOUGHT_RE = re.compile(r"^（[\s\S]*）$")


def _is_thought(t: str) -> bool:
    return bool(_THOUGHT_RE.match(js_trim(t)))


def _unwrap_thought(t: str) -> str:
    return re.sub(r"^（|）$", "", js_trim(t)).replace("）（", "")


def _her_line(l: dict) -> str:
    said = l.get("said") or []
    spoken = [s for s in said if not _is_thought(s)]
    thought = [s for s in said if _is_thought(s)]
    return (l["text"]
            + ("（他那时也在，后来忘了；你不知道他想起来没有）" if l.get("remember") else "")
            + ("（他当时不知道）" if l.get("hidden") else "")
            + (f"\n  你那时说的：{''.join(f'「{s}」' for s in spoken)}" if spoken else "")
            + (f"\n  你那时心里想的（没说出口）：{''.join(f'「{_unwrap_thought(s)}」' for s in thought)}" if thought else ""))


def _carried_said(r: dict) -> list[str]:
    return [s for h in r["hits"] for l in h["mine"] for s in (l.get("said") or [])]


def _is_carried(t: dict, carried: list[str]) -> bool:
    return any(t["text"] in c or c in t["text"] for c in carried)


def _said_line(t: dict, seen: set[str]) -> str:
    quote = f"（心里想的，没说出口）「{_unwrap_thought(t['text'])}」" if _is_thought(t["text"]) else f"「{t['text']}」"
    note = t.get("note")
    if not note:
        return quote
    first = note not in seen
    seen.add(note)
    return f"{quote}（{note if first else '同上，是那时的理解'}）"


def _unknown_note(kinds: set[str], also_known: bool) -> str | None:
    away, unaware = "不在场" in kinds, "没察觉" in kinds
    if not away and not unaware:
        return None
    if also_known:
        what = "有些你不在场，有些你当时没察觉" if away and unaware else "有些你不在场" if away else "有些你当时没察觉"
        return f"他提到的事里，{what}，不知道是怎么回事。"
    what = "，有些你不在场，有些你当时没察觉" if away and unaware else "你不在场" if away else "你当时没察觉"
    return f"他提到的这件事{what}，不知道是怎么回事。"


def _event_block(lines: list[str]) -> str:
    """一件她经历过的事：概要在前，细节缩进跟在后面"""
    details = ["  - " + l.replace("\n", "\n  ") for l in lines[1:]]
    return "\n".join([lines[0], *details])


LIVED = "你经历过（按发生的先后）"
FORGOT_WHEN = "（具体是什么时候，你忘了）"
_REST = ["你自己的事", "主要联系人跟你说过", "你后来知道", "你本来就知道", "你知道", "你好像听说过，细节不清楚"]

# 用途说明紧跟对应资料标题，不在主提示词最前面堆总规则。
_RECOLLECTION_SOURCE = "**这些是以前的事，不是本轮新消息。按标明的群聊、私聊或经历来源理解；过去的感受不自动代表你现在的情绪。**"
_REMEMBERED_WORDS = "**其中明确记录的、你自己说过的原话，如果适合当前对象和语境，可以直接使用。**其余记述只帮助理解事情，不作为说话的口吻范例；概述不能当成当时的原句。"


def _period_title(period: str) -> str:
    return f"「{period}」这段时间内，你自己的事"


def _knowledge_sections(r: dict) -> tuple[dict[str, list[str]], set[str]]:
    sections: dict[str, list[str]] = {LIVED: [], "你那时说过的话": []}
    seen: set[str] = set()

    def add(title: str, line: str) -> None:
        if line in seen:
            return
        seen.add(line)
        sections.setdefault(title, []).append(line)

    kinds: set[str] = set()
    notes: set[str] = set()
    carried = _carried_said(r)
    for h in r["hits"]:
        def own(t: dict) -> bool:
            return t["how"] in ("本人", "她自己") and t["know"] != "可能知道"
        placed = h["mission"].startswith("外/") and h.get("at") is not None and not h.get("period")
        lived = [*[_her_line(l) for l in h["mine"]], *([t["text"] for t in h["told"] if own(t)] if placed else [])]
        if lived:
            add(LIVED, _event_block(lived))
        for t in h["said"]:
            if not _is_carried(t, carried):
                add("你那时说过的话", _said_line(t, notes))
        for t in h["told"]:
            if placed and own(t):
                continue
            if h.get("period") and own(t):
                add(_period_title(h["period"]), f"{t['text']}{FORGOT_WHEN}" if h["mission"].startswith("外/") else t["text"])
            else:
                add(_told_section(t), t["text"])
        for u in h["unknown"]:
            kinds.add(u)
    for s in r["self"]:
        add("你自己的事", s)
    ordered = {t: v for t, v in sections.items() if t not in _REST}
    for t in _REST:
        if t in sections:
            ordered[t] = sections[t]
    ordered = {t: v for t, v in ordered.items() if v}
    return ordered, kinds


def build_knowledge_context(r: dict) -> str | None:
    if not r["hits"] and not r["self"]:
        return None
    if not r["hits"]:
        return "\n".join(["【关于你自己】", "这些是关于你的事实，不是本轮新消息或说话的口吻范例。", *[f"· {s}" for s in r["self"]]])
    sections, kinds = _knowledge_sections(r)
    out = ["【以前的事，你知道的】", _RECOLLECTION_SOURCE, _REMEMBERED_WORDS]
    for title, lst in sections.items():
        out += [f"{title}：", *[f"· {l}" for l in lst]]
    note = _unknown_note(kinds, len(sections) > 0)
    if note:
        out.append(note)
    out.append("→ 只按你知道的说；不知道的就是不知道。")
    return "\n".join(out)


def build_recently_recalled_note(items: list[dict]) -> str | None:
    if not items:
        return None
    return "\n".join(["【你刚才想起过】", "这是之前回忆过的事，不是本轮新发生的对话。", *[f"· {x['label']}（{x['ago']}）" for x in items],
                      "→ 这里只记了是哪件事；细节想不全的地方别自己补，他接着问起，你会再想起来。"])


def knowledge_recall_lines(r: dict) -> str | None:
    if not r["hits"] and not r["self"]:
        return None
    sections, kinds = _knowledge_sections(r)
    out = ["【以前的事】", _RECOLLECTION_SOURCE, _REMEMBERED_WORDS]
    for title, lst in sections.items():
        out += [f"{title}：", *[f"· {l}" for l in lst]]
    away, unaware = "不在场" in kinds, "没察觉" in kinds
    if away or unaware:
        what = "有些你不在场，有些你当时没察觉" if away and unaware else "有些你不在场" if away else "有些你当时没察觉"
        only = what if away and unaware else "你不在场" if away else "你当时没察觉"
        out.append(f"其中{what}，不知道是怎么回事。" if sections else f"这件事{only}，不知道是怎么回事。")
    return "\n".join(out)


def build_person_card(p: dict, quotes: list[dict]) -> str:
    out = [f"【你眼里的{p['name']}】", f"这些资料帮助你理解{p['name']}，不作为你平时说话的风格参考。", *p["two"]]
    if quotes:
        pron = p["pron"]
        out.append(f"你转述过{pron}的话（是你自己说的，不是{pron}的原话）：" if p.get("retold")
                   else f"{pron}说过的几句话（帮助你理解这个人，别当成刚发生的事）：")
        for q in quotes:
            when = f"（当时：{q['when']}）" if q.get("when") else ""
            out.append(f"· 「{q['text']}」{when}")
        if p["tier"] == "close" and not p.get("retold"):
            out.append(f"你偶尔可以模仿{p['name']}开个玩笑：把模仿的那句话放进引号，紧接着点明是在学谁，"
                       f"比如「{p['name']}大概会这么说，对吧？」或者「这话倒很像{p['name']}会说的」。"
                       "模仿只限这一句，之后恢复自己的口吻；临时模仿不能说成对方真的说过。")
    return "\n".join(out)


# ---------------- 风格库 ----------------

def build_style_context(samples: list[dict]) -> str | None:
    if not samples:
        return None
    blocks = []
    for s in samples:
        as_is = s.get("source") is not None and s.get("source") != "消息样例"
        ctx = s.get("context") or ""
        head = "（你主动开口）" if not ctx else ctx if as_is else f"对方：{ctx}"
        body = "\n".join(f"你：{r}" for r in s["reply"])
        blocks.append(f"{head}\n{body}")
    return "\n".join([
        "【风格参考｜你平时是怎么发消息的】",
        "**下面参考你的语气、节奏、用词和分条方式；其中你自己说过的原话，如果适合当前对象和语境，也可以直接使用。**",
        "样例中的对方发言只交代当时语境，不作为你的口吻范例；样例里的事件不是当前正在发生的事。",
        "",
        "\n\n".join(blocks),
    ])


def build_voice_ref_context(lines: list[dict]) -> str | None:
    if not lines:
        return None
    return "\n".join(["【风格参考｜你说过的话】", "**下面是你自己的台词。可以参考口吻；意思、对象和当下情境都合适时，也可以直接使用原句。**",
                      "不用为了套用台词而转移话题，不把原句所属的旧事件当成现在正在发生。同一句不必反复使用。",
                      *[f"· {l['text']}" for l in lines]])
