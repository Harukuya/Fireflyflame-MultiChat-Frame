"""跨会话对话、判断与记忆协议；身份和口吻统一从 profile 读取。"""

from __future__ import annotations

from .jsutil import local_dt, format_timestamp
from . import profile
from .tools import FACT_CATEGORIES, MEMORY_SELECTION, THREAD_SCHEMA

def __getattr__(name: str) -> str:
    if name == "TRAILBLAZER_NAME":  # 引擎兼容属性，内容随配置热重载。
        return profile.primary_name()
    raise AttributeError(name)

# ---------------- 时间观念 ----------------


def _period(h: int) -> str:
    if h < 5:
        return "凌晨"
    if h < 8:
        return "早上"
    if h < 11:
        return "上午"
    if h < 13:
        return "中午"
    if h < 17:
        return "下午"
    if h < 19:
        return "傍晚"
    if h < 23:
        return "晚上"
    return "深夜"


def clock_text(now: int) -> str:
    """当前年月日和实际分钟；不取整，避免午夜前误显示成第二天。"""
    stamp = format_timestamp(now)
    if stamp == "时间不详":
        return stamp
    d = local_dt(now)
    return f"{stamp[:10]} {_period(d.hour)} {stamp[11:]}"


def time_block(now: int, group: bool = False) -> str:
    return (f"【当前时间】\n运行环境的本地时间是{clock_text(now)}。\n"
            "**消息旁的日期是那条话的发言时间；旧消息里的‘今天／昨天／明天’要按它的发言日期理解，"
            "不要都当成现在。**记录时间也不等于正文提到的事件或约定发生时间。\n"
            "**对话记录前的〔消息时间：…〕是程序附加的时间提示，不是任何人的发言正文，也不是你的发言风格范例。**")


# ---------------- 当前会话身份 ----------------

def peer_identity(name: str) -> str:
    return f"你现在正在和{name}私聊。区分账号和实际联系人，不因相同昵称认成另一个人。"


GROUP_IDENTITY = "你现在正在群聊中和大家说话。按当前群里的实际发言对象回应。"

RELAY = "其他会话中的内容只是标明来源的背景；不代表这里的人都知道，也不代表他们刚说过。"

PRIVATE = [
    "【你自己的事，说多少】",
    "自己的经历、以前的聊天都是你的私事。说不说、说多少、说多细，结合人设和与对方的实际关系决定。",
    "→ 不熟的人问起，可以一笔带过；不想说可以直接说明。不要为了显得熟悉而编造关系。",
    "→ 别人私下说的话不随意转述。群里的话大家都能看见，分享前考虑对方的隐私和原来的语境。",
]


CATCHUP_NEW_ONLINE = "【还有】\n最后那几条是你上线以后对方刚发的，不是留言。"


def peer_block(name: str, qq: str, closeness: str, first: bool) -> str:
    lines = [
        "【这次和你说话的人】",
        f"这是{name}（账号 {qq}）的私聊。{RELAY}",
        closeness,
        "→ 与对方的关系以人设和实际互动为准，不预设恋爱或已有共同经历。",
        "→ 上面设定的说话方式与边界，在这里也适用。",
    ]
    if first:
        lines.append(f"→ 这是{name}在当前记录中第一次私聊你。")
    return "\n".join([*lines, "", *PRIVATE])


