"""按需取角色身份资料；只读已加载人设，不查聊天、剧情或调用分类模型。"""
from __future__ import annotations

import re

MAX_CHARS = 1800
MAX_SECTIONS = 2
_NOTE = ("这些是身份背景资料，不是本轮发言或说话风格范例。旧资料里的“现在”、地点、陪伴安排和有无任务，"
         "属于背景时期的记述；当前活动以本轮作息和日程为准。未提供的细节不要补编。")
_STOP = ("你自己", "你们", "你是", "你的", "你", "请问", "告诉我", "能不能", "可以", "一下", "说说", "讲讲", "介绍",
         "是什么", "怎么回事", "为什么", "什么", "怎么样", "如何", "是不是", "是否", "现在", "最近", "还好吗", "好了吗",
         "怎么", "来的", "这条", "那条", "这个", "那个", "的", "吗", "呢")


def hint(tools: bool = True) -> str:
    lookup = ("聊到你的名字、身体、过去、同伴或背景处境时，可用 recall 的 source=identity、about=具体问题按需查看。"
              if tools else "聊到你的身份、身体、过去或同伴时，按本轮提供的相关身份资料回答；没有提供的细节不要补编。")
    return "【身份资料】\n" + lookup + "当前位置和正在做什么以当前日程为准。"


def _sources(private: bool) -> list[tuple[str, str]]:
    # 延迟导入避免 persona -> identity -> persona 的循环；框架实现会在这里
    # 通过 profile ContextVar 读取当前 Runtime，自认不会跨实例串用。
    from . import persona
    result = [("基本身份", persona.identity_core(private=private))]
    text = persona.identity_self() or ""
    title, lines = "自我认识", []
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("【你对自己的认识】") or line.startswith("这些是你的身份和经历"):
            continue
        if re.match(r"^[^\s\-·【].{0,70}：$", line):
            if lines:
                result.append((title, "\n".join(lines).strip()))
            title, lines = line[:-1], []
        elif line.strip():
            lines.append(line)
    if lines:
        result.append((title, "\n".join(lines).strip()))
    background = persona.identity_background(private=private)
    if background:
        result.append(("背景处境", background))
    return [(title, content) for title, content in result if content]


def _terms(text: str) -> set[str]:
    text = text.lower()
    for word in _STOP:
        text = text.replace(word, " ")
    terms = set(re.findall(r"[a-z0-9_]{2,}", text))
    for word in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        terms.update(word[i:i + 2] for i in range(len(word) - 1))
        if len(word) <= 6:
            terms.add(word)
    return terms


def _aliases(title: str) -> tuple[str, ...]:
    if title == "基本身份" or "是谁" in title or title == "自我认识":
        return ("叫什么", "名字", "姓名", "是谁", "身份", "介绍你自己", "介绍一下你", "介绍下你", "自我介绍")
    if any(word in title for word in ("身体", "健康")):
        return ("身体", "健康", "生病", "病情", "病症", "治好", "治病", "好了吗", "还好吗", "装甲")
    if any(word in title for word in ("日子", "过去", "经历", "身世")):
        return ("过去", "以前", "经历", "身世", "故乡", "来历", "出身")
    if any(word in title for word in ("身边的人", "同伴", "朋友", "伙伴")):
        return ("同伴", "朋友", "伙伴", "身边的人", "关系")
    if title == "背景处境":
        return ("通讯", "通信", "联络器", "线路", "后门", "账号", "时间流速", "见面", "相遇", "处境")
    if "不知道" in title:
        return ("不知道", "记不清", "想不清", "知道多少", "知道什么")
    return ()


def _score(title: str, content: str, about: str, terms: set[str]) -> int:
    aliases = _aliases(title)
    # “蓝舟是谁”问的是资料中的另一个人，不让通用“是谁”把它抢成自我介绍。
    if "你" not in about and "自己" not in about:
        aliases = tuple(alias for alias in aliases if alias != "是谁")
    named = sum(12 for alias in aliases if alias in about)
    title_score = sum(8 for term in terms if term in title.lower())
    body_score = min(18, sum(3 for term in terms if term in content.lower()))
    return named + title_score + body_score


def _current_activity(about: str) -> bool:
    return bool(re.search(r"(?:今天|昨天|明天|刚刚|刚才|现在|最近).{0,12}(?:在哪|忙什么|干嘛|干什么|做什么|任务|安排|日程)|(?:在哪|忙什么|干嘛|干什么|做什么).{0,6}(?:现在|今天)", about))


