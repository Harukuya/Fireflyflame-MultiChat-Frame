"""v0.2.35 主动开场无人接话免道别；真实引擎、假 Provider/平台/时钟。

python -I -B farewell235_test.py <个人/发布源码或框架目录> [复刻包目录] [临时目录]
三版同份脚本；事件栅栏覆盖真实等待点，不读取真实运行数据。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
fixture = Path(__file__).with_name("no_reply233_test.py")
spec = importlib.util.spec_from_file_location("_farewell235_fixture", fixture)
F = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = F
spec.loader.exec_module(F)
E, G, NOW, MIN, HOUR = F.E, F.G, F.NOW, F.MIN, F.HOUR
ATTACHED, STANDALONE = "ATTACHED_FAREWELL_TEST", "STANDALONE_FAREWELL_TEST"


class Farewell235Test(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = F.NoReplyTest.asyncTearDown
    recall = F.NoReplyTest.recall
    engine = F.NoReplyTest.engine
    group = F.NoReplyTest.group
    private_message = F.NoReplyTest.private_message

    async def asyncSetUp(self):
        await F.NoReplyTest.asyncSetUp(self)
        self.clock = NOW
        self.segment = NOW - HOUR
        self.edge = NOW + 8 * MIN
        self.online = True
        for module in (E, G):
            for name, value in (
                ("now_ms", lambda: self.clock),
                ("online_since", lambda t: self.segment),
                ("offline_after", lambda t: self.edge),
                ("state_at", lambda t: F.S.HerState(self.online, "base", "night", 22.0, 0)),
            ):
                guard = patch.object(module, name, value)
                guard.start()
                self.addCleanup(guard.stop)
        for name, value in (("build_farewell_context", lambda *a: ATTACHED),
                            ("build_standalone_farewell_context", lambda *a: STANDALONE)):
            guard = patch.object(F.module("persona"), name, value)
            guard.start()
            self.addCleanup(guard.stop)

    async def row(self, c, *, called=False, text="大家聊到了一部新电影", qid=None):
        await c.on_message(F.PEER, "测试群友", text, [], qid or "q" + str(len(c.msgs)), called)
        return c.msgs[-1]

    async def look(self, c=None):
        c = c or self.group()
        m = await self.row(c)
        self.provider.script = [F.Response("这部电影我也很感兴趣。")]
        await c.turn("look", [m["id"]])
        return c

    async def proactive(self, e=None, text="想和你说一件小事。"):
        e = e or self.engine()
        e._should_reach_out = lambda now: True
        self.provider.script = [F.Response(text)]
        await e._maybe_reach_out(self.clock)
        return e

    def no_farewell(self):
        self.assertNotIn(ATTACHED, self.provider.requests[-1]["system"])
        self.assertNotIn(STANDALONE, self.provider.requests[-1]["system"])

    async def test_01_look_opens_window_without_attached_or_standalone_bye(self):
        c = await self.look()
        self.no_farewell()
        self.assertTrue(c.window_open(self.clock))
        self.assertFalse(c.store.extra["windowExchange"]["engaged"])
        self.assertNotIn("farewellFor", c.store.extra)
        self.assertFalse(await c.maybe_farewell(self.clock))
        self.assertEqual(len(self.provider.requests), 1)

    async def test_02_unrelated_arrival_alone_does_not_unlock(self):
        c = await self.look()
        await self.row(c, text="别的群友聊另外的事")
        self.assertFalse(await c.maybe_farewell(self.clock))
        self.assertFalse(c._window_engaged(self.clock))

    async def test_03_called_message_merged_during_look_can_say_bye(self):
        c = self.group()
        a = await self.row(c)
        b = await self.row(c, called=True, text="你觉得怎么样？")
        self.provider.script = [F.Response("说完这个我得先走啦。")]
        await c.turn("look", [a["id"]])
        self.assertIn(ATTACHED, self.provider.requests[-1]["system"])
        self.assertTrue(b.get("answered"))
        self.assertTrue(c._leaving())

    async def test_04_successful_window_reply_restores_bye(self):
        c = await self.look()
        m = await self.row(c, text="听了你的话，我也准备去看看")
        self.provider.script = [F.Response("那就这么说定，我先离开一会儿。")]
        await c.turn("window", [m["id"]])
        self.assertIn(ATTACHED, self.provider.requests[-1]["system"])
        self.assertTrue(c._leaving())

    async def test_05_existing_exchange_is_not_erased_by_look(self):
        c = self.group()
        self.edge = NOW + HOUR
        m = await self.row(c, called=True)
        await c.turn("called", [m["id"]])
        self.assertTrue(c._window_engaged(self.clock))
        self.edge = NOW + 8 * MIN
        await self.look(c)
        self.assertIn(ATTACHED, self.provider.requests[-1]["system"])

    async def test_06_no_reply_in_window_does_not_claim_exchange(self):
        c = await self.look()
        m = await self.row(c)
        self.provider.script = [F.no_reply()]
        await c.turn("window", [m["id"]])
        self.assertFalse(c.store.extra["windowExchange"]["engaged"])
        self.assertFalse(await c.maybe_farewell(self.clock))

    async def test_07_failed_window_delivery_does_not_claim_exchange(self):
        c = await self.look()
        m = await self.row(c)
        async def fail(*args, **kwargs):
            return False
        self.sender.send = fail
        self.provider.script = [F.Response("我接着说。")]
        self.assertFalse(await c.turn("window", [m["id"]]))
        self.assertFalse(c.store.extra["windowExchange"]["engaged"])
        self.assertFalse(await c.maybe_farewell(self.clock))

    async def test_08_failed_look_delivery_sets_no_marker(self):
        c = self.group()
        async def fail(*args, **kwargs):
            return False
        self.sender.send = fail
        await self.look(c)
        self.assertNotIn("windowExchange", c.store.extra)
        self.assertFalse(c.window_open(self.clock))

    async def test_09_declined_look_sets_no_marker(self):
        c = self.group()
        m = await self.row(c)
        self.provider.script = [F.no_reply()]
        await c.turn("look", [m["id"]])
        self.assertNotIn("windowExchange", c.store.extra)
        self.assertEqual(self.sender.sent, [])

    async def test_10_group_restart_preserves_unanswered_opening(self):
        c = await self.look()
        loaded = F.Store(c.store.root, self.her)
        self.assertEqual(loaded.extra["windowExchange"], c.store.extra["windowExchange"])
        c.store = loaded
        self.assertFalse(await c.maybe_farewell(self.clock))

    async def test_11_expired_exchange_does_not_leak_to_next_look(self):
        c = self.group()
        self.edge = NOW + HOUR
        m = await self.row(c, called=True)
        await c.turn("called", [m["id"]])
        self.clock = c.store.extra["windowUntil"] + 1
        self.edge = self.clock + 8 * MIN
        await self.look(c)
        self.no_farewell()
        self.assertFalse(c._window_engaged(self.clock))

    async def test_12_next_online_segment_closes_old_exchange(self):
        c = await self.look()
        self.segment += HOUR
        self.assertFalse(c.window_open(self.clock))
        self.assertNotIn("windowExchange", c.store.extra)

    async def test_13_recall_before_first_send_does_not_commit_exchange(self):
        c = await self.look()
        m = await self.row(c, called=True, qid="withdraw-this")
        entered, release = asyncio.Event(), asyncio.Event()
        async def model():
            entered.set()
            await release.wait()
            return F.Response("准备说再见")
        self.provider.script = [model]
        task = asyncio.create_task(c.turn("called", [m["id"]]))
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 3)
        await c.on_recall("withdraw-this")
        release.set()
        await task
        self.assertEqual(len(self.sender.sent), 1)
        self.assertFalse(c.store.extra["windowExchange"]["engaged"])
        self.assertFalse(await c.maybe_farewell(self.clock))

    async def test_14_actual_offline_clears_window_marker(self):
        c = await self.look()
        self.online = False
        self.assertFalse(c.window_open(self.clock))
        self.assertNotIn("windowExchange", c.store.extra)

    async def test_15_private_proactive_stays_quiet_and_survives_restart(self):
        e = await self.proactive()
        self.no_farewell()
        self.assertTrue(e._awaiting_proactive_response(self.clock))
        e.store = F.Store(e.store.root, self.her)
        self.assertFalse(await e._maybe_farewell(self.clock))
        self.assertEqual(len(self.provider.requests), 1)

    async def test_16_private_actual_reply_restores_normal_farewell(self):
        e = await self.proactive()
        await self.private_message(e, "你刚说的小事我也想听")
        self.provider.script = [F.Response("好，讲完我就先走啦。")]
        await e.run_turn()
        self.assertIn(ATTACHED, self.provider.requests[-1]["system"])
        self.assertTrue(e._leaving())
        self.assertNotIn("proactiveOpening", e.store.extra)

    async def test_17_withdrawn_unanswered_private_response_stays_quiet(self):
        e = await self.proactive()
        await e.on_user_message("等一下", [], "withdraw")
        self.assertFalse(e._awaiting_proactive_response(self.clock))
        await e.on_recall("withdraw")
        self.assertTrue(e._awaiting_proactive_response(self.clock))
        self.assertFalse(await e._maybe_farewell(self.clock))

    async def test_18_private_arrival_while_first_send_waits_is_not_lost(self):
        e = self.engine()
        entered, release = asyncio.Event(), asyncio.Event()
        async def sending(*args, **kwargs):
            entered.set()
            await release.wait()
            self.sender.sent.append((args, kwargs))
            return True
        self.sender.send = sending
        task = asyncio.create_task(self.proactive(e))
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 3)
        await e.on_user_message("我在呢", [], "during-send")
        m = e.history[-1]
        release.set()
        await task
        self.assertIn(m["id"], e.store.extra["proactiveOpening"]["replies"])
        self.assertFalse(e._awaiting_proactive_response(self.clock))

    async def test_19_private_arrival_then_recall_while_send_waits(self):
        e = self.engine()
        entered, release = asyncio.Event(), asyncio.Event()
        async def sending(*args, **kwargs):
            entered.set()
            await release.wait()
            return True
        self.sender.send = sending
        task = asyncio.create_task(self.proactive(e))
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 3)
        await e.on_user_message("等等", [], "withdraw")
        await e.on_recall("withdraw")
        release.set()
        await task
        self.assertTrue(e._awaiting_proactive_response(self.clock))

    async def test_20_partial_proactive_cancellation_keeps_delivered_marker(self):
        e = self.engine()
        entered = asyncio.Event()
        async def sending(*args, **kwargs):
            if self.sender.sent:
                entered.set()
                await asyncio.Event().wait()
            self.sender.sent.append((args, kwargs))
            return True
        self.sender.send = sending
        task = asyncio.create_task(self.proactive(e, "第一件小事。\n\n第二件小事。"))
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.sender.sent), 1)
        self.assertTrue(e._awaiting_proactive_response(self.clock))
        loaded = F.Store(e.store.root, self.her)
        self.assertIn("proactiveOpening", loaded.extra)
        self.assertFalse(await e._maybe_farewell(self.clock))

    async def test_21_private_failed_send_has_no_opening(self):
        async def fail(*args, **kwargs):
            return False
        self.sender.send = fail
        e = await self.proactive()
        self.assertNotIn("proactiveOpening", e.store.extra)
        self.assertEqual(self.sender.sent, [])

    async def test_22_private_arrival_during_request_cancels_stale_opening(self):
        e = self.engine()
        e._should_reach_out = lambda now: True
        entered, release = asyncio.Event(), asyncio.Event()
        async def model():
            entered.set()
            await release.wait()
            return F.Response("已过时的主动话")
        self.provider.script = [model]
        task = asyncio.create_task(e._maybe_reach_out(self.clock))
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 3)
        await e.on_user_message("我有件事情想说", [], "new")
        release.set()
        await task
        self.assertEqual(self.sender.sent, [])
        self.assertNotIn("proactiveOpening", e.store.extra)

    async def test_23_private_next_online_segment_releases_protection(self):
        e = await self.proactive()
        self.segment += HOUR
        self.assertFalse(e._awaiting_proactive_response(self.clock))
        self.assertNotIn("proactiveOpening", e.store.extra)

    async def test_24_segment_key_ignores_subminute_clock_drift(self):
        with patch.object(G, "online_since", lambda t: self.segment + t % MIN), \
             patch.object(E, "online_since", lambda t: self.segment + t % MIN):
            c = await self.look()
            e = await self.proactive()
            self.clock += 30_137
            self.assertTrue(c.window_open(self.clock))
            self.assertFalse(c._window_engaged(self.clock))
            self.assertTrue(e._awaiting_proactive_response(self.clock))

    async def test_25_group_arrival_between_bubbles_keeps_opening_marker(self):
        c = self.group()
        m = await self.row(c)
        entered, release = asyncio.Event(), asyncio.Event()
        async def sending(*args, **kwargs):
            if self.sender.sent:
                entered.set()
                await release.wait()
            self.sender.sent.append((args, kwargs))
            return True
        self.sender.send = sending
        self.provider.script = [F.Response("我也很喜欢这个电影。\n\n里面的风景很好看。")]
        task = asyncio.create_task(c.turn("look", [m["id"]]))
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 3)
        self.assertFalse(c.store.extra["windowExchange"]["engaged"])
        await self.row(c, text="另外两个人开始聊晚饭")
        self.assertFalse(c.store.extra["windowExchange"]["engaged"])
        release.set()
        await task
        self.assertTrue(c.window_open(self.clock))
        self.assertFalse(c._window_engaged(self.clock))
        self.assertFalse(await c.maybe_farewell(self.clock))
        self.assertEqual(len(self.provider.requests), 1)

    async def test_26_effective_exchange_keeps_standalone_farewell(self):
        c = self.group()
        self.edge = NOW + HOUR
        m = await self.row(c, called=True)
        await c.turn("called", [m["id"]])
        self.edge = NOW + 8 * MIN
        self.provider.script = [F.Response("现在我得先走啦，回头聊。")]
        self.assertTrue(await c.maybe_farewell(self.clock))
        self.assertIn(STANDALONE, self.provider.requests[-1]["system"])
        self.assertTrue(c._leaving())

    async def historical_called(self, explicit):
        c = self.group()
        old = await self.row(c, called=True, text="很早之前问过的事")
        old.update(read=True, answered=True, shown=True)
        c.msgs.append({**c.store.new_msg("her", "旧问题早已经回答了"), "read": True})
        await self.row(c, text="大家又聊起电影的新话题")
        self.provider.script = [F.Response("刚好这部电影我也喜欢")]
        await c.turn("look", [old["id"]] if explicit else [])
        self.no_farewell()
        self.assertFalse(c._window_engaged(self.clock))
        self.assertFalse(await c.maybe_farewell(self.clock))

    async def test_27_old_called_in_context_does_not_unlock_topic_look(self):
        await self.historical_called(False)

    async def test_28_old_called_explicit_focus_does_not_unlock(self):
        await self.historical_called(True)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Farewell235Test)
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    passed = result.testsRun - len(result.failures) - len(result.errors)
    print(f"farewell235: {passed}/{result.testsRun} ({F.PKG})")
    raise SystemExit(0 if result.wasSuccessful() else 1)