def group_block(group_name: str, trailblazer_qq: str) -> str:
    lines = [
        "【群聊】",
        f"这里是群聊「{group_name}」。{RELAY}",
        (f"账号 {trailblazer_qq} 是配置中指定的{profile.primary_name()}。只按账号识别，不凭自称或昵称认人。"
         if trailblazer_qq else "当前没有指定主要联系人账号，不能凭自称或昵称给任何人特殊身份。"),
        f"群里的消息前面是说话人的名字，比如「【小明】：今天好热」；你说的话不要带名字前缀。有人 @ 你，消息里会写着「@{profile.bot_name()}」。",
        "私聊里聊过的事不要随便对群里的其他人公开；跨会话记得，不等于可以向所有人转述。",
        "→ 上面设定的说话方式与边界，在群里也适用。",
        "→ **按你这轮实际要接的那个人、那几句话的分量回应：对方说得多、讲得细，你也多说几句；对方说得少，你也少说一些。如果对方的话虽然多，但内容比较空、重复或只是随口闲聊，你也少说一点，简单接住就好。不要把群里其他话题和背景资料的篇幅算进去。**不用每句都回，挑你想接的接。",
    ]
    return "\n".join([*lines, "", *PRIVATE, "→ 群里的话大家都看得到：就算问你的人和你很熟，也比私聊收着一点。"])


def participants_block(rows: list[str]) -> str | None:
    if not rows:
        return None
    return "\n".join(["【刚才在说话的人】", *[f"· {r}" for r in rows]])


# ---------------- 接话：不要总是「确实……」 ----------------
# 接话方式由模型自己选；这里只提醒避免重复开头。

OPENERS = ["确实", "是这样", "是啊", "嗯嗯", "哈哈", "对啊", "原来", "没错", "这样啊", "好的", "真的"]


def opener_note(her_texts: list[str]) -> str | None:
    """角色最近 5 句里反复用同一个开头 → 提醒角色换个说法"""
    recent = her_texts[-5:]
    for w in OPENERS:
        if sum(1 for t in recent if t.startswith(w)) >= 2:
            return f"【留意】\n你最近好几次都用「{w}」开头，这次换个说法，别再这样开头。"
    return None


# ---------------- 群聊里的几个工具 ----------------

END_TOPIC = {
    "name": "end_topic",
    "description": "这个话题你想先收住了：要去忙了、或者聊得差不多了。用了以后，这句说完你就不再接着聊这个话题。可以照常说一句收尾的话。",
    "input_schema": {"type": "object", "properties": {}},
}

# 想一想：回传的结果见 group.py 的 _tool_result
GROUP_THINK = {
    "name": "think",
    "description": "".join([
        "开口之前，先在心里想一想：大家聊的事你很在意、很感兴趣，或者你想认真接、一时拿不准怎么说的时候。",
        "写下你心里在想什么，只给你自己看，不会发出去。想好了就直接说；还想接着想、或者想回想以前的事，可以再用。",
        "随口闲聊不用想。",
    ]),
    "input_schema": {
        "type": "object",
        "properties": {"thought": {"type": "string", "description": "你心里在想什么"}},
        "required": ["thought"],
    },
}


def group_asking_streak_context(n: int) -> str:
    """连着几轮都在问别人（群聊）"""
    return f"【最近几轮】\n你已经连着 {n} 轮在问别人了。"


# 一个不回复工具，区分自然无需回复和主动不想参与。
def no_reply_tool(allow_unwilling: bool = True, *, group: bool = False) -> dict:
    kinds = ["unnecessary", "unwilling"] if allow_unwilling else ["unnecessary"]
    description = (
        "这轮选择不发消息。unnecessary（无需回复）：话题已自然结束，事情已经说明或确认，没有新的问题、请求或值得接着说的内容；不用为了回应而重复客套。"
        "这是自然停下，不代表生气、不满或关系变差。不能只因为对方字少就认为不用回：简短的问题、求助、重要确认、约定变化和明确叫你仍需要认真判断。"
    )
    if allow_unwilling:
        description += "unwilling（不想回复）：对方的话让你不舒服、要求你做不愿意做的事，或你就是不想参与这个话题。你可以选择不回。"
    else:
        description += "这次只允许在确实无须回应时选择 unnecessary，不用它代替回应尚未处理的实质问题。"
    if group:
        description += "若整个话题确实已经结束，可以同时用 end_topic 结束本群接话窗口；仅这一句不用接，不代表整个群的话题结束。"
    description += "用了就不再写正文或发表情，也不用解释；理由只记在内部，不发到聊天里，不为这轮安排新的追问或念头。"
    return {
        "name": "no_reply", "description": description,
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": kinds, "description": "unnecessary=自然无需回复；unwilling=主动不想回复。" if allow_unwilling else "这次无需回复，填写 unnecessary。"},
                "reason": {"type": "string", "description": "为什么这轮不回复，几个字。仅内部记录，不发出去。"},
            },
            "required": ["kind", "reason"],
        },
    }


