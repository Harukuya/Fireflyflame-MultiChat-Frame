"""日程提示与按需查询的真实请求集成；三版通用，只有假模型和临时数据。

python -I -B tests/activityflow236_test.py <框架根目录>
"""
from __future__ import annotations

import copy
import importlib.util
import inspect
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location("_activityflow236_fixture", Path(__file__).with_name("no_reply233_test.py"))
F = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = F
spec.loader.exec_module(F)
E, G, NOW, MIN, HOUR = F.E, F.G, F.NOW, F.MIN, F.HOUR
Farewell = F.module("farewell")
QUERY_MARKER = "按需查询结果测试标记，不属于常驻当前活动"


async def resolved(value):
    return await value if inspect.isawaitable(value) else value


class ActivityFlowTest(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = F.NoReplyTest.asyncTearDown
    recall = F.NoReplyTest.recall
    engine = F.NoReplyTest.engine
    group = F.NoReplyTest.group
    private_message = F.NoReplyTest.private_message
    group_message = F.NoReplyTest.group_message

    async def asyncSetUp(self):
        await F.NoReplyTest.asyncSetUp(self)
        self.activity = F.module("activity")
        self.state_fn = lambda t: F.S.HerState(True, "base", "night", 22.0, 0)
        for mod in (E, G, F.S, Farewell):
            if hasattr(mod, "state_at"):
                guard = patch.object(mod, "state_at", self.state_fn)
                guard.start()
                self.addCleanup(guard.stop)
            if hasattr(mod, "now_ms"):
                guard = patch.object(mod, "now_ms", lambda: NOW)
                guard.start()
                self.addCleanup(guard.stop)

    def request(self):
        self.assertTrue(self.provider.requests, "必须真正调用假 Provider")
        return self.provider.requests[-1]

    def assert_current(self, request=None, query=False):
        request = request or self.request()
        expected = self.activity.current_context(NOW, state_fn=self.state_fn, allow_query=query)
        self.assertIn(expected, request["system"])
        self.assertEqual(request["system"].count("【你这边的安排】"), 1)
        time = F.W.time_block(NOW, group=False).splitlines()[0]
        if time in request["system"]:
            self.assertLess(request["system"].index(time), request["system"].index("【你这边的安排】"))

    def plain_model(self):
        self.provider.provider_config["modalities"] = ["text"]

    @staticmethod
    def schedule_call(day="yesterday"):
        return {"name": "recall", "input": {"about": "自己的安排", "source": "schedule", "day": day}}

    async def test_01_private_request_has_only_current_activity(self):
        e = self.engine()
        await self.private_message(e, "今天读到一本有趣的书")
        await e.run_turn()
        self.assert_current(query=True)
        self.assertEqual(len(self.provider.requests), 1)

    async def test_02_peer_private_uses_same_activity(self):
        e = self.engine(peer=True)
        await self.private_message(e, "聊聊最近读的书吧")
        await e.run_turn()
        self.assert_current(query=True)

    async def test_03_group_reply_receives_current_activity(self):
        c = self.group()
        m = await self.group_message(c, text="你也喜欢看书吗")
        await c.turn("called", [m["id"]])
        self.assert_current(query=True)

    async def test_04_no_tools_does_not_advertise_unavailable_schedule_tool(self):
        self.plain_model()
        e = self.engine()
        await self.private_message(e, "今天我读到一本有趣的书")
        await e.run_turn()
        self.assert_current(query=False)
        self.assertEqual(self.request()["tools"], [])

    async def test_05_no_tools_explicit_private_question_gets_query_material(self):
        self.plain_model()
        e = self.engine()
        await self.private_message(e, "你昨天都做了什么？")
        with patch.object(self.activity, "text_query", return_value=QUERY_MARKER) as query:
            await e.run_turn()
        self.assertIn(QUERY_MARKER, self.request()["system"])
        self.assertTrue(query.called)
        self.assertEqual(len(self.provider.requests), 1)

    async def test_06_no_tools_explicit_group_question_gets_query_material(self):
        self.plain_model()
        c = self.group()
        m = await self.group_message(c, text="你昨天都做了什么？")
        with patch.object(self.activity, "text_query", return_value=QUERY_MARKER):
            await c.turn("called", [m["id"]])
        self.assertIn(QUERY_MARKER, self.request()["system"])
        self.assertEqual(len(self.provider.requests), 1)

    async def test_07_plain_chat_does_not_inject_full_yesterday_schedule(self):
        self.plain_model()
        e = self.engine()
        yesterday = self.activity.query(NOW, "yesterday", state_fn=self.state_fn)
        await self.private_message(e, "我昨天加班很晚，今天想早点休息")
        await e.run_turn()
        self.assertNotIn(yesterday, self.request()["system"])
        self.assert_current(query=False)

    async def judge(self, mode):
        c = self.group()
        m = await self.group_message(c, text="今天这个故事挺有趣的")
        self.provider.script = [F.Response(calls=[("judge", {"reply": False, "interest": 0,
                                                               "reason": "先听听", "focus": [], "to": "话题"})])]
        await c.assess([m], 0, mode, [m])
        return c

    async def test_08_phone_judgment_has_current_but_no_schedule_tools(self):
        await self.judge("look")
        self.assert_current(query=False)
        self.assertNotIn("recall", [x["name"] for x in self.request()["tools"]])

    async def test_09_window_judgment_has_current_but_no_schedule_tools(self):
        await self.judge("window")
        self.assert_current(query=False)
        self.assertNotIn("recall", [x["name"] for x in self.request()["tools"]])

    async def test_10_catchup_has_current_without_advertising_recall(self):
        e = self.engine()
        m = e.store.new_msg("user", "刚才留言问声好", ts=NOW - MIN)
        m["undelivered"] = True
        e.history.append(m)
        await e._catchup_now()
        self.assert_current(query=False)
        self.assertEqual([x["name"] for x in self.request()["tools"]], ["no_reply"])

    async def test_11_proactive_has_current_without_advertising_recall(self):
        e = self.engine()
        e._should_reach_out = lambda now: True
        await e._maybe_reach_out(NOW)
        self.assert_current(query=False)
        self.assertNotIn("recall", [x["name"] for x in self.request()["tools"]])

    async def test_12_thought_has_current_without_advertising_recall(self):
        e = self.engine()
        e.store.thought = {"id": "thought", "topic": "稍后问候", "tier": "soon", "createdAt": NOW-MIN,
                           "dueAt": NOW-1, "waited": False}
        await e._maybe_recheck(NOW)
        self.assert_current(query=False)
        self.assertNotIn("recall", [x["name"] for x in self.request()["tools"]])

    async def test_13_final_farewell_request_still_has_current_and_no_tools(self):
        e = self.engine()
        e.history.append(e.store.new_msg("her", "我得先离开了。", ts=NOW-MIN))
        Farewell.begin(e.store, NOW-MIN, NOW+5*MIN)
        m = e.store.new_msg("user", "好，注意休息", ts=NOW)
        m.update(undelivered=True, farewellFollowup=True)
        e.history.append(m)
        await e._farewell_reply()
        self.assert_current(query=False)
        self.assertEqual(self.request()["tools"], [])
        self.assertTrue(m.get("answered"))

    async def test_14_private_schedule_recall_bypasses_memory_and_knowledge(self):
        e = self.engine()
        e.memory.recall = AsyncMock(side_effect=AssertionError("日程不查长期记忆"))
        e.knowledge = SimpleNamespace(recall=AsyncMock(side_effect=AssertionError("日程不查剧情")))
        expected = self.activity.call_result(self.schedule_call(), NOW, state_fn=self.state_fn)
        result = await resolved(e._tool_result(self.schedule_call(), []))
        self.assertEqual(result, expected)
        e.memory.recall.assert_not_called()
        e.knowledge.recall.assert_not_called()

    async def test_15_group_schedule_recall_bypasses_memory_packs_and_commits(self):
        c = self.group()
        c.hub._recall_all = AsyncMock(side_effect=AssertionError("日程不查共享记忆或剧情"))
        c.round_memory.recall_note = AsyncMock(side_effect=AssertionError("日程不查群摘要"))
        commits = []
        expected = self.activity.call_result(self.schedule_call(), NOW, state_fn=self.state_fn)
        result = await c._tool_result(self.schedule_call(), commits=commits)
        self.assertEqual(result, expected)
        self.assertEqual(commits, [])
        c.hub._recall_all.assert_not_called()
        c.round_memory.recall_note.assert_not_called()

    async def test_16_old_private_memory_recall_remains_available(self):
        e = self.engine()
        e.memory.recall = AsyncMock(return_value="真实聊天记忆")
        e.knowledge = SimpleNamespace(recall=AsyncMock(return_value="旧剧情参考"))
        result = await resolved(e._tool_result({"name": "recall", "input": {"about": "以前约好的事情"}}, []))
        self.assertIn("真实聊天记忆", result)
        self.assertIn("旧剧情参考", result)
        e.memory.recall.assert_awaited_once()

    async def test_17_old_group_recall_keeps_deferred_commit(self):
        c = self.group()
        committed = []
        commit = lambda: committed.append(True)
        c.hub._recall_all = AsyncMock(return_value=("真实群记忆", commit))
        c.round_memory.recall_note = AsyncMock(return_value="相关摘要")
        commits = []
        result = await c._tool_result({"name": "recall", "input": {"about": "约好的事情", "source": "memory"}}, commits=commits)
        self.assertIn("真实群记忆", result)
        self.assertIn("相关摘要", result)
        self.assertEqual(commits, [commit])
        self.assertEqual(committed, [])

    async def test_18_real_tool_loop_resolves_schedule_without_history_mutation(self):
        e = self.engine()
        await self.private_message(e, "你昨天的安排是什么？")
        e.memory.recall = AsyncMock(side_effect=AssertionError("日程工具不能变成聊天回忆"))
        before_memory = copy.deepcopy(e.store.memory)
        self.provider.script = [F.Response(calls=[("recall", self.schedule_call()["input"])]), F.Response("昨天主要是在休息。")]
        with patch.object(self.activity, "call_result", wraps=self.activity.call_result) as route:
            await e.run_turn()
        self.assertEqual(len(self.provider.requests), 2)
        self.assertTrue(route.called)
        self.assertTrue(self.sender.sent)
        e.memory.recall.assert_not_called()
        self.assertEqual(e.store.memory, before_memory)

    async def test_19_real_group_tool_loop_resolves_schedule_without_commits(self):
        c = self.group()
        m = await self.group_message(c, text="你昨天的安排是什么？")
        c.hub._recall_all = AsyncMock(side_effect=AssertionError("日程不能记为回忆命中"))
        c.round_memory.recall_note = AsyncMock(side_effect=AssertionError("日程不能查包"))
        self.provider.script = [F.Response(calls=[("recall", self.schedule_call()["input"])]), F.Response("昨天只是做些日常小事。")]
        await c.turn("called", [m["id"]])
        self.assertEqual(len(self.provider.requests), 2)
        self.assertTrue(self.sender.sent)
        c.hub._recall_all.assert_not_called()
        c.round_memory.recall_note.assert_not_called()

    async def test_20_current_context_never_gets_written_into_original_messages(self):
        e = self.engine()
        await self.private_message(e, "来聊聊吧")
        await e.run_turn()
        self.assertTrue(self.provider.requests)
        for m in e.history:
            self.assertNotIn("【你这边的安排】", m.get("text", ""))

    async def test_21_answered_private_schedule_question_is_not_queried_again(self):
        self.plain_model()
        e = self.engine()
        old = await self.private_message(e, "你昨天都做了什么？")
        old.update(seen=True, answered=True)
        e.history.append(e.store.new_msg("her", "刚才已经说过昨天的大概安排。", ts=NOW-MIN))
        await self.private_message(e, "这本书挺有趣的")
        yesterday = self.activity.query(NOW, "yesterday", state_fn=self.state_fn)
        await e.run_turn()
        self.assertNotIn(yesterday, self.request()["system"])

    async def test_22_old_group_schedule_question_does_not_expand_new_focus(self):
        self.plain_model()
        c = self.group()
        old = await self.group_message(c, text="你昨天都做了什么？")
        old.update(read=True, shown=True, answered=True, called=False)
        current = await self.group_message(c, text="这本书挺有趣的")
        yesterday = self.activity.query(NOW, "yesterday", state_fn=self.state_fn)
        await c.turn("called", [current["id"]])
        self.assertNotIn(yesterday, self.request()["system"])

    async def test_23_future_schedule_boundary_cannot_fall_back_to_story(self):
        e = self.engine()
        e.memory.recall = AsyncMock(side_effect=AssertionError("未来日程不能回落聊天记忆"))
        e.knowledge = SimpleNamespace(recall=AsyncMock(side_effect=AssertionError("未来日程不能回落剧情")))
        result = await resolved(e._tool_result(self.schedule_call("future"), []))
        self.assertEqual(result, self.activity.query(NOW, "future", state_fn=self.state_fn))
        e.memory.recall.assert_not_called()
        e.knowledge.recall.assert_not_called()

    async def test_24_catchup_relative_yesterday_uses_message_date(self):
        e = self.engine()
        m = e.store.new_msg("user", "你昨天上午做了什么？", ts=NOW-24*HOUR)
        m["undelivered"] = True
        e.history.append(m)
        await e._catchup_now()
        expected = self.activity.query(NOW, "before_yesterday", state_fn=self.state_fn)
        self.assertIn(expected, self.request()["system"], "昨天发的‘昨天’现在是前天，不能注入昨天日程")
        self.assertNotIn(self.activity.query(NOW, "yesterday", "上午", state_fn=self.state_fn), self.request()["system"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ActivityFlowTest)
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    passed = result.testsRun - len(result.failures) - len(result.errors)
    print(f"activityflow236: {passed}/{result.testsRun} ({F.PKG})")
    raise SystemExit(0 if result.wasSuccessful() else 1)
