"""只在被问到时展开近期日程；作息不是聊天记忆，也不是具体任务经历。"""

from __future__ import annotations

from datetime import timedelta
import re

from . import schedule
from .jsutil import MIN, local_dt


_DAYS = {"today", "yesterday", "before_yesterday", "future"}
_PERIODS = {"凌晨": (0, 5 * 60), "早上": (5 * 60, 9 * 60), "上午": (6 * 60, 12 * 60),
            "中午": (11 * 60, 14 * 60), "下午": (12 * 60, 18 * 60),
            "傍晚": (17 * 60, 20 * 60), "晚上": (18 * 60, 24 * 60), "夜里": (0, 6 * 60)}
_NAMES = {"sleep": "休息时间", "duty": "日常事务", "downtime": "空闲时间", "night": "夜间空闲"}
_PAST = "前天及更早的零碎日程已经记不太清，可以只概括为生活中的琐事，不补编精确时段、地点或具体经历。"
_FUTURE = "明天及以后的具体忙碌事项尚未告知；其余只能谈普通生活小事，不编具体事项、地点或时间表。"
_KNOWN = "这只说明日程能查到的范围，不否定聊天记忆中已经明确的经历、约定或承诺。"


def _describe(state) -> str:
    return schedule.describe_activity(state)


def _summary(state) -> str:
    if state.activity == "mission":
        return "忙碌时间"
    return _NAMES.get(state.activity, "处理日常事务")


def _bounds(now: int, yesterday: bool = False) -> tuple[int, int]:
    # 日期仅用于内部定位，不给角色另造年月日。按本机日历处理跨月/跨年/DST。
    day = local_dt(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if yesterday:
        day -= timedelta(days=1)
    return int(day.timestamp() * 1000), int((day + timedelta(days=1)).timestamp() * 1000)


def _segments(now: int, day: str, state_fn=None) -> list[dict]:
    fn = state_fn or schedule.state_at
    start, end = _bounds(now, day == "yesterday")
    rows = []
    # 不缓存：框架宿主可在不同 ContextVar 角色配置下调用同一个函数对象。
    for point in range(start, end, MIN):
        state = fn(point)
        detail, summary = _describe(state), _summary(state)
        key = (state.activity, state.location, getattr(state, "destination", None), detail)
        if rows and rows[-1]["key"] == key:
            rows[-1]["end"] = min(point + MIN, end)
        else:
            rows.append({"start": point, "end": min(point + MIN, end), "key": key,
                         "detail": detail, "summary": summary, "day_end": end})
    return rows


def _span(row: dict) -> str:
    end = "24:00" if row["end"] == row["day_end"] else local_dt(row["end"]).strftime("%H:%M")
    return f"{local_dt(row['start']):%H:%M}–{end}"


def current_context(now: int, *, state_fn=None, allow_query: bool = False) -> str:
    """只给当前段；来回切换在线状态本身不把相同活动拆开。"""
    row = next(r for r in _segments(now, "today", state_fn) if r["start"] <= now < r["end"])
    lines = ["【你这边的安排】", "时段按通讯器时钟理解；只用今天、昨天等说法，不另换日期。",
             f"今天 {_span(row)} — {row['summary']}（现在所在的时段）", row["detail"],
             "这是粗略安排，不代表已经发生了具体的小事；没有给出的细节不要编造。"]
    if allow_query:
        lines.append("被问到其他时段时，用 recall 的 source=schedule 查询；今天其他时段与昨天按需查看，平时不展开整天安排。")
    return "\n".join(lines)


def _clock(value: str) -> int | None:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value)
    return int(match[1]) * 60 + int(match[2]) if match else None


def _period_range(period: str) -> tuple[int, int] | None:
    if period in _PERIODS:
        return _PERIODS[period]
    if period in ("", "全天", "一天", "整天"):
        return 0, 1440
    point = _clock(period)
    if point is not None:
        return point, point + 1
    parts = re.split(r"\s*[-–—至]\s*", period)
    if len(parts) == 2:
        begin = _clock(parts[0])
        end = 1440 if parts[1] == "24:00" else _clock(parts[1])
        if begin is not None and end is not None and begin < end:
            return begin, end
    return None