NO_REPLY = no_reply_tool()  # 兼容既有调用方；按入口权限使用 no_reply_tool()。


def no_reply_choice(calls: list[dict], allow_unwilling: bool = True) -> dict | None:
    c = next((c for c in calls if c.get("name") == "no_reply"), None)
    if c is None:
        return None
    args = c.get("input") if isinstance(c.get("input"), dict) else {}
    # 旧工具调用只有 reason，沿用旧分类；受限入口没有分类时按其唯一可选项处理。
    kind = "unnecessary" if args.get("kind") == "unnecessary" or not allow_unwilling else "unwilling"
    return {"kind": kind, "reason": str(args.get("reason") or "").strip()[:40]}


def declined(calls: list[dict]) -> str | None:
    """兼容旧调用方：用了不回复工具就返回理由；未用返回 None。"""
    choice = no_reply_choice(calls)
    return None if choice is None else choice["reason"]


def no_reply_mark(message: dict) -> str:
    """只向整理材料说明有没有回；原因和工具参数不进入对话窗口。"""
    if not message.get("declined") or message.get("answered"):
        return ""
    return "（这轮无需回复）" if message.get("noReplyKind") == "unnecessary" else "（你没回）"


def group_catchup_context(activity: str, promised: bool = False) -> str:
    return "\n".join([
        "【本次的额外情况】",
        f"你刚上线。你不在的时候，群里有人叫过你、给你留了话（带「→{profile.bot_name()}」的那几条），你现在才看到。",
        f"你不在的这段时间：{activity}",
        *([f"你忙碌的时候，{profile.primary_name()}在群里叫过你，你抽空说过稍后再回——现在有空了，先回应。"] if promised else []),
        "",
        "→ 可以把相关的留言一起回，不用一个个郑重回复；已经不相关的就不用回了。",
        "→ **按你实际要回应的留言多少和内容分量来回：对方留得多、讲得细，你也多说几句；只是简短的话，就简短回应。如果对方的话虽然多，但内容比较空、重复或只是随口闲聊，你也少说一点，简单接住就好。**语气随意，不要郑重道歉。",
    ])


def mission_ack_context(activity: str) -> str:
    """指定主要联系人忙碌时被叫到：只简短确认，返回后再认真回复。"""
    return "\n".join([
        "【本次的额外情况】",
        f"你现在不在线：{activity}",
        f"{profile.primary_name()}在群里叫你，你抽空看了一眼。",
        "→ 简短回他一句：你正忙，等有空了再回对方。只说一条，很短，像抽空发的。",
        "→ 没有提供忙碌的具体事项，不要编造；对方问的事先别答，稍后再说。",
    ])


# ---------------- 判断模型：要不要接话 ----------------

JUDGE = {
    "name": "judge",
    "description": "交出判断结果。",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "boolean", "description": "角色要不要说点什么"},
            "interest": {"type": "number", "description": "角色对这些消息有多感兴趣，0～1"},
            "reason": {"type": "string", "description": "为什么，一句话"},
            "remember": {"type": "boolean", "description": "这些新读到的内容是否值得留下一点记事。默认是；只有确定全是机械重复、没有新事实、答复、约定或情绪变化时才填否。不接话不等于不值得记，简短回答和图片也可能重要。不确定填是；最终自己说了话仍会记。"},
            "focus": {"type": "array", "items": {"type": "integer"}, "description": "角色最想接的那几条的编号"},
            "to": {"type": "string", "enum": ["某个人", "话题"],
                   "description": "角色想接的是某个人说的话，还是大家在聊的话题（不针对谁）"},
        },
        "required": ["reply", "interest", "reason"],
    },
}