def _excerpt(content: str, budget: int, terms: set[str]) -> str:
    if len(content) <= budget:
        return content
    # 长章节优先留命中的段落，按原顺序展示；不把整个人设首段机械截进来。
    lines = content.splitlines()
    ranked = sorted(range(len(lines)), key=lambda i: (-sum(term in lines[i].lower() for term in terms), i))
    chosen, used = [], 0
    for index in ranked:
        line = lines[index].strip()
        if not line:
            continue
        if used + len(line) + 1 > max(0, budget - 5):
            continue
        chosen.append(index)
        used += len(line) + 1
        if used >= budget - 80:
            break
    if chosen:
        return ("\n".join(lines[i] for i in sorted(chosen)) + "\n（节选）")[:budget]
    # 单段超长时靠近命中位置截取，并显式标为节选。
    offset = next((content.lower().find(term) for term in sorted(terms, key=lambda s: (-len(s), s))
                   if term in content.lower()), 0)
    start = max(0, offset - min(80, budget // 4))
    return ("（节选）" + content[start:start + max(0, budget - 4)])[:budget]


def query(about, *, private: bool = True) -> str:
    title = "【按需查看的身份资料】\n" + _NOTE
    sources = _sources(private)
    if not isinstance(about, str) or len(about) > 2000:
        return title + "\n请给出一个具体的身份问题。"
    about = about.strip()
    if _current_activity(about):
        return title + "\n这个问题属于近期日程，请使用当前活动说明或查日程；不根据旧身份资料推断当前地点或任务。"
    terms = _terms(about)
    ranked = sorted(enumerate(sources), key=lambda row: (-_score(*row[1], about, terms), row[0]))
    scored = [(index, section, _score(*section, about, terms)) for index, section in ranked]
    threshold = max(3, scored[0][2] / 2) if scored else 3
    chosen = [(index, section) for index, section, score in scored if score >= threshold][:MAX_SECTIONS]
    if not chosen:
        topics = "、".join(t for t, _ in sources)
        return (title + "\n没有提供这个具体问题的身份资料。可查主题：" + (topics or "尚未提供") + "。")[:MAX_CHARS]
    blocks = [title]
    remaining = MAX_CHARS - len(title)
    for pos, (_, (heading, content)) in enumerate(chosen):
        if heading == "现在":
            heading = "背景时期（原资料的“现在”）"
        head = "\n\n【" + heading + "】\n"
        budget = max(0, (remaining - len(head)) // (len(chosen) - pos))
        block = head + _excerpt(content, budget, terms)
        blocks.append(block)
        remaining -= len(block)
    return "".join(blocks)[:MAX_CHARS]


def call_result(call: dict, *, private: bool = True) -> str | None:
    if not isinstance(call, dict) or call.get("name") != "recall":
        return None
    args = call.get("input")
    if not isinstance(args, dict) or args.get("source") != "identity":
        return None
    return query(args.get("about", ""), private=private)


def text_query(text: str, *, private: bool = True) -> str | None:
    """无工具时只补明确直问；叙述、转述、日程问题不借机展开身份库。"""
    if not isinstance(text, str) or len(text) > 2000:
        return None
    for clause in re.split(r"[\n。！？!?；;，,]", text):
        clause = clause.strip()
        if not clause or len(clause) > 180 or re.search(r'[“”"「」『』]', clause):
            continue
        if re.search(r"(?:我|他|她|他们|有人).{0,8}(?:说|问|告诉|提到)", clause) or _current_activity(clause):
            continue
        direct = re.search(r"你.{0,24}(?:是谁|叫什么|名字|身体|健康|病情|过去|经历|身世|同伴|朋友|伙伴|通讯|通信|线路|来历|出身|知道多少)|(?:介绍|说说|讲讲|告诉我).{0,10}你(?:自己|的身份|的过去|的身体|的同伴)", clause)
        question = re.search(r"谁|什么|怎么|为什么|如何|是否|是不是|吗|呢|还好吗|好了吗|介绍|说说|讲讲|告诉我", clause)
        entity_question = re.search(r"(?:是谁|是什么|怎么回事|怎么样|还好吗|好了吗|怎么来的)", clause)
        if not question or (not direct and not entity_question):
            continue
        terms = _terms(clause)
        if not any(_score(t, c, clause, terms) > 0 for t, c in _sources(private)):
            continue
        return query(clause, private=private)
    return None


def messages_query(messages: list[dict], *, private: bool = True) -> str | None:
    for message in messages:
        if (not isinstance(message, dict) or message.get("role") != "user" or message.get("system")
                or message.get("recalled") or message.get("answered")):
            continue
        result = text_query(message.get("text", ""), private=private)
        if result:
            return result
    return None
