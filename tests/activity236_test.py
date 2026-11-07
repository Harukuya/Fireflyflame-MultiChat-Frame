"""日程边界专项；只用假状态和固定时间，不读宿主配置、不调用模型。"""
from datetime import datetime
import importlib
from pathlib import Path
import re
import sys
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
PACKAGE = "multichat" if (ROOT / "multichat").is_dir() else "firefly"
A = importlib.import_module(PACKAGE + ".activity")
S = importlib.import_module(PACKAGE + ".schedule")
T = importlib.import_module(PACKAGE + ".tools")
checks = []


def check(ok, label):
    checks.append(bool(ok))
    print(("OK " if ok else "FAIL ") + label)


def stamp(value):
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M").timestamp() * 1000)


NOW = stamp("2027-01-01 10:30")


def fake(t):
    dt = A.local_dt(t)
    act = "sleep" if dt.hour < 6 else "duty" if dt.hour < 12 else "downtime" if dt.hour < 19 else "night"
    if dt.day == 31:
        act = "mission"
    return SimpleNamespace(online=bool(dt.hour % 2), location="mission" if act == "mission" else "base",
                           activity=act, destination="测试地点，天气很冷" if act == "mission" else None,
                           her_hour=dt.hour + 2, offset_hours=2)


# 用稳定的假描述测试分段，与真实角色文本无关；在线每小时翻转不改变活动。
original_describe = S.describe_activity
S.describe_activity = lambda s: {"sleep": "你在休息", "duty": "你处理日常事务", "downtime": "你有空", "night": "你夜里醒着", "mission": "你在测试地点忙碌，天气很冷"}[s.activity]
current = A.current_context(NOW, state_fn=fake, allow_query=True)
check("【你这边的安排】" in current and "今天 06:00–12:00" in current, "当前段范围与通讯器时间一致")
check("现在所在的时段" in current and "你处理日常事务" in current, "当前段有标记与详细背景")
check("00:00–06:00" not in current and "测试地点" not in current, "默认不带今日其他段或昨日内容")
check(not re.search(r"\d{4}[-年/]", current), "不为角色新增年月日")
check("source=schedule" in current and "source=schedule" not in A.current_context(NOW, state_fn=fake), "只在允许查询时提示工具")

today = A.query(NOW, state_fn=fake)
rows = [line for line in today.splitlines() if line.startswith("今天 ")]
check(len(rows) == 4, "活动分段不随在线每小时翻转而碎裂")
check("之后的安排，尚未发生" in today and "已过去的时段" in today, "未来时段不冒充已经发生")
check(sum("现在所在的时段" in line for line in rows) == 1, "今日仅一个当前段")
yesterday = A.query(NOW, "yesterday", state_fn=fake)
check("昨天 00:00–24:00" in yesterday and "测试地点" in yesterday, "跨年昨天正确取上一自然日")
check("现在所在的时段" not in yesterday, "昨天没有当前段")
check("测试地点" not in today, "今日查询不混入昨日地点")
at_midnight = A.current_context(stamp("2027-01-01 00:00"), state_fn=fake)
check("今天 00:00–06:00" in at_midnight, "午夜整点属于新一天")
last_minute = A.current_context(stamp("2026-12-31 23:59"), state_fn=fake)
check("今天 00:00–24:00" in last_minute, "午夜前不提前换日")

morning = A.query(NOW, "today", "上午", state_fn=fake)
check("06:00–12:00" in morning and "12:00–19:00" not in morning, "按上午筛选不带下午")
point = A.query(NOW, "today", "12:00", state_fn=fake)
check("12:00–19:00" in point and "06:00–12:00" not in point, "时段右端不含、下一段左端包含")
interval = A.query(NOW, "today", "05:30-06:30", state_fn=fake)
check("00:00–06:00" in interval and "06:00–12:00" in interval and "12:00–19:00" not in interval, "查询区间取相交活动")
check("19:00–24:00" in A.query(NOW, "today", "19:00-24:00", state_fn=fake), "支持24点作为结束边界")
check("没认出" in A.query(NOW, "today", "25:99", state_fn=fake), "非法时钟不猜测查询")
check("没认出" in A.query(NOW, "today", "22:00-02:00", state_fn=fake), "反向时段不隐式跨日")
check("参数无效" in A.query(NOW, [], state_fn=fake), "非字符串日期不崩溃")
check("参数无效" in A.query(NOW, "today", "x" * 41, state_fn=fake), "时段参数有长度边界")

def forbidden(t):
    raise AssertionError("越界日期不应枚举作息")