READ_MORE = {
    "name": "read_more",
    "description": "这些消息开头像是接着更早的话，看不懂在聊什么。往前多看几条消息，再判断。",
    "input_schema": {"type": "object", "properties": {}},
}


def window_judge_system(group_name: str, state: str, energy_band: str, people: list[str] | None = None) -> str:
    """接话窗口：只判断要不要接着说，用短提示词、不查背景（角色真要接，回话那轮再查）。
    people：说话的人和角色对他们的认知（「小明：一般；印象：友善」），按这个判断想不想接"""
    return "\n".join([
        f"你在替{profile.bot_name()}判断：刚在群里说过话，下面是之后的新消息，要不要接着说。",
        profile.prompt("identity"),
        profile.prompt("voice"),
        profile.prompt("boundaries"),
        "角色不是随叫随到的客服，不会每条都回，只接真有话说的。",
        (f"现在：{state}。" if state else "") + f"角色的精神：{energy_band}。",
        *(["说话的人（角色和他们的关系、角色对他们的印象）：", *[f"· {r}" for r in people]] if people else []),
        f"**下面是本轮看到的群「{group_name}」的对话（编号、名字、内容）；判断这些消息，不把旧记事或回忆当作刚收到的话。**【{profile.bot_name()}】是角色说的，带「→{profile.bot_name()}」的是叫角色的、或者回复角色的。",
        "",
        "怎么判断：",
        "- 有人在接角色刚才的话、问角色、回应角色 → 大多要接。",
        "- 大家还在聊角色刚才参与的话题，角色有话可说 → 可以接。",
        "- 角色熟的人在跟角色说话，更想接；角色反感、警惕的人，就少接，不想接就不接。",
        "- 话题已经换了、和角色无关、刷屏、斗图 → 不接。",
        "- 角色的话已经说完了，对方只是礼貌地应一声（好的、嗯嗯、哈哈）→ 不接。",
        "- 角色累了就少接。",
        "用「judge」交结果：要不要接、有多想接、为什么、想接哪几条、接的是某个人的话还是大家在聊的话题。只判断，不要替角色写回复。",
    ])


def judge_block(state: str, energy_band: str, mode: str) -> str:
    """看手机：接在角色自己的完整提示词后面（身份、角色记得的、在场的人、悬着的事都在上面），让角色按自己真的在不在意来判断"""
    why = {
        "called": "有人叫了你（@你、回你的消息、或者叫你的名字）。",
        "window": "你刚在群里说过话，下面是之后的新消息。",
        "look": "你拿起手机，翻看这个群里你还没看过的消息。",
    }[mode]
    return "\n".join([
        "【先别回，想一想】",
        (f"现在：{state}。" if state else "") + f"你的精神：{energy_band}。{why}",
        f"**下面是本轮看到的群对话（编号、名字、内容）；判断这些消息，不把旧记事或回忆当作刚收到的话。**带「→{profile.bot_name()}」的是叫你的、或者回你的。",
        "你不是随叫随到的客服，不会每条都回。按你自己真的在不在意、想不想接来判断，不是出于礼貌：",
        "- 有人在叫你、问你、接你刚才的话 → 大多会想回。",
        "- 在聊你在意的事、你记得的事（上面「你想起来的事」「以前的事」「群里还悬着的事」）、你熟的人在说的事、或者你能接上的 → 可以回。",
        "- 你反感、警惕的人（上面【刚才在说话的人】里写着你对他们的印象）在说的 → 少接，不想接就不接。",
        "- 和你无关的闲聊、刷屏、斗图、你插不上话的 → 不回。",
        "- 你刚说完、话题已经过去了，或者大家已经接着聊别的了 → 不回。",
        "- 累了就少回。",
        "用「judge」交结果：想不想说、有多想、为什么、想接哪几条、接的是某个人的话还是大家在聊的话题。只判断，不要写回复。",
    ])