def _recent_context(now: int, state_fn=None, *, reference_ts: int | None = None) -> str:
    anchor = now if reference_ts is None else reference_ts
    cutoff = anchor - 30 * MIN
    today_start, _ = _bounds(now)
    yesterday_start, _ = _bounds(now, True)
    if anchor <= yesterday_start:
        return "【日程查询】\n" + _PAST + "\n" + _KNOWN
    days = [day for day, start, end in (("yesterday", yesterday_start, today_start),
                                       ("today", today_start, _bounds(now)[1]))
            if start < anchor and end > cutoff]
    lines = ["【按需查看：刚刚的安排】", "只看那条来话发出时往前的半小时；下文今天／昨天相对于当前回复时刻，不是具体事情的逐项经历。"]
    if cutoff < yesterday_start:
        lines.append("这半小时更早的部分已在前天，只记得些生活琐事，不再列精确日程。")
    for day in days:
        label = "昨天" if day == "yesterday" else "今天"
        for row in _segments(now, day, state_fn):
            if row["end"] <= cutoff or row["start"] >= anchor:
                continue
            shown = {**row, "start": max(cutoff, row["start"]), "end": min(anchor, row["end"])}
            phase = "现在仍在这一段" if row["end"] > now else "来话时刚过去的时段"
            lines.append(f"{label} {_span(shown)} — {row['summary']}（{phase}）\n该段背景：{row['detail']}")
    lines.append("只回答这段时间的大致活动，未提供的具体经历不要编造。" + _KNOWN)
    return "\n".join(lines)


def query(now: int, day: str = "today", period: str = "", *, state_fn=None) -> str:
    """日程来源仅允许今天、昨天；越界直接说明范围，不转入知识库。"""
    if not isinstance(day, str) or day not in _DAYS or not isinstance(period, str) or len(period) > 40:
        return "【日程查询】参数无效；只查今天或昨天的安排，不能用知识库补成实际日程。"
    if day in ("before_yesterday", "future"):
        return "【日程查询】\n" + (_PAST if day == "before_yesterday" else _FUTURE) + "\n" + _KNOWN
    if period.strip() in ("刚刚", "刚才", "recent"):
        if day != "today":
            return "【日程查询】“刚刚”只指现在往前半小时；要查昨天，请指定昨天的时段。"
        return _recent_context(now, state_fn)
    interval = _period_range(period.strip())
    if interval is None:
        return "【日程查询】没认出要查的时段。可用刚刚、上午、下午、晚上、HH:mm 或 HH:mm-HH:mm；不要据此编造安排。"
    day_label = "昨天" if day == "yesterday" else "今天"
    lines = [f"【按需查看：{day_label}的安排】", "时段按通讯器时钟理解；这些是粗略活动，不是具体任务经过。"]
    for row in _segments(now, day, state_fn):
        dt = local_dt(row["start"])
        begin = dt.hour * 60 + dt.minute
        finish_dt = local_dt(row["end"])
        finish = 1440 if row["end"] == row["day_end"] else finish_dt.hour * 60 + finish_dt.minute
        if finish <= interval[0] or begin >= interval[1]:
            continue
        phase = "现在所在的时段" if day == "today" and row["start"] <= now < row["end"] else (
            "之后的安排，尚未发生" if day == "today" and row["start"] > now else "已过去的时段")
        lines.append(f"{day_label} {_span(row)} — {row['summary']}（{phase}）\n该段背景：{row['detail']}")
    lines.append("只按被问到的部分回答；未提供的具体经历不要编造。" + _KNOWN)
    return "\n".join(lines)


def call_result(call: dict, now: int, *, state_fn=None) -> str | None:
    """None 表示保持旧回忆路径；日程分支的参数失败也不能回落到知识库。"""
    if not isinstance(call, dict) or call.get("name") != "recall":
        return None
    args = call.get("input")
    if not isinstance(args, dict) or args.get("source") != "schedule":
        return None
    return query(now, args.get("day", "today"), args.get("period", ""), state_fn=state_fn)


