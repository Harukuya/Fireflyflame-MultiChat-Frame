"""
她能用的工具（原版 tools.ts）

⭐ 原则：除了她发出去的话，其他由模型决定的事都走工具。
⭐ 每一轮只给她当下用得上的工具；说明文字跟着工具走——不给这个工具，这段字就不在提示词里。
description 也是提示词：原版部分保留；本版另给「这轮的感受」加可选的自身情绪。「一起听」这次不做，没有那两个工具。
2026-10-29 原版更新（复刻包旁边的「更新-多想几步与回他的方式」）：加了想一想（think）、回想说明加半句、这轮的感受加 way。
"""

from __future__ import annotations

from .jsutil import js_trim
from .mood import EMOTIONS
from .ways import way_of_label

NO_INPUT = {"type": "object", "properties": {}}

TIER_LABEL = {"soon": "马上", "a_while": "一会儿", "later": "晚点"}
TIER_DESC = {
    "soon": "马上 = 发完又想补一句，一分钟以内",
    "a_while": "一会儿 = 过几分钟",
    "later": "晚点 = 过很久，可能是你下次上线、手头没事的时候",
}


def keep_thought(tiers: list[str]) -> dict:
    return {
        "name": "keep_thought",
        "description": "".join([
            "这轮话说完了，但你心里还挂着点什么，想过一阵再说：补一句刚才的话、忽然想起的事、或者想问他的事。",
            "留下这个念头，到时候你会再看一眼那时的情况，再决定说不说。",
            "他说了要走、在忙，或者这件事已经聊完了，就不用留。",
            "不留，就表示这轮说完了。",
        ]),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "你想说什么，几个字的提示就行。只给你自己看，不会发出去。"},
                "when": {
                    "type": "string",
                    "enum": [TIER_LABEL[t] for t in tiers],
                    "description": f"多久以后：{'；'.join(TIER_DESC[t] for t in tiers)}。",
                },
            },
            "required": ["topic", "when"],
        },
    }


WAIT_MORE = {
    "name": "wait_more",
    "description": "现在还不是说的时候，但这件事你还想说。再等一阵，到时候你会再看一次。这只能用一次。用了就不要再写消息。",
    "input_schema": NO_INPUT,
}

LET_GO = {
    "name": "let_go",
    "description": "这会儿不说了：话题过去了、他好像在忙、或者你其实没什么想说的。不用解释。用了就不要再写消息。",
    "input_schema": NO_INPUT,
}

RECALL = {
    "name": "recall",
    "description": "".join([
        "回想以前的事：你和他聊过的，或者你经历过的。他问你还记不记得什么、或者你想确认以前的事时用。",
        "写下想回想什么，会告诉你还记得的概述、事实和约定，不会翻出旧聊天逐字记录。",
        "其中明确留下的、你自己说过的原话，适合当前对象和语境时可以直接使用。没有记下的细节就说记不清，不要把概述编成原句。",
        "要回想就先回想，拿到结果再开口。想起来的事又让你想到别的，可以接着回想。",
        "他聊到的事，你觉得和以前的哪件事有关、但一时想不起细节时，也可以回想。",
        "问到你今天其他时段或昨天做什么，用 source=schedule 查日程，day 指定哪一天，period 可指定时段；这与聊天记忆、背景知识分开，未提供的具体经历不能补编。",
        "问到你的名字、身体、过去、同伴或背景处境，用 source=identity、about=具体问题，只取相关身份资料；当前地点和任务仍查 schedule。",
    ]),
    "input_schema": {
        "type": "object",
        "properties": {
            "about": {"type": "string", "description": "想回想什么，几个字"},
            "source": {"type": "string", "enum": ["memory", "schedule", "identity"], "description": "默认 memory 查聊天记忆及背景；schedule 只查你自己的近期日程，不搜索背景知识；identity 只查身份、自我认识和背景处境，不搜索聊天记忆或剧情。"},
            "day": {"type": "string", "enum": ["today", "yesterday", "before_yesterday", "future"], "description": "schedule 专用，均相对于当前回复时刻：今天／昨天／前天及更早／明天及以后。旧消息中的今天、昨天须按它的发言日换算后再选；后两类只返回记不清或尚未告知的边界。"},
            "period": {"type": "string", "maxLength": 40, "description": "schedule 专用：刚刚（今天往前半小时，含跨午夜）、上午、下午、晚上、HH:mm 或 HH:mm-HH:mm；省略查该日安排。时间按通讯器时钟理解。"},
        },
        "required": ["about"],
    },
}

