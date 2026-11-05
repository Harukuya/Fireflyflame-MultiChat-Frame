"""Quoted replies: real core, fake provider/platform, temporary data only.

python -I -B quote239_test.py <plugin/framework root> [reference] [temporary root]
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, PropertyMock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

spec = importlib.util.spec_from_file_location("_quote239_fixture", Path(__file__).with_name("no_reply233_test.py"))
F = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = F
spec.loader.exec_module(F)
try:
    Q = F.module("quotes")
except ModuleNotFoundError:
    Q = None
H, R, M, J, FW, RP = [F.module(n) for n in ("history", "rounds", "memory", "judge", "farewell", "recent")]
OLD, NEW = "被准确引用的旧话蓝色小屋", "这次回复正文橙色风铃"
OLD_TS = F.NOW - 3 * 24 * F.HOUR


class QuoteTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = F.NoReplyTest.asyncSetUp
    asyncTearDown = F.NoReplyTest.asyncTearDown
    recall = F.NoReplyTest.recall
    engine = F.NoReplyTest.engine
    group = F.NoReplyTest.group

    def setUp(self):
        self.assertIsNotNone(Q, "quoted reply support must exist")

    @staticmethod
    def quoted(**fields):
        return {"qid": "original-id", "role": "her", "text": OLD, "ts": OLD_TS, **fields}

    @staticmethod
    def row(text=OLD, **fields):
        return {"id": "local-old", "qid": "original-id", "role": "her", "text": text, "ts": OLD_TS, **fields}

    @staticmethod
    def joined(turns):
        return "\n".join(m["content"] if isinstance(m["content"], str) else "\n".join(
            b.get("text", "") for b in m["content"] if isinstance(b, dict)) for m in turns)

    def request_text(self, index=-1):
        self.assertTrue(self.provider.requests, "must reach fake Provider via the actual Llm")
        return self.joined(self.provider.requests[index]["messages"])

    async def add_private(self, e, text=NEW, quote=None):
        await e.on_user_message(text, [], "incoming-id", quote=self.quoted() if quote is None else quote)
        return e.history[-1]

    async def add_group(self, c, text=NEW, quote=None, called=True):
        await c.on_message(F.PEER, "同名用户", text, [], "incoming-id", called,
                           quote=self.quoted() if quote is None else quote)
        return c.msgs[-1]

    async def test_01_normalize_is_bounded_one_level_copy(self):
        raw = self.quoted(text="字" * 2100, quote={"text": "nested"}, path="secret-path", url="secret-url",
                          attachment={"file": "secret-file"}, sent=True)
        got = Q.normalize_quote(raw)
        self.assertEqual(len(got["text"]), 2000)
        self.assertTrue(got["truncated"])
        self.assertEqual(set(got), {"qid", "role", "text", "ts", "missing", "truncated"})
        raw["text"] = "later mutation"
        self.assertEqual(got["text"], "字" * 2000)

    async def test_02_malformed_timestamps_and_ids_never_crash(self):
        for ts in [True, None, "123", float("nan"), float("inf"), -1, 0, 10 ** 1000]:
            with self.subTest(ts_type=type(ts).__name__):
                got = Q.normalize_quote(self.quoted(ts=ts))
                self.assertNotIn("ts", got)
                self.assertIn("原消息时间未提供", Q.render_quote({"quote": got}))
        for qid in [True, {}, [], "long" * 200, "id with space"]:
            self.assertNotIn("qid", Q.normalize_quote(self.quoted(qid=qid)))
        self.assertIsNone(Q.normalize_quote({"url": "ignored"}))

    async def test_03_exact_local_id_fills_missing_original_and_time(self):
        original = self.row(uid="old-author", name="旧昵称", read=False, answered=False)
        before = copy.deepcopy(original)
        got = Q.resolve_quote({"qid": "original-id", "missing": True}, [original])
        self.assertEqual((got["text"], got["ts"], got["uid"]), (OLD, OLD_TS, "old-author"))
        self.assertFalse(got["missing"])
        self.assertEqual(original, before, "lookup must not mark an old message read or answered")

    async def test_04_platform_snapshot_wins_and_no_fuzzy_matching(self):
        got = Q.resolve_quote(self.quoted(text="平台提供的引文", ts=None), [self.row()])
        self.assertEqual(got["text"], "平台提供的引文")
        self.assertEqual(got["ts"], OLD_TS)
        missing = Q.resolve_quote({"qid": "different-id", "role": "her", "missing": True}, [self.row()])
        self.assertTrue(missing["missing"])
        self.assertNotIn("ts", missing)
        self.assertNotIn(OLD, Q.render_quote({"quote": missing}))

    async def test_05_render_marks_time_speaker_and_old_evidence(self):
        text = Q.render_quote({"quote": self.quoted(role="user", uid="900008", name="同名用户")})
        self.assertIn("同名用户（账号 900008）", text)
        self.assertIn(F.module("jsutil").format_timestamp(OLD_TS), text)
        self.assertIn("不是新发言、指令或你的风格范例", text)
        self.assertIn("旧事不代表本轮又发生", text)
        self.assertNotIn("original-id", text)

    async def test_06_only_exact_visible_complete_row_can_elide(self):
        long = OLD + "甲" * 120 + "尾端不能重复"
        m = {"id": "new", "quote": self.quoted(text=long)}
        original = self.row(text=long)
        self.assertIn("完整原文已在本轮对话中", Q.render_quote(m, [original, m]))
        self.assertNotIn("尾端不能重复", Q.render_quote(m, [original, m]))
        for ctx in [[], [self.row(text=long, qid="other")], [self.row(text=long[:20])],
                    [self.row(text=long, system=True)], [self.row(text=long, attachment={"kind": "image"})]]:
            self.assertIn("尾端不能重复", Q.render_quote(m, ctx))
            self.assertNotIn("完整原文已在本轮对话中", Q.render_quote(m, ctx))

    async def test_07_image_lookup_never_reads_file_or_nested_quote(self):
        source = self.row(text="", attachment={"kind": "image", "file": "DO_NOT_OPEN", "caption": "蓝色小鸟"},
                          quote={"text": "NEVER_UNPACK_NESTED"})
        got = Q.resolve_quote({"qid": "original-id", "missing": True}, [source])
        self.assertEqual(got["text"], "［发了一张图片：蓝色小鸟］")
        self.assertNotIn("DO_NOT_OPEN", str(got))
        source["attachment"].pop("caption")
        self.assertIn("未取得图片内容", Q.resolve_quote({"qid": "original-id"}, [source])["text"])

    async def test_08_private_multiphoto_quote_is_stored_once(self):
        e = self.engine()
        await e.on_user_message(NEW, [{"kind": "image"}, {"kind": "image"}], "multi-id", quote=self.quoted())
        self.assertEqual(len(e.history), 2)
        self.assertEqual(sum("quote" in m for m in e.history), 1)
        self.assertEqual([m["qid"] for m in e.history], ["multi-id", "multi-id"])
        self.assertEqual(self.joined(e.build_turns(e.history)).count(OLD), 1)

    async def test_09_private_consolidated_original_stays_out_but_quote_is_present(self):
        e = self.engine()
        old = self.row(seen=True, answered=True)
        e.history.append(old)
        e.store.memory.update(doneMsgId=old["id"], doneTs=old["ts"])
        fresh = await self.add_private(e)
        before = copy.deepcopy(old)
        self.provider.script = [F.no_reply()]
        await e.run_turn()
        text = self.request_text()
        self.assertIn(OLD, text)
        self.assertIn(NEW, text)
        self.assertEqual(text.count(OLD), 1)
        self.assertEqual(old, before)
        self.assertEqual(e.store.memory["doneMsgId"], old["id"])
        self.assertEqual([m["id"] for m in e.recent_snapshot()["rows"]], [fresh["id"]])

    async def test_10_private_window_elides_only_exact_qid(self):
        e = self.engine()
        text = OLD + "甲" * 120 + "精确原文末尾"
        e.history.append(self.row(text=text))
        await self.add_private(e, quote=self.quoted(text=text))
        rendered = self.joined(e.build_turns(e.history))
        self.assertEqual(rendered.count("精确原文末尾"), 1)
        self.assertIn("完整原文已在本轮对话中", rendered)
        e.history[0].pop("qid")
        rendered = self.joined(e.build_turns(e.history))
        self.assertEqual(rendered.count("精确原文末尾"), 2)
        self.assertNotIn("完整原文已在本轮对话中", rendered)

    async def test_11_quote_only_private_reaches_model(self):
        e = self.engine()
        m = await self.add_private(e, text="")
        self.provider.script = [F.no_reply()]
        await e.run_turn()
        self.assertIn(OLD, self.request_text())
        self.assertTrue(m.get("seen"))
        self.assertEqual(len(self.provider.requests), 1)

    async def test_12_other_person_quote_does_not_call_group(self):
        c = self.group()
        m = await self.add_group(c, called=False, quote=self.quoted(role="user", uid="900008", name="别人"))
        self.assertFalse(m["called"])
        await c.passive_turn()
        self.assertEqual(self.provider.requests, [])
        self.assertFalse(F.G._trivial({**m, "text": ""}))

    async def test_13_group_covered_original_not_reopened_or_repacked(self):
        c = self.group()
        old = self.row(read=True)
        c.msgs.append(old)
        c.round_memory.enqueue([old])
        c.round_memory._records[0].update(state="done", summary="先前已打包的简短记事")
        before_ids = set(c.round_memory.claimed_ids())
        m = await self.add_group(c)
        self.provider.script = [F.no_reply()]
        await c.turn("called", [m["id"]])
        text = self.request_text()
        self.assertEqual(text.count(OLD), 1)
        self.assertNotIn("完整原文已在本轮对话中", text)
        self.assertEqual(set(c.round_memory.claimed_ids()) - before_ids, {m["id"]})
        self.assertNotIn("answered", old)

    async def test_14_group_trimmed_archive_resolves_one_exact_original(self):
        c = self.group()
        old = self.row(read=True)
        c.history_archive.sync([old])
        m = await self.add_group(c, quote={"qid": "original-id", "missing": True})
        self.assertEqual(m["quote"]["text"], OLD)
        self.assertEqual(m["quote"]["ts"], OLD_TS)
        self.assertNotIn(old, c.msgs)
        self.assertEqual([r["id"] for r in c.history_archive.messages], [old["id"]])

    async def test_15_group_and_private_share_no_id_lookup(self):
        a, b = self.group(), self.group()
        a.history_archive.sync([self.row(read=True)])
        m = await self.add_group(b, quote={"qid": "original-id", "missing": True})
        self.assertTrue(m["quote"]["missing"])
        e = self.engine()
        p = await self.add_private(e, quote={"qid": "original-id", "missing": True})
        self.assertTrue(p["quote"]["missing"])

    async def test_16_judge_receives_quote_and_keeps_focus_numbering(self):
        c = self.group()
        m = await self.add_group(c, text="", called=False, quote=self.quoted(role="user", uid="900008", name="甲"))
        self.provider.script = [F.Response(calls=[("judge", {"reply": True, "interest": .8,
                                                            "reason": "承接引用", "focus": [1], "to": "某个人"})])]
        result = await J.judge(self.llm, "测试判断", "window", [m], 0)
        self.assertEqual(result["focus"], [m["id"]])
        self.assertEqual(result["read"], [m])
        text = self.request_text()
        self.assertIn(OLD, text)
        self.assertIn("甲（账号 900008）", text)
        self.assertIn("未附加文字", text)
        self.assertNotIn("［图片］", text)

    async def test_17_archive_and_restart_preserve_one_level_quote(self):
        c = self.group()
        m = await self.add_group(c)
        m["read"] = True
        m["quote"]["nested"] = {"file": "DO_NOT_KEEP"}
        c.history_archive.sync([m])
        h = H.History(c.store)
        self.assertEqual(len(h.messages), 1)
        row = h.messages[0]
        self.assertEqual(row["quote"]["text"], OLD)
        self.assertNotIn("DO_NOT_KEEP", str(row))
        self.assertIn(OLD, H._line(row))
        self.assertEqual(row["ts"], F.NOW)
        self.assertEqual(row["quote"]["ts"], OLD_TS)

    async def test_18_shared_pool_keeps_quote_inside_new_reply_only(self):
        e = self.engine()
        m = await self.add_private(e)
        self.provider.script = [F.no_reply()]
        await e.run_turn()
        snap = e.recent_snapshot()
        self.assertEqual(len(snap["rows"]), 1)
        self.assertEqual(snap["rows"][0]["id"], m["id"])
        self.assertEqual(snap["rows"][0]["quote"]["ts"], OLD_TS)
        unit = {"kind": "raw", "item": snap["rows"][0]}
        text = RP.RecentPool._unit_text(unit, "")
        self.assertIn(OLD, text)
        self.assertIn("引用旧消息", text)
        self.assertIn(NEW, text)

    async def test_19_round_summary_sees_quote_but_only_new_message_id(self):
        c = self.group()
        m = await self.add_group(c)
        m["read"] = True
        c.round_memory.enqueue([m])
        captured = []
        async def summarize(system, messages):
            captured.append((system, messages))
            return F.Obj(ok=True, text='{"summary":"我接住了对旧话的回应","topics":[],"anchors":[],"unresolved":[]}')
        await c.round_memory.summarize(summarize)
        self.assertTrue(captured)
        text = self.joined(captured[0][1])
        self.assertIn(OLD, text)
        self.assertIn(NEW, text)
        self.assertIn("旧事不代表本轮又发生", text)
        self.assertEqual(c.round_memory._records[0]["ids"], [m["id"]])
        self.assertEqual(c.round_memory._records[0]["start"], F.NOW)

    async def test_20_longterm_consolidation_preserves_reference_attribution(self):
        e = self.engine()
        m = await self.add_private(e, quote=self.quoted(role="user", uid="900008", name="另一个人"))
        captured = []
        async def consolidate(system, messages, **kwargs):
            captured.append(messages)
            return F.Obj(ok=False, calls=[], kind="scripted", detail="")
        await e.memory.consolidate(consolidate, [m], F.NOW)
        text = self.joined(captured[0])
        self.assertIn("另一个人（账号 900008）", text)
        self.assertIn(OLD, text)
        self.assertIn(NEW, text)
        self.assertIn("旧事不代表本轮又发生", text)
        self.assertIsNone(e.store.memory["doneMsgId"])

    async def test_21_quoted_author_is_not_a_new_scoring_participant(self):
        c = self.group()
        m = await self.add_group(c, quote=self.quoted(role="user", uid="900008", name="引文作者"))
        captured = []
        async def consolidate(system, messages, **kwargs):
            captured.append(messages)
            return F.Obj(ok=False, calls=[], kind="scripted", detail="")
        await c.memory.consolidate_peer(consolidate, [m], F.NOW, "测试群", F.PRIMARY, allow_read_only=True)
        text = self.joined(captured[0])
        roster, dialogue = text.split("【刚才的对话】", 1)
        self.assertNotIn("900008", roster)
        self.assertIn("900008", dialogue)
        self.assertIn(OLD, dialogue)

    async def test_22_trimmed_round_view_does_not_double_render_quote(self):
        row = self.row(text=NEW * 50, role="user", quote=self.quoted(text=OLD * 100))
        rows = R.RoundMemory._limited_messages([row], 30, 500)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("quote", rows[0])
        self.assertTrue(rows[0].get("_display_only"))
        self.assertLessEqual(len(H._line(rows[0])), 500)
        self.assertLessEqual(H._line(rows[0]).count("〔引用旧消息"), 1)

    async def test_23_farewell_long_quote_never_displaces_new_body(self):
        m = self.row(text=NEW, role="user", quote=self.quoted(text="长" * 2000))
        text = self.joined(FW.followup_turns([], [m]))
        self.assertIn(NEW, text)
        self.assertEqual(text.count("长"), 400)
        self.assertIn("引文节选", text)
        self.assertEqual(m["quote"]["text"], "长" * 2000)

    async def test_24_recalling_quoted_old_message_does_not_cut_new_reply(self):
        e = self.engine()
        old = self.row(role="user", seen=True, answered=True)
        e.history.append(old)
        m = await self.add_private(e, quote=self.quoted(role="user"))
        entered, release = asyncio.Event(), asyncio.Event()
        async def response():
            entered.set()
            await release.wait()
            return F.no_reply()
        self.provider.script = [response]
        task = asyncio.create_task(e.run_turn())
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 2)
        await e.on_recall("original-id")
        self.assertFalse(e.recall_cut)
        self.assertEqual(m["quote"]["text"], OLD)
        release.set()
        await task
        self.assertTrue(m.get("seen"))
        self.assertEqual(len(self.provider.requests), 1)

    async def test_25_recalling_new_quoting_message_still_cuts_round(self):
        e = self.engine()
        await self.add_private(e)
        entered, release = asyncio.Event(), asyncio.Event()
        async def response():
            entered.set()
            await release.wait()
            return F.Response("不应送出")
        self.provider.script = [response]
        task = asyncio.create_task(e.run_turn())
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 2)
        await e.on_recall("incoming-id")
        release.set()
        await task
        self.assertFalse(self.sender.sent)
        self.assertFalse(e._pending())

    async def test_26_private_backlog_quote_survives_consolidated_boundary(self):
        e = self.engine()
        old = self.row()
        e.history.append(old)
        e.store.memory.update(doneMsgId=old["id"], doneTs=old["ts"])
        m = await self.add_private(e)
        m["undelivered"] = True
        self.provider.script = [F.no_reply()]
        await e._catchup()
        self.assertIn(OLD, self.request_text())
        self.assertIn(NEW, self.request_text())
        self.assertFalse(m.get("undelivered"))
        self.assertEqual(e.store.memory["doneMsgId"], old["id"])

    async def test_27_archive_lookup_copies_only_the_exact_match(self):
        c = self.group()
        c.history_archive.sync([self.row(read=True), self.row(text="另一条", id="other", qid="other-id", read=True)])
        c._archive_read = lambda: True
        with patch.object(H.History, "messages", new_callable=PropertyMock,
                          side_effect=AssertionError("must not copy the whole archive for quote lookup")):
            m = await self.add_group(c, quote={"qid": "original-id", "missing": True})
        self.assertEqual(m["quote"]["text"], OLD)
        found = c.history_archive.lookup_qid("original-id", full=True)
        found["text"] = "mutated copy"
        self.assertEqual(c.history_archive.lookup_qid("original-id", full=True)["text"], OLD)
        legacy = c.history_archive.lookup_qid("original-id")
        if F.PKG == "firefly":
            self.assertEqual(set(legacy), {"id", "qid", "answered", "role"})
        else:
            self.assertEqual(legacy["text"], OLD, "framework keeps its existing full-row lookup contract")

    async def test_28_private_offline_short_reply_sees_quote_without_finishing_backlog(self):
        e = self.engine()
        self.opts["offline_reply"] = True
        def state(t):
            return F.S.HerState(t >= F.NOW + F.HOUR, "base", "sleep", 12, 0)
        self.provider.script = [F.Response("晚些时候我再认真接着回你。")]
        with patch.object(F.E, "state_at", state):
            m = await self.add_private(e)
            await asyncio.wait_for(e._offline_notice_task, 2)
        self.assertIn(OLD, self.request_text())
        self.assertIn(NEW, self.request_text())
        self.assertTrue(m.get("undelivered"))
        self.assertFalse(m.get("answered"))
        self.assertEqual(len(self.provider.requests), 1)

    async def test_29_group_offline_short_reply_sees_quote_without_opening_window(self):
        c = self.group()
        self.opts["offline_reply"] = True
        def state(t):
            return F.S.HerState(t >= F.NOW + F.HOUR, "mission", "mission", 12, 0)
        self.provider.script = [F.Response("我记得这句话，忙完再接着说。")]
        with patch.object(F.G, "state_at", state):
            await c.on_message(F.PRIMARY, "测试联系人", NEW, [], "incoming-id", True, quote=self.quoted())
        self.assertIn(OLD, self.request_text())
        self.assertIn(NEW, self.request_text())
        m = next(m for m in c.msgs if m.get("qid") == "incoming-id")
        self.assertTrue(m.get("left"))
        self.assertFalse(m.get("answered"))
        self.assertFalse(c.store.extra.get("windowUntil"))

    async def test_30_group_catchup_keeps_quote_when_old_original_already_packed(self):
        c = self.group()
        old = self.row(read=True)
        c.msgs.append(old)
        c.round_memory.enqueue([old])
        c.round_memory._records[0].update(state="done", summary="此前那轮的记事")
        m = await self.add_group(c)
        m["left"] = True
        self.provider.script = [F.no_reply()]
        await c.catchup()
        self.assertEqual(self.request_text().count(OLD), 1)
        self.assertIn(NEW, self.request_text())
        self.assertFalse(m.get("left"))

    async def test_31_farewell_does_not_publish_truncated_quote_as_fully_read(self):
        e = self.engine()
        FW.begin(e.store, F.NOW - 10_000, F.NOW + F.MIN)
        quote = self.quoted(text=OLD + "长" * 1500)
        m = await self.add_private(e, quote=quote)
        self.provider.script = [F.Response("这句我明白，不过我现在确实得先走了。")]
        await e._farewell_reply()
        text = self.request_text()
        self.assertIn(OLD, text)
        self.assertIn(NEW, text)
        self.assertIn("引文节选", text)
        self.assertFalse(m.get("readForSharing"))
        self.assertEqual(m["quote"]["text"], quote["text"])
        self.assertTrue(m.get("answered"))

    async def test_32_group_old_quote_recall_is_not_recall_of_current_focus(self):
        c = self.group()
        old = self.row(role="user", uid="900008", name="旧作者", read=True, answered=True)
        c.msgs.append(old)
        c.history_archive.sync([old])
        m = await self.add_group(c, quote=self.quoted(role="user", uid="900008", name="旧作者"))
        entered, release = asyncio.Event(), asyncio.Event()
        async def response():
            entered.set()
            await release.wait()
            return F.no_reply()
        self.provider.script = [response]
        task = asyncio.create_task(c.turn("called", [m["id"]]))
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 2)
        await c.on_recall("original-id")
        self.assertFalse(c._focus_recalled([m["id"]]))
        release.set()
        await task
        self.assertTrue(m.get("read"))
        self.assertEqual(m["quote"]["text"], OLD)
        self.assertEqual(len(self.provider.requests), 1)

    async def trimmed_look(self, shared):
        c = self.group()
        c.schedule_history = lambda: None
        long = self.row(text="本条正文" * 1800, id="old-long", qid="old-platform", role="user",
                        uid="reply-author", name="本条作者", read=True, shown=True, ts=F.NOW - 10_000,
                        quote=self.quoted(qid="source-platform", role="user", uid="quoted-author", name="被引作者",
                                          text="开端" + "填" * 750 + "猫耳星芒" + "填" * 1000))
        c.msgs.append(long)
        c.round_memory.enqueue([long])
        c.round_memory._records[0]["gaveUp"] = True  # Failed summary remains a raw fallback.
        if shared:
            c.round_memory.remember_shared([long])
        saved = copy.deepcopy(c.round_memory._shared.get(long["id"]))
        later = [self.row(text="之后已成包的聊天" + str(i), id="later-" + str(i), qid="later-q" + str(i),
                          role="user", uid=F.PEER, name="后来的作者", read=True, shown=True,
                          ts=F.NOW - 9000 + i) for i in range(405)]
        c.msgs.extend(later)
        for start in range(0, len(later), 30):
            c.round_memory.enqueue(later[start:start + 30])
            c.round_memory._records[-1].update(state="done", summary="这一段已成包")
        await c.on_message(F.PEER, "新作者", "眼前的全新消息", [], "new-q", False)
        new = c.msgs[-1]
        self.assertEqual(len(c.msgs), 400, "exercise the real BUFFER_MAX trimming")
        self.assertFalse(any(m["id"] == long["id"] for m in c.msgs))
        entered, release = asyncio.Event(), asyncio.Event()
        async def read_more():
            entered.set()
            await release.wait()
            return F.Response(calls=[("read_more", {})])
        self.provider.script = [read_more, F.Response(calls=[("judge", {"reply": False, "interest": .2,
                                          "reason": "只看这一次", "focus": [], "remember": True})])]
        before_energy = self.her.energy["value"]
        task = asyncio.create_task(c.look(lambda gid: True))
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 2)
        self.assertNotIn("猫耳星芒", self.request_text())
        release.set()
        await task
        self.assertEqual(len(self.provider.requests), 2)
        self.assertIn("猫耳星芒", self.request_text())
        self.assertIn("被引作者", self.request_text())
        archived = next(m for m in c.history_archive.messages if m["id"] == long["id"])
        self.assertEqual(archived["text"], long["text"])
        self.assertEqual(archived["quote"], Q.normalize_quote(long["quote"]))
        self.assertEqual(c.round_memory._shared.get(long["id"]), saved)
        self.assertEqual(c.store.extra["look"]["count"], 1)
        self.assertEqual(self.her.energy["value"], before_energy - 1)
        self.assertTrue(new["read"])
        self.assertIn(new["id"], c.round_memory._shared)
        if shared:
            display = RP.RecentPool._unit_text({"kind": "raw", "item": saved}, "猫耳星芒")
            self.assertIn("猫耳星芒", display)
            self.assertIn("被引作者", display)
            self.assertIn("引用旧消息", display)

    async def test_33_trimmed_old_quote_read_more_preserves_archive_and_existing_shared_snapshot(self):
        await self.trimmed_look(shared=True)

    async def test_34_trimmed_old_quote_read_more_does_not_invent_full_shared_read(self):
        await self.trimmed_look(shared=False)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(QuoteTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.testsRun - len(result.failures) - len(result.errors)
    print(f"quote239 {passed}/{result.testsRun}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