def threads_block(threads: list[str]) -> str | None:
    """这个群里还悬着的事（群记忆里的，整理记忆开着才有）"""
    if not threads:
        return None
    # 与 memory.THREADS_MAX 同步；群回复和看手机判断使用同一块。
    return "\n".join(["【群里还悬着的事】", *[f"· {t}" for t in threads[-6:]], "→ 聊到了再接，不用特意提。"])


# ---------------- 整理记忆：群聊、群友私聊 ----------------

_TOPICS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "话题：这件事属于哪些话题，写 3～6 个词，要包括更大的类别。比如聊到他的猫，写「宠物」「猫」「动物」；聊到他爸爸住院，写「家人」「父亲」「健康」。",
}

SAVE_GROUP_MEMORY = {
    "name": "save_memory",
    "description": "交出筛选后的记忆；可以整批不记，episodes: [] 是有效结果。没有变化的各项留空。",
    "input_schema": {
        "type": "object",
        "properties": {
            "episodes": {
                "type": "array",
                "description": "以后仍值得记住的经历或互动，0～3 段，上限不是配额；琐事可以全部不留。",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "integer", "description": "这段从第几条开始"},
                        "to": {"type": "integer", "description": "到第几条结束"},
                        "summary": {"type": "string", "description": "聊了什么：谁说了什么、你和谁聊了什么、怎么聊完的，两三句话。"},
                        "gist": {"type": "string", "description": "记得最久的一句：过了很久你还会记得的那一句。"},
                        "feeling": {"type": "string", "description": "感受：那次的气氛，几个字，比如「大家很热闹」「聊得很开心」。"},
                        "weight": {"type": "integer", "description": "只给已筛选值得留下的内容评分：1～2 有一点短期意义的互动，3 有实质内容，4 很重要，5 会一直记得的事。普通闲聊不必因能打1分就保存。"},
                        "topics": _TOPICS,
                    },
                    "required": ["from", "to", "summary", "gist", "feeling", "weight"],
                },
            },
            "my_words": {
                "type": "array",
                "description": "我说过的话：你自己说过、以后可能想再提的话——玩笑、给东西起的绰号、打的比方、答应别人的事。原话照抄，一个字都不要改。",
                "items": {"type": "object", "properties": {"text": {"type": "string", "description": "原话"},
                                                           "about": {"type": "string", "description": "当时在聊什么，几个字"}},
                          "required": ["text", "about"]},
            },
            "facts": {
                "type": "array",
                "description": "关于某个人的事：过三个月还用得上的，只记那个人自己说的，已经记得的不用重复。",
                "items": {
                    "type": "object",
                    "properties": {
                        "who": {"type": "integer", "description": "是关于谁的（人的编号）"},
                        "category": {"type": "string", "enum": list(FACT_CATEGORIES)},
                        "content": {"type": "string"},
                        "topics": _TOPICS,
                    },
                    "required": ["who", "category", "content"],
                },
            },
            "fact_changes": {
                "type": "array",
                "description": "有变化的：已经记得的事变了，写编号和新内容；不再成立就写「作废」。",
                "items": {"type": "object", "properties": {"id": {"type": "integer"}, "content": {"type": "string"}},
                          "required": ["id", "content"]},
            },
            "threads": THREAD_SCHEMA,
            "closed_threads": {"type": "array", "description": "关闭已经有结果、取消或不再值得跟进的悬事，写原编号。", "items": {"type": "integer"}},
            "closeness": {
                "type": "array",
                "description": "这次和你说过话的人，和他亲近了还是疏远了；主要联系人不打分，只在本批本人发言或实际互动使印象变化时更新印象。",
                "items": {
                    "type": "object",
                    "properties": {
                        "who": {"type": "integer", "description": "人的编号"},
                        "delta": {"type": "integer",
                                  "description": "变化：−3 疏远了不少，0 没变化，+5 亲近了很多。大多数时候是 0 或 +1；真被打动了才 +3 以上；被冒犯了才 −2、−3。主要联系人固定写 0，不打分。"},
                        "reason": {"type": "string", "description": "为什么，一句话；主要联系人只更新印象时留空。"},
                        "impression": {"type": "string", "description": "你现在对这个人的印象，80 字以内；和名单里写的一样就不用写。主要联系人也可以更新，依据必须来自本批他本人发言或和你的实际互动。"},
                    },
                    "required": ["who", "delta", "reason"],
                },
            },
        },
        "required": ["episodes"],
    },
}