# 想一想（2026-10-29 原版加的「多想几步」）：想的内容只给她自己看，不发出去、不进聊天记录、不进记忆
THINK = {
    "name": "think",
    "description": "".join([
        "开口之前，先在心里想一想：他说的事你很在意、很感兴趣，或者你想认真回他、一时拿不准怎么说的时候。",
        "写下你心里在想什么，只给你自己看，不会发出去。想好了就直接回他；还想接着想、或者想回想以前的事，可以再用。",
        "随口闲聊不用想。",
    ]),
    "input_schema": {
        "type": "object",
        "properties": {"thought": {"type": "string", "description": "你心里在想什么"}},
        "required": ["thought"],
    },
}

FEEL_LABEL = {"up": "更来劲了", "ok": "还行", "drain": "有点耗神"}


def round_feeling(ways: list[dict] | None = None, way_note: str | None = None) -> dict:
    """这轮的感受。2026-10-29 原版加了「怎么回的」（ways.py）：只列这一轮能用的方式；说明跟着工具走，不给就不在提示词里。
    ways：这一轮能用的方式 [{id, label, desc}]，不传 = 不报方式；way_note：way 的说明（群里换个说法，不传就是原版那句）"""
    props: dict = {}
    if ways:
        listed = "；".join(f"{w['label']} = {w['desc']}" for w in ways)
        props["way"] = {
            "type": "string",
            "enum": [w["label"] for w in ways],
            "description": way_note or f"这轮你是怎么回他的：{listed}。不必每次都照常回复，看你想怎么回；别连着几轮都在问他、向他要东西。",
        }
    return {
        "name": "round_feeling",
        "description": "回完他的话，顺手记一下这一轮你自己的感受。每次回他都记。只给你自己记着，不会告诉他，也不要在消息里提。",
        "input_schema": {
            "type": "object",
            "properties": {
                **props,
                "energy": {
                    "type": "string",
                    "enum": list(FEEL_LABEL.values()),
                    "description": "这轮聊下来，你的精神：跟他聊得开心、来劲，选「更来劲了」；平平常常，选「还行」；聊得费神、沉重，或者你本来就累，选「有点耗神」。",
                },
                "real": {
                    "type": "boolean",
                    "description": "这轮聊到了实在的东西——他跟你说了心里话、你们认真聊了某件事，或者你觉得被他在乎了。随口寒暄不算。",
                },
                "emotion": {
                    "type": "string",
                    "enum": list(EMOTIONS),
                    "description": "这轮之后你自己的情绪变化：变得轻快、开心或被安慰；平稳、没有明显变化；变得难过、失落或烦闷。这不是你和对方的亲疏。没有明显变化可以不填。",
                },
                "curious": {
                    "type": "string",
                    "description": "他说的事里，有你真想多知道的，写几个字，比如「他新养的猫」。没有就不填。",
                },
            },
            "required": ["energy", "real", "way"] if ways else ["energy", "real"],
        },
    }


ROUND_FEELING = round_feeling()  # 不报方式的那版

FACT_CATEGORIES = ["基本情况", "喜好", "身边的人和事", "在意的事", "你们之间"]

