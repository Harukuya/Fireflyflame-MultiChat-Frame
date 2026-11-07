"""Per-purpose model routing through real conversation flows, temporary data only.

python -I -B route_scope240_test.py <source> [reference] [temporary directory]
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

spec = importlib.util.spec_from_file_location("_route240_fixture", Path(__file__).with_name("no_reply233_test.py"))
F = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = F
spec.loader.exec_module(F)
MR, R = F.module("model_routes"), F.module("rounds")


class RouteTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = F.NoReplyTest.asyncTearDown
    engine = F.NoReplyTest.engine
    group = F.NoReplyTest.group
    private_message = F.NoReplyTest.private_message
    group_message = F.NoReplyTest.group_message

    async def recall(self, memory, about, msgs):
        return "本地查到的旧事"

    async def asyncSetUp(self):
        await F.NoReplyTest.asyncSetUp(self)
        self.selected = {}
        self.main = self.provider
        self.main.provider_config["modalities"] = ["text"]
        self.llm = F.Llm(self.select_provider, F.LOG, self.events)
        self.hub.llm = self.hub.judge_llm = self.llm

    def select_provider(self):
        purpose = MR.current_purpose()
        return MR.pin_provider((id(self), purpose), lambda: self.selected.get(purpose, self.main))

    def routed(self, purpose, *, tools=True, image=False):
        p = F.Provider()
        p.provider_config = {"id": purpose, "model": purpose,
                             "modalities": ["text", *(["tool_use"] if tools else []), *(["image"] if image else [])]}
        self.selected[purpose] = p
        return p

    @staticmethod
    def joined(p):
        return str(p.requests)

    def only(self, p, count=1):
        self.assertEqual(len(p.requests), count)
        self.assertEqual(self.main.requests, [], "must not leak to the session's primary model")
        self.assertEqual(MR.current_purpose(), "", "scope must restore after the operation")

    async def test_01_private_reply_selects_before_tools_and_image_preparation(self):
        p = self.routed("private_reply", tools=True)
        p.script = [F.no_reply()]
        e = self.engine()
        seen = []
        async def captions(rows, saved, deadline):
            seen.append((MR.current_purpose(), e.llm.supports("image"), e.llm.supports("tool_use")))
            rows[-1]["attachment"]["caption"] = "一只蓝色小鸟"
            saved()
        e.captions = captions
        await e.on_user_message("这个怎么样", [{"kind": "image", "file": "fake.png"}], "q1")
        await e.run_turn()
        self.only(p)
        self.assertEqual(seen, [("private_reply", False, True)])
        self.assertIn("一只蓝色小鸟", self.joined(p))
        self.assertIn("no_reply", [t["name"] for t in p.requests[0]["tools"]])

    async def test_02_group_reply_uses_route_and_has_no_tools_when_unsupported(self):
        self.main.provider_config["modalities"] = ["text", "tool_use", "image"]
        p = self.routed("group_reply", tools=False)
        c = self.group()
        m = await self.group_message(c)
        await c.turn("called", [m["id"]])
        self.only(p)
        self.assertEqual(p.requests[0]["tools"], [])

    async def test_03_private_catchup_routes_before_preparing_images(self):
        p = self.routed("catchup")
        p.script = [F.no_reply()]
        e = self.engine()
        m = await self.private_message(e)
        m["undelivered"] = True
        scopes = []
        async def captions(rows, saved, deadline):
            scopes.append(MR.current_purpose())
        e.captions = captions
        await e._catchup()
        self.only(p)
        self.assertEqual(scopes, ["catchup"])
        self.assertNotIn("undelivered", m)

    async def test_04_group_catchup_is_distinct_from_reply(self):
        p = self.routed("catchup")
        c = self.group()
        m = await self.group_message(c)
        m["left"] = True
        await c.turn("catchup", [m["id"]])
        self.only(p)

    async def test_05_proactive_uses_selected_provider(self):
        p = self.routed("proactive")
        e = self.engine()
        e.history.append(e.store.new_msg("her", "昨天那件事我还记着", ts=F.NOW - 6 * F.HOUR))
        e._should_reach_out = lambda now: True
        await e._maybe_reach_out(F.NOW)
        self.only(p)

    async def test_06_thought_checks_selected_tool_capability(self):
        p = self.routed("thought")
        p.script = [F.Response(calls=[("let_go", {})])]
        e = self.engine()
        e.history.append(e.store.new_msg("her", "那件事还有后续吗", ts=F.NOW - F.HOUR))
        e.store.thought = {"id": "thought", "topic": "后续", "tier": "soon", "createdAt": F.NOW - F.HOUR,
                           "dueAt": F.NOW - 1, "waited": False, "planted": True}
        await e._maybe_recheck(F.NOW)
        self.only(p)
        self.assertIsNone(e.store.thought)

    async def test_07_standalone_private_farewell_uses_farewell_route(self):
        p = self.routed("farewell", tools=False)
        e = self.engine()
        e.history.append(e.store.new_msg("her", "刚才聊得很开心", ts=F.NOW - F.MIN))
        with patch.object(F.E, "offline_after", return_value=F.NOW + 5 * F.MIN):
            await e._maybe_farewell(F.NOW)
        self.only(p)

    async def test_08_group_farewell_uses_farewell_route(self):
        p = self.routed("farewell", tools=False)
        c = self.group()
        m = await self.group_message(c)
        await c.turn("farewell", [m["id"]], "现在告别")
        self.only(p)

    async def closing(self, group):
        p = self.routed("closing", tools=False)
        target = self.group() if group else self.engine()
        m = await (self.group_message(target) if group else self.private_message(target))
        m.update(farewellFollowup=True)
        m["left" if group else "undelivered"] = True
        state = {"used": False}
        with patch.object(target, "_leaving", return_value=state):
            await target._farewell_reply()
        self.only(p)
        self.assertEqual(p.requests[0]["tools"], [])

    async def test_09_private_final_closing_has_own_route(self):
        await self.closing(False)

    async def test_10_group_final_closing_has_own_route(self):
        await self.closing(True)

    async def test_11_group_offline_short_reply_uses_own_route(self):
        p = self.routed("offline_reply", tools=False)
        c = self.group()
        m = await self.group_message(c, primary=True)
        offline_state = F.S.HerState(False, "base", "sleep", 3.0, 0)
        with patch.object(F.G, "state_at", return_value=offline_state), \
             patch.object(F.module("offline"), "segment_key", return_value="rest-route"):
            await c._mission_ack_turn(m, offline_state, F.NOW, "rest-route")
        self.only(p)
        self.assertEqual(p.requests[0]["tools"], [])

    async def judge_route(self, mode):
        p = self.routed("window_judge" if mode == "window" else "look_judge", image=True)
        p.script = [F.Response(calls=[("judge", {"reply": False, "interest": .1, "reason": "没有要接的", "focus": []})])]
        c = self.group()
        m = await self.group_message(c)
        m["attachment"] = {"kind": "image", "file": "fake.png"}
        flags = []
        async def captions(gid, rows, saved, deadline, *, force_text=False):
            flags.append((MR.current_purpose(), force_text))
            rows[-1]["attachment"]["caption"] = "判断只会收到文字图注"
            saved()
        self.hub.captions = captions
        await c.assess([m], 0, mode, [m])
        self.only(p)
        self.assertEqual(flags, [("window_judge" if mode == "window" else "look_judge", True)])
        self.assertIn("判断只会收到文字图注", self.joined(p))

    async def test_12_window_judge_forces_text_caption_even_with_vision(self):
        await self.judge_route("window")

    async def test_13_look_judge_forces_text_caption_even_with_vision(self):
        await self.judge_route("look")

    async def memory_route(self, group, purpose):
        p = self.routed(purpose, tools=purpose != "memory_haze")
        c = self.group() if group else self.engine()
        c.mem_busy = False
        if group:
            c.schedule_history = lambda: None
        m = await (self.group_message(c) if group else self.private_message(c))
        m["read" if group else "seen"] = True
        m["attachment"] = {"kind": "image", "file": "fake.png"}
        flags = []
        async def captions(*args, force_text=False):
            flags.append((MR.current_purpose(), force_text))
            rows, saved = args[1:3] if group else args[:2]
            rows[-1]["attachment"]["caption"] = "整理必须看到的图注"
            saved()
        if group:
            self.hub.captions = captions
        else:
            c.captions = captions
        async def consolidate(chat, session, now, *args, **kwargs):
            self.assertIn("caption", session[-1]["attachment"])
            await chat("整理", [{"role": "user", "content": "整理材料"}])
            return [] if group else "skipped"
        async def haze(chat, now, **kwargs):
            await chat("合并印象", [{"role": "user", "content": "旧事"}])
            return True
        async def tags(chat, now):
            await chat("话题", [{"role": "user", "content": "旧事"}])
            return True
        with patch.object(c.memory, "pending_session", return_value=[m] if purpose == "memory" else None), \
             patch.object(c.memory, "pending_haze", return_value={"month": "2025-01"} if purpose == "memory_haze" else None), \
             patch.object(c.memory, "pending_tags", return_value=[{"id": 1}] if purpose == "memory_tags" else []), \
             patch.object(c.memory, "consolidate_peer" if group else "consolidate", side_effect=consolidate), \
             patch.object(c.memory, "merge_haze", side_effect=haze), patch.object(c.memory, "tag_old", side_effect=tags):
            self.assertEqual(await c.run_memory(force=True), "整理好了")
        self.only(p)
        self.assertEqual(flags, [("memory", True)] if purpose == "memory" else [])

    async def test_14_private_memory_routes_independently_of_text_only_primary(self):
        await self.memory_route(False, "memory")

    async def test_15_group_memory_routes_independently_of_text_only_primary(self):
        await self.memory_route(True, "memory")

    async def test_16_private_haze_requires_no_tool_capability(self):
        await self.memory_route(False, "memory_haze")

    async def test_17_group_haze_requires_no_tool_capability(self):
        await self.memory_route(True, "memory_haze")

    async def test_18_private_tag_backfill_uses_own_tool_model(self):
        await self.memory_route(False, "memory_tags")

    async def test_19_group_tag_backfill_uses_own_tool_model(self):
        await self.memory_route(True, "memory_tags")

    async def test_20_memory_tool_gate_rejects_text_only_override_despite_capable_primary(self):
        self.main.provider_config["modalities"] = ["text", "tool_use"]
        p = self.routed("memory", tools=False)
        e = self.engine()
        e.mem_busy = False
        m = await self.private_message(e)
        with patch.object(e.memory, "pending_session", return_value=[m]):
            self.assertIsNone(await e.run_memory(force=True))
        self.assertEqual(p.requests + self.main.requests, [])

    async def test_21_summary_uses_selected_text_model_and_archived_caption_only_for_seen_images(self):
        p = self.routed("summary", tools=False)
        p.script = [F.Response('{"remember":false,"summary":""}')]
        self.main = None
        c = self.group()
        c.mem_busy = False
        c.schedule_history = lambda: None
        seen = {"id": "old-image", "role": "user", "text": "看这个", "ts": F.NOW - F.HOUR,
                "read": True, "uid": F.PEER, "name": "旧用户", "attachment": {"kind": "image", "file": "old.png"},
                "quote": {"role": "her", "text": "之前的话", "missing": False}}
        unseen = {**copy.deepcopy(seen), "id": "never-saw-image", "attachment": {"kind": "image", "file": "other.png"}}
        c.round_memory.enqueue([seen, unseen], image_ids=[seen["id"]])
        flags = []
        async def captions(gid, rows, saved, deadline, *, force_text=False):
            flags.append((MR.current_purpose(), force_text, [r["id"] for r in rows]))
            rows[0]["attachment"]["caption"] = "缓存已裁掉的蓝色小鸟"
            saved()
        self.hub.captions = captions
        self.assertTrue(await c.run_history())
        self.assertEqual(flags, [("summary", True, ["old-image"])])
        self.assertEqual(len(p.requests), 1)
        self.assertIn("缓存已裁掉的蓝色小鸟", self.joined(p))
        archive = {r["id"]: r for r in c.history_archive.messages}
        self.assertEqual(archive[seen["id"]]["quote"], seen["quote"])
        self.assertNotIn("caption", archive[unseen["id"]]["attachment"])
        self.assertEqual(c.msgs, [], "do not resurrect trimmed entries into the receive buffer")

    async def test_22_summary_schedule_is_not_blocked_by_absent_primary(self):
        self.routed("summary", tools=False)
        self.main = None
        c = self.group()
        c.mem_busy = False
        self.hub.round_llm_for = lambda gid: self.llm
        c.round_memory.enqueue([{"id": "row", "role": "user", "read": True, "text": "有意义的新事项", "ts": F.NOW}])
        spawned = []
        def spawn(coro):
            spawned.append(coro)
            coro.close()
        with patch.object(c, "spawn", side_effect=spawn):
            c.schedule_history()
        self.assertEqual(len(spawned), 1)

    async def test_23_tool_continuation_keeps_provider_after_configuration_change(self):
        p = self.routed("private_reply")
        next_p = F.Provider()
        e = self.engine()
        e._recall_all = self.recall
        async def change_model():
            self.selected["private_reply"] = next_p
            return F.Response(calls=[("think", {"text": "想想"})])
        p.script = [change_model, F.Response("同一轮仍由原来的模型接着回答。")]
        await self.private_message(e)
        await e.run_turn()
        self.only(p, 2)
        self.assertEqual(next_p.requests, [])
        await self.private_message(e, "接下来呢")
        await e.run_turn()
        self.assertEqual(len(next_p.requests), 1, "a new business round must get a fresh provider snapshot")

    async def test_24_parallel_sessions_keep_independent_purposes(self):
        a, b = self.routed("private_reply"), self.routed("group_reply")
        barrier, release = asyncio.Event(), asyncio.Event()
        async def pause():
            barrier.set()
            await release.wait()
            return F.no_reply()
        a.script = [pause]
        b.script = [F.no_reply()]
        e, c = self.engine(), self.group()
        await self.private_message(e)
        m = await self.group_message(c)
        task = asyncio.create_task(e.run_turn())
        self.tasks.append(task)
        await asyncio.wait_for(barrier.wait(), 2)
        await c.turn("called", [m["id"]])
        release.set()
        await task
        self.assertEqual((len(a.requests), len(b.requests)), (1, 1))
        self.assertEqual(self.main.requests, [])
        self.assertEqual(MR.current_purpose(), "")

    async def test_25_caption_save_merges_only_caption_without_resurrecting_removed_rows(self):
        c = self.group()
        m = await self.group_message(c)
        m.update(read=True, attachment={"kind": "image", "file": "a.png"}, quote={"role": "her", "text": "旧话", "missing": False})
        c.history_archive.sync([m])
        view = copy.deepcopy(m)
        view.update(text="被截断的假原文", quote={"text": "不能覆盖"})
        view["attachment"]["caption"] = "图注"
        c._save_captions([view])
        row = c.history_archive.messages[0]
        self.assertEqual((row["text"], row["quote"]), (m["text"], m["quote"]))
        self.assertEqual(row["attachment"]["caption"], "图注")
        c.history_archive.remove_ids([m["id"]])
        c.msgs.clear()
        c._save_captions([view])
        self.assertEqual(c.history_archive.messages, [])

    async def test_26_old_caption_callbacks_still_work_and_real_typeerror_is_not_hidden(self):
        e, c = self.engine(), self.group()
        calls = []
        async def private_old(rows, saved, deadline):
            calls.append("private")
        async def group_old(gid, rows, saved, deadline):
            calls.append("group")
        e.captions, self.hub.captions = private_old, group_old
        await e._see_images([], force_text=True)
        await c._see_images([], force_text=True)
        self.assertEqual(calls, ["private", "group"])
        async def broken(rows, saved, deadline, *, force_text=False):
            raise TypeError("actual callback failure")
        e.captions = broken
        with self.assertRaisesRegex(TypeError, "actual callback failure"):
            await e._see_images([], force_text=True)

    async def test_27_private_offline_short_reply_routes_without_consuming_backlog(self):
        p = self.routed("offline_reply", tools=False)
        e = self.engine()
        self.opts["offline_reply"] = True
        def state(t):
            return F.S.HerState(t >= F.NOW + F.HOUR, "base", "sleep", 12, 0)
        with patch.object(F.E, "state_at", state):
            m = await self.private_message(e)
            await asyncio.wait_for(e._offline_notice_task, 2)
        self.only(p)
        self.assertTrue(m["undelivered"])
        self.assertFalse(m.get("answered"))

    async def test_28_longterm_image_preparation_skips_originals_already_replaced_by_complete_summary(self):
        c = self.group()
        a = {"id": "a", "role": "user", "read": True, "ts": F.NOW, "text": "这张图",
             "attachment": {"kind": "image", "file": "a.png"}}
        b = {**copy.deepcopy(a), "id": "b", "attachment": {"kind": "image", "file": "b.png"}}
        key = c.round_memory.enqueue([a])
        record = next(r for r in c.round_memory._records if r["id"] == key)
        record.update(state="done", summary="我记得图片上是蓝色小鸟。")
        self.assertEqual(c.round_memory.memory_raw_messages([a, b]), [b])
        self.assertIn("蓝色小鸟", c.round_memory.memory_note([a, b]))
        record["partial"] = True
        self.assertEqual(c.round_memory.memory_raw_messages([a, b]), [a, b])

    async def test_29_cancellation_during_provider_call_restores_scope_and_next_round_can_change_model(self):
        p = self.routed("private_reply")
        started = asyncio.Event()
        async def never_finishes():
            started.set()
            await asyncio.Event().wait()
        p.script = [never_finishes]
        e = self.engine()
        await self.private_message(e)
        task = asyncio.create_task(e.run_turn())
        self.tasks.append(task)
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(MR.current_purpose(), "")
        next_p = self.routed("private_reply")
        next_p.script = [F.no_reply()]
        await self.private_message(e, "下一条独立消息")
        await e.run_turn()
        self.assertEqual(len(next_p.requests), 1)

    async def test_30_summary_removed_during_caption_preparation_does_not_call_model(self):
        c = self.group()
        row = {"id": "drop", "role": "user", "read": True, "text": "这轮已被长期整理", "ts": F.NOW}
        c.round_memory.enqueue([row])
        started, release = asyncio.Event(), asyncio.Event()
        called = []
        async def prepare(rows, image_ids):
            started.set()
            await release.wait()
            return True
        async def chat(system, messages):
            called.append(messages)
            return F.Obj(ok=True, text='{"summary":"不该调用"}')
        task = asyncio.create_task(c.round_memory.summarize(chat, prepare=prepare))
        self.tasks.append(task)
        await asyncio.wait_for(started.wait(), 2)
        c.round_memory.drop_consolidated(row["id"], row["ts"])
        release.set()
        self.assertFalse(await task)
        self.assertEqual(called, [])

    async def test_31_window_identity_card_uses_only_fresh_direct_message(self):
        p = self.routed("window_judge")
        c = self.group()
        old = {"id": "old", "role": "user", "read": True, "text": "你是谁？", "ts": F.NOW - F.HOUR,
               "uid": F.PEER, "name": "群友"}
        new = {"id": "new", "role": "user", "text": "晚安", "ts": F.NOW,
               "uid": F.PEER, "name": "群友", "quote": {"role": "user", "text": "你是谁？"}}
        async def assess():
            p.script = [F.Response(calls=[("judge", {"reply": False, "interest": .1, "reason": "自然结束", "focus": []})])]
            return await c.assess([old, new], 0, "window", [old, new])
        await assess()
        self.assertNotIn("【按需查看的身份资料】", p.requests[-1]["system"])
        new["text"] = "你是谁？"
        await assess()
        self.assertIn("【按需查看的身份资料】", p.requests[-1]["system"])
        self.assertNotIn("银狼", p.requests[-1]["system"])

    async def read_more_identity(self, question_is_old):
        p = self.routed("look_judge")
        c = self.group()
        pool = [{"id": f"r{i}", "role": "user", "text": f"正在聊的小事{i}", "ts": F.NOW - 40 + i,
                 "uid": F.PEER, "name": "群友"} for i in range(40)]
        pool[25]["text"] = "你是谁？"
        if question_is_old:
            pool[25]["read"] = True
            pool[26]["quote"] = {"role": "user", "text": "你是谁？"}
        reached, release = asyncio.Event(), asyncio.Event()
        async def read_more():
            reached.set()
            await release.wait()
            return F.Response(calls=[("read_more", {})])
        p.script = [read_more, F.Response(calls=[("judge", {"reply": False, "interest": .1, "reason": "看完了", "focus": []})])]
        task = asyncio.create_task(c.assess(pool, 35, "look", pool[-12:]))
        self.tasks.append(task)
        await asyncio.wait_for(reached.wait(), 2)
        self.assertNotIn("【按需查看的身份资料】", p.requests[0]["system"])
        release.set()
        await task
        self.assertEqual(len(p.requests), 2)
        if question_is_old:
            self.assertNotIn("【按需查看的身份资料】", p.requests[1]["system"])
        else:
            self.assertIn("【按需查看的身份资料】", p.requests[1]["system"])

    async def test_32_look_read_more_gets_identity_only_after_reading_new_question(self):
        await self.read_more_identity(False)

    async def test_33_look_read_more_does_not_reopen_identity_from_old_read_or_quote(self):
        await self.read_more_identity(True)

    async def test_34_longterm_waiting_existing_caption_task_refreshes_its_own_archive_copy(self):
        c = self.group()
        c.mem_busy = False
        c.schedule_history = lambda: None
        m = {"id": "archived-image", "role": "user", "read": True, "ts": F.NOW - 4 * F.HOUR,
             "text": "现在看看这张", "uid": F.PEER, "name": "测试用户",
             "quote": {"role": "her", "text": "准确的旧话", "missing": False},
             "attachment": {"kind": "image", "file": "existing.png"}}
        c.history_archive.sync([m])
        first = c.history_archive.messages[0]
        images = Path(c.store.images)
        images.mkdir(parents=True, exist_ok=True)
        (images / "existing.png").write_bytes(b"only-a-fake-provider-reads-this")
        captioner = F.module("caption").Captioner(str(images), F.LOG, self.events)
        caption_model = F.Provider()
        started, ready, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def caption_result():
            started.set()
            await release.wait()
            return F.Response("一只蓝色小鸟")
        caption_model.script = [caption_result]
        captioner.start(caption_model, first["attachment"], lambda: c._save_captions([first]))
        try:
            await asyncio.wait_for(started.wait(), 2)
            async def captions(gid, rows, saved, deadline, *, force_text=False):
                ready.set()
                await captioner.ready(caption_model, rows, saved, deadline)
            self.hub.captions = captions
            self.routed("memory")
            observed = []
            async def consolidate(chat, session, *args, **kwargs):
                observed.append(copy.deepcopy(session[0]))
                return []
            with patch.object(c.memory, "consolidate_peer", side_effect=consolidate):
                task = asyncio.create_task(c.run_memory(force=True))
                self.tasks.append(task)
                await asyncio.wait_for(ready.wait(), 2)
                release.set()
                self.assertEqual(await task, "整理好了")
            self.assertEqual(len(caption_model.requests), 1, "reuse the in-progress caption rather than call it again")
            self.assertEqual(observed[0]["attachment"]["caption"], "一只蓝色小鸟")
            self.assertEqual(observed[0]["quote"], m["quote"])
            self.assertEqual((observed[0]["text"], observed[0]["read"]), (m["text"], True))
            self.assertEqual(c.msgs, [], "do not restore the archived message to the receive buffer")
        finally:
            release.set()
            captioner.stop()


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RouteTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.testsRun - len(result.failures) - len(result.errors)
    print(f"route_scope240 {passed}/{result.testsRun}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