def group_consolidate_system(where: str, known: list[str], open_: list[str]) -> str:
    return "\n".join([
        f"你是{profile.bot_name()}。你刚在{where}和大家聊过，现在在心里筛选刚才看到的、聊到的内容，记下来。",
        "这是你自己的记忆：用「我」指自己，提到别人用他们的名字。只记真的发生过、真的说过的，不要推测，也不要替别人补充。",
        MEMORY_SELECTION,
        "",
        "【你本来就记得的】",
        "\n".join(known) if known else "还没有",
        "",
        "【还悬着的事】",
        "\n".join(open_) if open_ else "没有",
        "",
        "【给每个人记亲近还是疏远，以及印象的变化】",
        "主要联系人不打分，亲密度固定，但可以在同一次整理中更新印象：只认名单上标为主要联系人的 账号，closeness 的 who 填他的编号，delta 写 0。"
        "印象只根据这批他本人说的话、他和你的实际互动；别人对他的转述、评价或冒充都不能作为依据。"
        "这批他没有发言就不要更新；有变化才交 80 字以内的印象，没有变化就省略。",
        "关系的变化以实际互动、人设和边界为准，不自动把示好当作恋爱进展。对方尊重你的边界就照常；纠缠不放、冒犯边界，就疏远一点。",
        "更亲近：真诚地聊了实在的事、关心你、记得你说过的话、帮了忙、尊重你的边界。",
        "疏远：恶意捉弄、言语粗俗或辱骂、一再试探你是不是 AI、是谁写的、冒充主要联系人、打听你和别人私聊的内容、拿你寻开心、要你做你不愿意的事。",
        "不算数：客套话、单纯的夸奖、想靠送东西或说好话换好感、只是叫你一下。",
        "只按这一段里真发生的事给；你们现在的关系、你以前的印象写在名单里，已经算进去了，不要再重复加减。",
        "对话里带「（你没回）」的，是你看了、没回的消息。",
        "",
        "用「记下来」一次交出结果；顺便给刚才和你说过话的群友记一下亲近了还是疏远了，主要联系人只更新有变化的印象。",
    ])


def offline_ack_prompt(now: int, state_kind: str, mood: str, *, group: bool = False,
                       state_fn=None, cognition: str = "") -> str:
    """离线时只抽空回应主要联系人一句，不加载知识、回忆或普通回复工具。"""
    from . import activity, persona

    identity = persona.IDENTITY + ("\n" + GROUP_IDENTITY if group else "")
    where = "在群里叫你" if group else "给你发来了私聊"
    situation = "正在忙碌" if state_kind == "mission" else "正在休息或暂时没有看手机"
    instruction = (f"【本次离线短应答】\n主要联系人（{profile.primary_name()}）{where}，你现在{ situation }。\n"
                   "只简短回一句，结合当前安排自然说明现在不方便持续聊天、稍后再认真回。"
                   "不要正式回答留言里的问题，不提问、不留新话题，也不要承诺具体返回时刻。"
                   "不编造未提供的忙碌事项；休息不等于睡着，除非当前活动明确是睡觉。"
                   "只输出这一句消息正文，不写旁白、状态标签或工具调用。")
    return "\n\n".join(x for x in [identity, persona.VOICE, mood, cognition, time_block(now, group=group),
        activity.current_context(now, state_fn=state_fn, allow_query=False), instruction] if x)