MEMORY_SELECTION = """【哪些值得长期留下】
先筛选，再记。不是聊天纪要，也不需要覆盖每个话题；可以整批什么都不记，episodes 填 [] 仍然是完整、成功的结果。0～3 段只是上限，不是配额。
只留下以后仍有意义的具体经历、重要互动或关系变化。普通寒暄、吃喝报备、随口感叹、重复的梗、已经结束的小插曲通常不留；不能为了显得有意义而补出感动、承诺或情绪变化，也不要为了让琐事留得更久而抬高重要性。
人物事实只留稳定、以后用得上的信息；临时状态不是长期事实。原话也要挑以后确实值得再提的，不把日常回复逐句收藏。已有内容没有变化就不再交一份。
没有事件可记，仍可单独更新确有依据的人物印象、已有事实或关闭已经结束的悬事；没有任何变化就全部留空。
【哪些事情仍值得挂着】
只选同时满足「明确具体、确实尚未完成、以后值得再关心」的约定、承诺或重要待定结果。普通未来时、客套的下次再聊、吃饭睡觉报备、愿望、没聊完的话题都不算。
每轮记事中的「未落定」只是当时的候选，必须结合后面的内容重新判断；现在已完成、已取消或不再值得跟进的，不要继续挂着。
悬事每个会话最多六条。每条写清值得跟进的依据，并选保留 1～7 天，从本批对话的时间算，不是从整理那一刻重新计时。没有合适的就填 []。
同一件事不要换说法重复新增；已结束或过期的旧项不能简单重新挂起。修改已有项带 id，修改不能延长原期限。确有实质的新约定时才关闭旧项并新增，写明新时间或新事项，不能照抄旧内容。旧项已有结果、取消或已无跟进价值，用 closed_threads 关闭；满六条时先认真取舍，不为了凑数挤掉仍重要的旧事。"""

THREAD_SCHEMA = {
    "type": "array",
    "maxItems": 6,
    "description": "值得后续关心的具体未完成事项，可全部为空；不是所有没说完的话题。已有事项修改须带 id，不能借修改续期。",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "修改已有悬事时填原编号；新增时省略。"},
            "content": {"type": "string", "description": "具体是谁、什么事、还在等什么，80 字以内。"},
            "kind": {"type": "string", "enum": ["约定", "承诺", "重要待定"]},
            "reason": {"type": "string", "description": "依据本批真实内容，为什么以后仍值得关心；不能只写尚未完成。"},
            "keep_days": {"type": "integer", "minimum": 1, "maximum": 7,
                          "description": "值得继续挂几天，按事情的预计进展选择 1～7；不要全部默认七天。"},
        },
        "required": ["content", "kind", "reason", "keep_days"],
    },
}

_TOPICS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "话题：这件事属于哪些话题，写 3～6 个词，要包括更大的类别。比如聊到他的猫，写「宠物」「猫」「动物」；聊到他爸爸住院，写「家人」「父亲」「健康」。",
}

SAVE_MEMORY = {
    "name": "save_memory",
    "description": "交出筛选后的记忆；可以整批不记，episodes: [] 是有效结果。没有变化的各项留空。",
    "input_schema": {
        "type": "object",
        "properties": {
            "trailblazer_impression": {
                "type": "string",
                "description": "你现在对主要联系人的印象，80 字以内；只根据这段里他本人的发言或和你的实际互动，有变化才写。亲密度固定，不打分。",
            },
            "episodes": {
                "type": "array",
                "description": "以后仍值得记住的经历或互动，0～3 段，上限不是配额；琐事可以全部不留。",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "integer", "description": "这段从第几条开始"},
                        "to": {"type": "integer", "description": "到第几条结束"},
                        "summary": {"type": "string", "description": "聊了什么：这段聊了什么、怎么聊完的，两三句话。"},
                        "gist": {"type": "string", "description": "记得最久的一句：过了很久你还会记得的那一句。"},
                        "feeling": {"type": "string", "description": "感受：那次的气氛或他的状态，几个字，比如「他很累」「聊得很开心」。"},
                        "weight": {"type": "integer", "description": "只给已筛选值得留下的内容评分：1～2 有一点短期意义的互动，3 有实质内容，4 很重要，5 会一直记得的事。普通闲聊不必因能打1分就保存。"},
                        "topics": _TOPICS,
                    },
                    "required": ["from", "to", "summary", "gist", "feeling", "weight"],
                },
            },
            "my_words": {
                "type": "array",
                "description": "我说过的话：你自己说过、以后可能想再提的话——玩笑、给东西起的绰号、打的比方、答应他的事。原话照抄，一个字都不要改。",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "原话"},
                        "about": {"type": "string", "description": "当时在聊什么，几个字"},
                    },
                    "required": ["text", "about"],
                },
            },
            "facts": {
                "type": "array",
                "description": "关于他的事：过三个月还用得上的，只记他自己说的，已经记得的不用重复。",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": list(FACT_CATEGORIES)},
                        "content": {"type": "string"},
                        "topics": _TOPICS,
                    },
                    "required": ["category", "content"],
                },
            },
            "fact_changes": {
                "type": "array",
                "description": "有变化的：已经记得的事变了，写编号和新内容；不再成立就写「作废」。",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "content": {"type": "string"}},
                    "required": ["id", "content"],
                },
            },
            "threads": THREAD_SCHEMA,
            "closed_threads": {
                "type": "array",
                "description": "关闭已经有结果、取消或不再值得跟进的悬事，写原编号。",
                "items": {"type": "integer"},
            },
        },
        "required": ["episodes"],
    },
}

