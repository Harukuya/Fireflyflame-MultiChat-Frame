"""v0.2.34 私聊窗口：已整理原话退出，最近三条长期记忆承接。

python -I -B privatecontext234_test.py <个人/发布源码或框架目录> [复刻包目录] [临时目录]
只使用真实引擎和内存逻辑、假的 Provider/QQ/时间；不读宿主数据或密钥。
新接口缺失会记为失败，仍继续其余用例，支持旧源码负对照。
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 只借用服务边界与建实例方法，不执行旧版本用例。
fixture_path = Path(__file__).with_name("no_reply233_test.py")
spec = importlib.util.spec_from_file_location("_privatecontext234_fixture", fixture_path)
F = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = F
spec.loader.exec_module(F)
M, J, Farewell = (F.module(name) for name in ("memory", "jsutil", "farewell"))
NOW, MIN, HOUR, DAY = F.NOW, F.MIN, F.HOUR, 24 * F.HOUR
OLD_USER, OLD_HER, NEW_USER = "已整理原话用户标记甲", "已整理原话助手标记乙", "这轮真实新消息标记丙"
MEMORY = "最近长期记忆标记丁"


class PrivateContextTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = F.NoReplyTest.asyncSetUp
    asyncTearDown = F.NoReplyTest.asyncTearDown
    recall = F.NoReplyTest.recall
    engine = F.NoReplyTest.engine
    private_message = F.NoReplyTest.private_message

    def new(self, peer=False, enabled=True):
        e = self.engine(peer)
        e.memory.enabled = lambda: enabled
        return e

    def row(self, e, text, role="user", ts=None, **flags):
        m = e.store.new_msg(role, text, ts=NOW if ts is None else ts)
        m.update(flags)
        e.history.append(m)
        return m

    def old(self, e, *, age=HOUR, remember=True):
        self.row(e, OLD_USER, ts=NOW-age-1000, seen=True, answered=True)
        last = self.row(e, OLD_HER, "her", NOW-age)
        e.store.memory.update(doneMsgId=last["id"], doneTs=last["ts"])
        if remember:
            e.store.memory["episodes"] = [self.ep(1, MEMORY, NOW-age-1000, NOW-age)]
        return last

    @staticmethod
    def ep(id_, text, start=None, end=None, **flags):
        start = NOW-HOUR if start is None else start
        end = start+MIN if end is None else end
        return {"id": id_, "fromMsg": "from-"+str(id_), "toMsg": "to-"+str(id_),
                "start": start, "end": end, "summary": text, "gist": "提要"+text,
                "feeling": "安心", "weight": 5, "level": 0, "recalls": 0,
                "lastRecalledAt": 0, "topics": [], **flags}

    @staticmethod
    def ws(e, history=None, **kwargs):
        return e.memory.working_set(e.history if history is None else history, private=True, **kwargs)

    def captured(self, index=-1):
        self.assertTrue(self.provider.requests, "必须走到真实 Llm 的假 Provider 边界")
        request = self.provider.requests[index]
        return request, str(request["messages"])

    def assert_window(self, request, text, *, memory=True):
        self.assertNotIn(OLD_USER, text)
        self.assertNotIn(OLD_HER, text)
        if memory:
            self.assertIn(MEMORY, request["system"])
        self.assertNotIn("失败后", text)

    async def test_01_id_cursor_removes_only_consolidated_prefix(self):
        e = self.new()
        self.old(e)
        fresh = self.row(e, NEW_USER)
        before = copy.deepcopy(e.history)
        self.assertEqual([m["id"] for m in self.ws(e)], [fresh["id"]])
        self.assertEqual(e.history, before, "窗口只投影，不删除磁盘原文")

    async def test_02_timestamp_cursor_works_when_id_trimmed(self):
        e = self.new()
        old = self.old(e)
        e.history.remove(old)
        fresh = self.row(e, NEW_USER)
        self.assertEqual([m["id"] for m in self.ws(e)], [fresh["id"]])

    async def test_03_timestamp_only_legacy_cursor(self):
        e = self.new()
        self.old(e)
        e.store.memory["doneMsgId"] = None
        fresh = self.row(e, NEW_USER)
        self.assertEqual([m["id"] for m in self.ws(e)], [fresh["id"]])

    async def test_04_id_position_wins_over_equal_timestamps(self):
        e = self.new()
        old = self.old(e)
        fresh = self.row(e, NEW_USER, ts=old["ts"])
        self.assertEqual([m["id"] for m in self.ws(e)], [fresh["id"]])

    async def test_05_all_consolidated_empty_window_no_summary_fallback(self):
        e = self.new()
        self.old(e, remember=False)
        self.assertEqual(self.ws(e), [])
        note, ids = e.memory.recent_private_context(NOW)
        self.assertFalse(note)
        self.assertEqual(ids, set())
        self.assertEqual(e.build_turns(e.history), [])

    async def test_06_unconsolidated_undelivered_and_later_rows_survive(self):
        e = self.new()
        self.old(e)
        a = self.row(e, "未读留言", undelivered=True)
        b = self.row(e, "留言之后的消息")
        self.assertEqual([m["id"] for m in self.ws(e)], [a["id"], b["id"]])
        self.assertTrue(a["undelivered"])

    async def test_07_disabled_caps_only_unconsolidated_rows(self):
        e = self.new(enabled=False)
        self.old(e)
        rows = [self.row(e, f"未整理第{i}句", ts=NOW+i) for i in range(60)]
        self.assertEqual([m["id"] for m in self.ws(e)], [m["id"] for m in rows[-50:]])
        e.history[:] = e.history[:2]
        self.assertEqual(self.ws(e), [], "禁用整理也不能把已经整理的原话重新带回")

    async def test_08_without_cursor_keeps_unconsolidated_history(self):
        e = self.new()
        rows = [self.row(e, f"没有整理过{i}", ts=NOW+i) for i in range(60)]
        self.assertEqual(self.ws(e), rows)

    async def test_09_explicit_round_targets_survive_cursor_advance(self):
        e = self.new()
        self.old(e)
        target = self.row(e, NEW_USER)
        later = self.row(e, "随后一条", "her", NOW+1)
        e.store.memory.update(doneMsgId=later["id"], doneTs=later["ts"])
        self.assertEqual([m["id"] for m in self.ws(e, keep_ids={target["id"]})], [target["id"]])
        e.round_ids = {target["id"]}
        self.assertIn(NEW_USER, str(e.build_turns(e.history)))
        e._mark_seen()
        self.assertIn(target["id"], e._asked)
        self.assertNotIn(e.history[0]["id"], e._asked)

    async def test_10_default_working_set_keeps_group_legacy_contract(self):
        e = self.new()
        self.old(e)
        self.assertEqual(e.memory.working_set(e.history), e.history)

    async def test_11_latest_three_by_creation_id_display_event_order(self):
        e = self.new()
        eps = [self.ep(1, "较早写入但事件新", NOW-MIN),
               self.ep(2, "最近写入第二个", NOW-2*HOUR),
               self.ep(3, "最近写入第三个", NOW-4*HOUR),
               self.ep(4, "最近写入第四个", NOW-3*HOUR)]
        e.store.memory["episodes"] = eps
        note, ids = e.memory.recent_private_context(NOW)
        self.assertEqual(ids, {2, 3, 4})
        self.assertNotIn("较早写入但事件新", note)
        self.assertLess(note.index("最近写入第三个"), note.index("最近写入第四个"))
        self.assertLess(note.index("最近写入第四个"), note.index("最近写入第二个"))

    async def test_12_fewer_than_three_and_no_foreign_auto_fill(self):
        e, other = self.new(), self.new()
        e.store.memory["episodes"] = [self.ep(7, "本地唯一")]
        other.store.memory["episodes"] = [self.ep(99, "外地不会凑数")]
        e.memory.others = lambda: [(other.memory, "其他会话", "")]
        note, ids = e.memory.recent_private_context(NOW)
        self.assertEqual(ids, {7})
        self.assertIn("本地唯一", note)
        self.assertNotIn("外地不会凑数", note)

    async def test_13_summary_has_full_date_time_and_relative_time(self):
        e = self.new()
        start, end = NOW-DAY, NOW-DAY+2*MIN
        e.store.memory["episodes"] = [self.ep(1, MEMORY, start, end)]
        note, _ = e.memory.recent_private_context(NOW)
        self.assertIn(J.format_timestamp(start), note)
        self.assertIn(J.format_timestamp(end)[-5:], note)
        self.assertTrue(any(word in note for word in ("昨天", "前天", "今天稍早", "前几天")), note)
        self.assertIn("交谈", note)

    async def test_14_faded_memory_does_not_restore_precise_summary(self):
        e = self.new()
        e.store.memory["episodes"] = [self.ep(1, "精确细节不可再显示", NOW-10*DAY,
                                                level=2, weight=2, gist="只记得大概事情")]
        note, ids = e.memory.recent_private_context(NOW)
        self.assertEqual(ids, {1})
        self.assertIn("模糊", note)
        self.assertIn("只记得大概事情", note)
        self.assertNotIn("精确细节不可再显示", note)

    async def test_15_expired_or_deleted_memory_not_resurrected(self):
        e = self.new()
        self.old(e)
        e.store.memory["episodes"] = [self.ep(1, "过期事件", NOW-31*DAY, weight=1)]
        note, ids = e.memory.recent_private_context(NOW)
        self.assertNotIn("过期事件", note or "")
        self.assertFalse(ids)
        self.assertEqual(self.ws(e), [])
        e.store.memory["episodes"] = []
        self.assertFalse(e.memory.recent_private_context(NOW)[0])

    async def test_16_passive_summary_display_neither_strengthens_nor_saves(self):
        e = self.new()
        e.store.memory["episodes"] = [self.ep(1, MEMORY, level=1, weight=4, recalls=2,
                                                lastRecalledAt=NOW-DAY)]
        before = copy.deepcopy(e.store.memory)
        with patch.object(e.store, "save_memory", side_effect=AssertionError("正常展示不应写盘")):
            for _ in range(3):
                self.assertTrue(e.memory.recent_private_context(NOW)[0])
        self.assertEqual(e.store.memory, before)

    async def test_17_automatic_association_dedup_local_but_not_foreign_same_id(self):
        e, other = self.new(), self.new()
        e.store.memory["episodes"] = [self.ep(1, "本地已展示事件")]
        other.store.memory["episodes"] = [self.ep(1, "其他会话同号事件")]
        e.memory.others = lambda: [(other.memory, "另一个会话", "")]
        with patch.object(M.Memory, "_score", return_value=[100, 100]):
            note = await e.memory.associate("事件", [], NOW, private=True)
        self.assertNotIn("本地已展示事件", note or "")
        self.assertIn("其他会话同号事件", note)
        self.assertEqual(e.store.memory["episodes"][0]["recalls"], 0)

    async def test_18_group_default_association_not_private_dedup(self):
        e = self.new()
        e.store.memory["episodes"] = [self.ep(1, "群仍可联想的事件")]
        with patch.object(M.Memory, "_score", return_value=[100]):
            note, commit = await e.memory.associate_deferred("事件", [], NOW)
        self.assertIn("群仍可联想的事件", note)
        self.assertEqual(e.store.memory["episodes"][0]["recalls"], 0)
        commit()
        self.assertEqual(e.store.memory["episodes"][0]["recalls"], 1)

    async def test_19_real_private_request_uses_new_raw_and_three_memories(self):
        e = self.new()
        self.old(e)
        await self.private_message(e, NEW_USER)
        await e.run_turn()
        request, text = self.captured()
        self.assert_window(request, text)
        self.assertIn(NEW_USER, text)
        self.assertNotIn("【本轮后台情形】", text)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertTrue(self.sender.sent)

    async def test_20_peer_private_request_uses_same_window(self):
        e = self.new(peer=True)
        self.old(e)
        await self.private_message(e, NEW_USER)
        await e.run_turn()
        request, text = self.captured()
        self.assert_window(request, text)
        self.assertIn(NEW_USER, text)

    async def test_21_real_catchup_retains_all_unconsolidated_backlog(self):
        e = self.new()
        self.old(e)
        for text in (NEW_USER, "补发第二条留言"):
            self.row(e, text, ts=NOW-MIN, undelivered=True)
        await e._catchup_now()
        request, text = self.captured()
        self.assert_window(request, text)
        self.assertIn(NEW_USER, text)
        self.assertIn("补发第二条留言", text)
        self.assertNotIn("【本轮后台情形】", text)
        self.assertFalse(any(m.get("undelivered") for m in e.history))

    async def test_22_background_empty_inserts_one_context_not_history(self):
        e = self.new(peer=True)
        self.old(e)
        before = copy.deepcopy(e.history)
        turns = e.build_turns(e.history, background=True)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["role"], "user")
        self.assertIn("程序", turns[0]["content"])
        self.assertIn("测试群友", turns[0]["content"])
        self.assertIn("不是", turns[0]["content"])
        self.assertEqual(e.history, before)
        result = await self.llm.chat("根据安排决定", turns)
        self.assertTrue(result.ok)
        self.assertEqual(len(self.provider.requests), 1)

    async def test_23_background_assistant_only_is_preserved_after_normalization(self):
        e = self.new()
        self.old(e)
        self.row(e, "整理以后她自己刚说的", "her")
        turns = e.build_turns(e.history, background=True)
        result = await self.llm.chat("内部测试", turns)
        self.assertTrue(result.ok)
        request, text = self.captured()
        self.assertEqual([r["role"] for r in request["messages"]], ["user", "assistant"])
        self.assertIn("整理以后她自己刚说的", text)
        self.assertNotIn(OLD_HER, text)

    async def test_24_background_with_real_user_has_no_extra_trigger(self):
        e = self.new()
        self.old(e)
        self.row(e, NEW_USER)
        normal = e.build_turns(e.history)
        self.assertEqual(e.build_turns(e.history, background=True), normal)
        self.assertEqual(len(normal), 1)

    async def test_25_real_proactive_request_after_all_raw_consolidated(self):
        e = self.new()
        self.old(e, age=4*HOUR)
        e._should_reach_out = lambda now: True
        await e._maybe_reach_out(NOW)
        request, text = self.captured()
        self.assert_window(request, text)
        self.assertIn("程序", text)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertTrue(self.sender.sent)

    async def test_26_real_recheck_request_after_all_raw_consolidated(self):
        e = self.new()
        self.old(e)
        e.store.thought = {"id": "thought", "topic": "稍后问候", "tier": "soon", "createdAt": NOW-MIN,
                           "dueAt": NOW-1, "waited": False}
        await e._maybe_recheck(NOW)
        request, text = self.captured()
        self.assert_window(request, text)
        self.assertIn("程序", text)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertTrue(self.sender.sent)

    async def test_27_real_standalone_farewell_after_all_raw_consolidated(self):
        e = self.new()
        self.old(e, age=MIN)
        with patch.object(F.E, "offline_after", return_value=NOW+3*MIN):
            await e._maybe_farewell(NOW)
        request, text = self.captured()
        self.assert_window(request, text)
        self.assertIn("程序", text)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertTrue(self.sender.sent)

    async def test_28_real_short_farewell_filters_old_previous_without_lookup(self):
        e = self.new()
        self.old(e)
        self.row(e, "刚才还没整理的道别", "her")
        Farewell.begin(e.store, NOW, NOW+3*MIN)
        selected = self.row(e, NEW_USER, undelivered=True, farewellFollowup=True)
        await e._farewell_reply()
        request, text = self.captured()
        self.assert_window(request, text)
        self.assertIn(NEW_USER, text)
        self.assertIn("刚才还没整理的道别", text)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(request["tools"], [])
        self.assertTrue(selected.get("answered"))

    async def test_29_caption_and_seen_share_the_filtered_window(self):
        e = self.new()
        self.old(e)
        current = self.row(e, NEW_USER, attachment={"kind": "image", "caption": "新的图"})
        calls = []

        async def caption(messages, saved, deadline):
            calls.append([m["id"] for m in messages])

        e.captions = caption
        e.round_ids = {current["id"]}
        self.assertTrue(await e._ready_to_ask(NOW+20_000))
        self.assertTrue(await e._prepare_background(e.rev))
        e._mark_seen()
        self.assertEqual(calls, [[current["id"]], [current["id"]]])
        self.assertEqual(e._asked, [current["id"]])
        self.assertEqual(set(e._recent_input), {current["id"]})

    async def test_30_busy_memory_refuses_before_any_request_or_cursor_update(self):
        e = self.new()
        self.old(e)
        target = self.row(e, NEW_USER)
        e.mem_busy, e.in_flight = False, True
        before = copy.deepcopy(e.store.memory)
        result = await e.run_memory(force=True)
        self.assertIn("正在回话", result or "")
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(e.store.memory, before)
        self.assertIn(target, self.ws(e))

    async def test_31_concurrent_cursor_change_keeps_actual_round_target(self):
        e = self.new()
        self.old(e)
        target = await self.private_message(e, NEW_USER)
        entered, release = asyncio.Event(), asyncio.Event()
        original = e.memory.associate

        async def paused(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        e.memory.associate = paused
        task = asyncio.create_task(e.run_turn())
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 2)
        self.assertTrue(e.in_flight)
        self.assertEqual(self.provider.requests, [])
        e.store.memory.update(doneMsgId=target["id"], doneTs=target["ts"])
        release.set()
        await asyncio.wait_for(task, 3)
        request, text = self.captured()
        self.assert_window(request, text)
        self.assertIn(NEW_USER, text)
        self.assertTrue(target.get("answered"))

    async def test_32_leading_unconsolidated_assistant_survives_real_llm(self):
        e = self.new(peer=True)
        old = self.row(e, OLD_USER, ts=NOW-HOUR, seen=True, answered=True)
        self.row(e, "游标之后尚未整理的助手原话", "her", NOW-HOUR+1000)
        self.row(e, NEW_USER)
        e.store.memory.update(doneMsgId=old["id"], doneTs=old["ts"])
        before = copy.deepcopy(e.history)
        result = await self.llm.chat("继续当前私聊", e.build_turns(e.history))
        self.assertTrue(result.ok)
        request, text = self.captured()
        self.assertEqual([m["role"] for m in request["messages"]], ["user", "assistant", "user"])
        self.assertIn("游标之后尚未整理的助手原话", text)
        self.assertIn(NEW_USER, text)
        self.assertNotIn(OLD_USER, text)
        marker = request["messages"][0]["content"]
        self.assertIn("未整理", marker)
        self.assertIn("不是测试群友新", marker)
        self.assertEqual(e.history, before)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PrivateContextTest)
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    passed = result.testsRun - len(result.failures) - len(result.errors)
    print(f"privatecontext234: {passed}/{result.testsRun} ({F.PKG})")
    raise SystemExit(0 if result.wasSuccessful() else 1)