past = A.query(NOW, "before_yesterday", state_fn=forbidden)
future = A.query(NOW, "future", state_fn=forbidden)
check("记不太清" in past and "聊天记忆" in past and "测试地点" not in past, "早期日程不精确回放、不否定记忆")
check("尚未告知" in future if PACKAGE == "multichat" else "剧本还没告知" in future, "未来安排按角色边界处理")
check("约定" in future and "测试地点" not in future, "未来未知不抹掉既有承诺")
check(A.call_result({"name": "recall", "input": {"about": "旧事"}}, NOW, state_fn=forbidden) is None, "旧回忆参数走原路径")
check(A.call_result({"name": "think", "input": {"source": "schedule"}}, NOW, state_fn=forbidden) is None, "非回忆工具不误分流")
check(A.call_result({"name": "recall", "input": {"source": "memory", "day": "yesterday"}}, NOW, state_fn=forbidden) is None, "显式记忆来源仍走原路径")
check("昨天 00:00–24:00" in A.call_result({"name": "recall", "input": {"source": "schedule", "day": "yesterday"}}, NOW, state_fn=fake), "显式日程来源得到日程")
check("参数无效" in A.call_result({"name": "recall", "input": {"source": "schedule", "day": "2099-01-01"}}, NOW, state_fn=forbidden), "日程参数错误也不回落知识库")

check("昨天" in A.text_query("你昨天在干嘛？", NOW, state_fn=fake), "无工具支持明确问昨天活动")
check("12:00–19:00" in A.text_query("你今天下午在做什么？", NOW, state_fn=fake), "无工具识别时段")
check("记不太清" in A.text_query("你前天做了什么？", NOW, state_fn=forbidden), "无工具前天同样受边界约束")
check(A.text_query("你明天有什么安排？", NOW, state_fn=forbidden) == future, "无工具未来不调用作息计算")
check(A.text_query("我昨天做了很多事。", NOW, state_fn=forbidden) is None, "自述不触发日程")
check(A.text_query("我昨天问你在做什么", NOW, state_fn=forbidden) is None, "转述旧问句不触发")
check(A.text_query("你昨天推荐的电影是什么？", NOW, state_fn=forbidden) is None, "普通聊天旧事不替换为日程")
check(A.text_query("他说‘你昨天干嘛去了’", NOW, state_fn=forbidden) is None, "引述他人问题不触发")
check(A.text_query("你现在在做什么？", NOW, state_fn=fake).count("该段背景") == 1, "只问现在仅展开当前段")
check(A.text_query("x" * 2001, NOW, state_fn=forbidden) is None, "直问探测输入有界")
check(A.text_query("你昨天的行程有点紧。", NOW, state_fn=forbidden) is None, "行程陈述不当作问题")
check(A.text_query("你昨天给我列的日程很清楚", NOW, state_fn=forbidden) is None, "普通评价不误查日程")
check(A.text_query("我们昨天约好明天干嘛？", NOW, state_fn=forbidden) is None, "已聊约定保留回忆路径")
check(A.text_query("你还记得昨天我们聊了什么？", NOW, state_fn=forbidden) is None, "询问聊天记忆不抢成日程")
check("昨天" in A.text_query("你昨天几点睡的？", NOW, state_fn=fake), "支持询问休息时间")
check("昨天" in A.text_query("你昨天做了些什么？", NOW, state_fn=fake), "支持口语做了些什么")
check("昨天" in A.text_query("给我看看你昨天的日程", NOW, state_fn=fake), "支持明确请求查看日程")
check(A.text_query("你今天在哪？", NOW, state_fn=fake).count("该段背景") == 1, "问今天在哪默认仅当前段")
recent = A.query(stamp("2027-01-01 06:10"), "today", "刚刚", state_fn=fake)
check("今天 05:40–06:00" in recent and "今天 06:00–06:10" in recent and "12:00" not in recent, "刚刚只看半小时并包含刚结束的上一段")
cross = A.query(stamp("2027-01-01 00:10"), "today", "刚才", state_fn=fake)
check("昨天 23:40–24:00" in cross and "今天 00:00–00:10" in cross and "2027" not in cross, "刚刚跨午夜带昨日末段、不带角色日期")
check("00:00–24:00" not in cross and cross.count("该段背景") == 2, "刚刚不会展开昨日整表")
check("刚刚的安排" in A.text_query("你刚刚干了什么？", NOW, state_fn=fake), "无工具刚刚采用半小时查询")
check(A.query(NOW, "future", "刚刚", state_fn=forbidden) == future, "未来边界优先于刚刚参数")
check("请指定昨天的时段" in A.query(NOW, "yesterday", "刚刚", state_fn=forbidden), "不猜测昨天刚刚对应什么时刻")
delayed_now, message_at = stamp("2027-01-01 00:01"), stamp("2026-12-31 23:59")
check("记不太清" in A.text_query("你昨天上午做了什么？", delayed_now, reference_ts=message_at, state_fn=forbidden), "跨年留言里的昨天按原发言日映射前天")
old_today = A.text_query("你今天上午做了什么？", delayed_now, reference_ts=message_at, state_fn=fake)
check("昨天 00:00–24:00" in old_today and "今天 06:00" not in old_today, "旧留言今天映射当前昨天")
old_tomorrow = A.text_query("你明天上午有什么安排？", delayed_now, reference_ts=message_at, state_fn=fake)
check("今天 06:00–12:00" in old_tomorrow and "之后的安排" in old_tomorrow, "旧留言明天映射当前今天")
old_after_tomorrow = A.text_query("你后天有什么安排？", NOW, reference_ts=stamp("2026-12-30 10:00"), state_fn=fake)
check("今天的安排" in old_after_tomorrow, "跨年后天按原日期偏移两天")
month_now = stamp("2027-03-01 00:01")
month_ref = stamp("2027-02-28 23:59")
check("记不太清" in A.text_query("你昨天在做什么？", month_now, reference_ts=month_ref, state_fn=forbidden), "跨月短月份按自然日换算")
check("原来话时间" in old_today and not re.search(r"\d{4}[-年/]", old_today), "标明按来话理解而不新增角色日期")
old_recent = A.text_query("你刚刚干了什么？", delayed_now, reference_ts=stamp("2026-12-31 23:50"), state_fn=fake)
check("昨天 23:20–23:50" in old_recent and "今天 00:00" not in old_recent, "延迟刚刚围绕原发言时刻、不是回复时刻")
same_day_recent = A.text_query("你刚才在忙什么？", NOW, reference_ts=stamp("2027-01-01 06:10"), state_fn=fake)
check("今天 05:40–06:00" in same_day_recent and "今天 06:00–06:10" in same_day_recent and "10:30" not in same_day_recent, "同日延迟刚才也保留原时间锚点")
partial_recent = A.text_query("你刚刚干了什么？", NOW, reference_ts=stamp("2026-12-31 00:10"), state_fn=fake)
check("前天" in partial_recent and "昨天 00:00–00:10" in partial_recent and "23:40–24:00" not in partial_recent, "刚刚触及前天仅保留可查部分与边界")
check("记不太清" in A.text_query("你刚刚干嘛？", NOW, reference_ts=stamp("2026-12-30 23:59"), state_fn=forbidden), "过早刚刚不回放精确日程")
check(A.text_query("你昨天做什么？", NOW, reference_ts=NOW + 1, state_fn=forbidden) is None, "未来消息时间不猜测")
check(A.text_query("你昨天做什么？", NOW, reference_ts=float("nan"), state_fn=forbidden) is None, "非法消息时间不崩溃")
check(A.text_query("你昨天做什么？", NOW, reference_ts="2027-01-01", state_fn=forbidden) is None, "字符串消息时间不隐式猜测")
ignored = [{"role": role, "text": "你昨天干嘛？", "ts": NOW, **flags} for role, flags in (
    ("her", {}), ("assistant", {}), ("user", {"system": True}), ("user", {"recalled": True}), ("user", {"answered": True}))]