def text_query(text: str, now: int, *, state_fn=None, reference_ts: int | None = None) -> str | None:
    """无工具模型的保守直问补充；普通叙述、聊天旧事不自动当成日程查询。"""
    if not isinstance(text, str) or len(text) > 2000:
        return None
    reference = now
    if reference_ts is not None:
        if isinstance(reference_ts, bool) or not isinstance(reference_ts, (int, float)):
            return None
        try:
            reference = int(reference_ts)
            local_dt(reference)
        except (OverflowError, OSError, ValueError):
            return None
        if reference > now:
            return None
    relative_age = (local_dt(reference).date() - local_dt(now).date()).days
    clauses = re.split(r"[\n。！？!?；;，,]", text)
    for clause in clauses:
        clause = clause.strip()
        if not clause or len(clause) > 180 or re.search(r"[“”\"「」『』]", clause):
            continue
        # 必须直接问“你”；不把“我昨天问你在干什么”或别人转述误作查询。
        if re.search(r"(?:我|他|她|他们|有人).{0,8}(?:说|问|告诉|提到)", clause):
            continue
        if re.search(r"约好|约定|承诺|说好|还记得|聊了|聊过|我们|咱们|回忆", clause):
            continue
        action = re.search(r"你.{0,25}(?:做(?:了)?(?:些)?什么|干(?:了)?(?:些)?什么|干嘛|忙(?:了)?(?:些)?什么|有什么安排|有啥安排|怎么安排|(?:日程|行程|安排)(?:是什么|怎么样|如何)|在哪(?:里|儿)?|去了哪|(?:几点|什么时候).{0,8}(?:休息|睡|起床|忙|出发|回来|结束))", clause)
        requested_table = re.search(r"(?:说说|告诉我|看看|讲讲).{0,10}你.{0,12}(?:日程|行程|安排)|你.{0,15}(?:日程|行程|安排).{0,6}(?:说说|看看|讲讲)", clause)
        if not action and not requested_table:
            continue
        day = ("before_yesterday" if re.search(r"前天|大前天|上周|上星期|上个月|去年", clause) else
               "future" if re.search(r"明天|后天|下周|下星期|下个月|以后", clause) else
               "yesterday" if "昨天" in clause or "昨晚" in clause else "today")
        if not re.search(r"今天|昨天|昨晚|前天|明天|后天|上周|上星期|上个月|去年|下周|下星期|下个月|以后|刚刚|刚才|现在|上午|下午|晚上|早上|中午|凌晨|傍晚|\d{1,2}:\d{2}", clause):
            continue
        period = next((p for p in _PERIODS if p in clause), "晚上" if "昨晚" in clause else "")
        clock = re.search(r"(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*[-–—至]\s*(?:[01]?\d|2[0-4]):[0-5]\d)?", clause)
        if clock:
            period = clock[0]
        if re.search(r"刚刚|刚才", clause) and day == "today":
            return _recent_context(now, state_fn, reference_ts=reference)
        if ("现在" in clause or re.search(r"你.{0,12}在哪(?:里|儿)?", clause)) and day == "today" and not period:
            period = local_dt(reference).strftime("%H:%M")
        delta = (0 if day == "today" else -1 if day == "yesterday" else
                 1 if day == "future" and "明天" in clause else
                 2 if day == "future" and "后天" in clause else None)
        if delta is not None:
            target_age = relative_age + delta
            day = "today" if target_age == 0 else "yesterday" if target_age == -1 else "before_yesterday" if target_age < -1 else "future"
        result = query(now, day, period, state_fn=state_fn)
        if relative_age:
            result = "【按原来话时间理解的日程问题】\n问题中的相对时间按它发出时理解；下面的今天／昨天相对于当前回复时刻。\n" + result
        return result
    return None


def messages_query(messages: list[dict], now: int, *, state_fn=None) -> str | None:
    """只查调用方明确传入的本轮目标，保留原发言时间，最多两份不同结果。"""
    results = []
    for message in messages:
        if (not isinstance(message, dict) or message.get("role") != "user" or message.get("system")
                or message.get("recalled") or message.get("answered")):
            continue
        result = text_query(message.get("text", ""), now, state_fn=state_fn, reference_ts=message.get("ts"))
        if result and result not in results:
            results.append(result)
            if len(results) == 2:
                break
    return "\n\n".join(results) if results else None