TAG_MEMORIES = {
    "name": "tag_memories",
    "description": "给这些记忆补上话题。",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer", "description": "编号"}, "topics": _TOPICS},
                    "required": ["id", "topics"],
                },
            },
        },
        "required": ["items"],
    },
}


def tools_for(replying: bool, thought_tiers: list[str], recheck_can_wait: bool | None = None,
              think: bool = False, ways: list[dict] | None = None) -> list[dict]:
    """这一轮给她哪些工具。recheck_can_wait 不是 None 表示是到点掂量那一轮。
    2026-10-29：think = 给不给「想一想」（只在回他话的那轮）；ways = 这一轮能用的回他的方式（ways.py），不传 = 不报方式。
    回他话那轮的顺序：想一想、回想、留个念头（能留时）、这轮的感受。"""
    out: list[dict] = []
    if replying and think:
        out.append(THINK)
    if replying:
        out.append(RECALL)
    if replying and thought_tiers:
        out.append(keep_thought(thought_tiers))
    if replying:
        out.append(round_feeling(ways))
    if recheck_can_wait is not None:
        if recheck_can_wait:
            out.append(WAIT_MORE)
        out.append(LET_GO)
    return out


def ends_turn(call: dict) -> bool:
    return call["name"] in ("wait_more", "let_go")


def reads_result(call: dict) -> bool:
    """这几个工具要先把结果交给她，她才能开口（「回想」「想一想」）。交给 llm.py 的 chat_with_tools"""
    return call["name"] in ("recall", "think")


def result_for(call: dict, last: bool = False) -> str:
    """回传给她的执行结果。last：这是能想的最后一步了（「回想」「想一想」接下来就收走了，她得开口）"""
    name = call["name"]
    if name == "think":
        return "思考到这里，决定这轮要回复还是不回复。" if last else "嗯。想清楚后再决定这轮是否回复。"
    if name == "keep_thought":
        return "记下了。到时候你会再想一想要不要说。"
    if name == "round_feeling":
        return "记下了。"
    if name in ("wait_more", "let_go"):
        return "好。"
    return "没有这个动作。"


def intent_from_calls(calls: list[dict]) -> dict:
    out: dict = {}
    for c in calls:
        name, inp = c["name"], c.get("input") or {}
        if name == "keep_thought":
            tier = next((t for t, label in TIER_LABEL.items() if label == inp.get("when")), None)
            topic = js_trim(inp["topic"])[:40] if isinstance(inp.get("topic"), str) else ""
            if tier:
                out["thought"] = {"topic": topic, "tier": tier}
        elif name == "round_feeling":
            out["meaningful"] = inp.get("real") is True
            out["feel"] = next((f for f, label in FEEL_LABEL.items() if label == inp.get("energy")), None)
            emotion = inp.get("emotion")
            out["emotion"] = emotion if isinstance(emotion, str) and emotion in EMOTIONS else "steady"
            q = js_trim(inp["curious"])[:30] if isinstance(inp.get("curious"), str) else ""
            if q:
                out["curious"] = q
            out["way"] = way_of_label(inp.get("way"))
        elif name == "think" and isinstance(inp.get("thought"), str):
            out["thoughts"] = [*out.get("thoughts", []), js_trim(inp["thought"])]
        elif name == "wait_more":
            out["wait"] = True
        elif name == "let_go":
            out["letGo"] = True
    return out