check(A.messages_query(ignored, NOW, state_fn=forbidden) is None, "批量查询排除非目标角色系统撤回已答")
delayed = A.messages_query([{"role": "user", "text": "你昨天上午做什么？", "ts": message_at}], delayed_now, state_fn=forbidden)
check("记不太清" in delayed, "批量目标保留原消息时间")
first = {"role": "user", "text": "你今天上午做什么？", "ts": NOW}
second = {"role": "user", "text": "你今天下午做什么？", "ts": NOW}
third = {"role": "user", "text": "你昨天做什么？", "ts": NOW}
batch = A.messages_query([first, first, second, third], NOW, state_fn=fake)
check(batch.count("【按需查看：") == 2 and "06:00–12:00" in batch and "12:00–19:00" in batch and "昨天 00:00" not in batch, "批量目标去重并保留两条不同查询")
check(A.messages_query([{"role": "user", "text": "今天我在家", "ts": NOW}], NOW, state_fn=forbidden) is None, "批量普通自述不触发查询")

props = T.RECALL["input_schema"]["properties"]
check(set(props["source"]["enum"]) == {"memory", "schedule", "identity"}, "工具来源可显式区分")
check(set(props["day"]["enum"]) == A._DAYS and props["period"]["maxLength"] == 40, "工具日期与参数边界一致")
check(T.RECALL["input_schema"]["required"] == ["about"], "兼容旧工具调用必填项")
S.describe_activity = original_describe
check("【你这边的安排】" in A.current_context(NOW), "真实作息函数可组装当前段")
if PACKAGE == "multichat":
    P = importlib.import_module("multichat.profile")
    P.configure({"schedule_mode": "always"}, ROOT / "prompts")
    first = A.current_context(NOW)
    P.configure({"busy_start": "10:00", "busy_end": "12:00"}, ROOT / "prompts")
    second = A.current_context(NOW)
    check(first != second and "忙碌时间" in second, "同日更换配置不吃到另一角色缓存")
    check(all(word not in (ROOT / "multichat" / "activity.py").read_text(encoding="utf-8") for word in ("开拓者", "流萤", "雅利洛", "星核猎手", "剧本")), "框架日程不带角色剧情文字")

passed = sum(checks)
print(f"activity236 {passed}/{len(checks)}")
raise SystemExit(0 if passed == len(checks) else 1)
