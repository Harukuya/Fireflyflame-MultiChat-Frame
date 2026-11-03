"""接话判断：本轮原话＋本会话此前两包，不用八条硬退出；三份源码通用。

python -I -B window235_test.py <源码根目录> [<复刻包目录>] [<临时目录>]
只用临时存盘、假模型、可控时间，不加载插件入口或读取真实宿主配置。
"""

import asyncio
import copy
import importlib
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve()
OUTPUT = Path(sys.argv[-1]).resolve() if len(sys.argv) >= 3 else Path(tempfile.gettempdir())
OUTPUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
PACKAGE = "multichat" if (ROOT / "multichat").is_dir() else "firefly"
G = importlib.import_module(PACKAGE + ".group")
R = importlib.import_module(PACKAGE + ".rounds")
S = importlib.import_module(PACKAGE + ".store")
M = importlib.import_module(PACKAGE + ".memory")
L = importlib.import_module(PACKAGE + ".llm")
E = importlib.import_module(PACKAGE + ".energy")
EVENTS = importlib.import_module(PACKAGE + ".events")
NOW = 1793714400000


class FakeJudge:
    def __init__(self):
        self.calls = []
        self.reply = False
        self.wait = None

    def supports(self, modality):
        return False

    async def chat_with_retry(self, system, messages, tools=None, tool_choice=None):
        self.calls.append((system, copy.deepcopy(messages), tools, tool_choice))
        if self.wait:
            await self.wait()
        count = len(messages[0]["content"].splitlines())
        return L.ChatResult(ok=True, calls=[{"name": "judge", "input": {
            "reply": self.reply, "interest": 0.8 if self.reply else 0,
            "reason": "接着刚才的话" if self.reply else "听完了", "focus": [count] if self.reply else [], "to": "话题"}}])


class WindowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="window235-", dir=OUTPUT)
        self.addCleanup(self.temp.cleanup)
        self.now = NOW
        for module in (G, R):
            p = patch.object(module, "now_ms", lambda: self.now)
            p.start()
            self.addCleanup(p.stop)
        state = SimpleNamespace(online=True, location="base", activity="night", her_hour=22, offset_hours=2)
        for name, value in (("state_at", lambda t: state), ("online_since", lambda t: NOW - 3600000)):
            p = patch.object(G, name, value)
            p.start()
            self.addCleanup(p.stop)
        if PACKAGE == "multichat":
            profile = importlib.import_module("multichat.profile")
            context = profile.use(profile.prepare({"character_name": "测试伙伴", "primary_user_name": "测试联系人",
                                                  "schedule_mode": "always"}, ROOT / "prompts"))
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        self.log = logging.getLogger("window235")
        self.her = S.Her(str(Path(self.temp.name) / "shared"))
        self.her.energy = {**E.fresh_energy(NOW), "value": 85}
        self.judge = FakeJudge()
        self.shared_calls = []
        def shared(*args, **kwargs):
            self.shared_calls.append(args)
            return "不该出现的其他群资料"
        self.hub = SimpleNamespace(her=self.her, group_name=lambda gid: "测试群" + gid,
                                   embedder=None, trailblazer="", closed=False, captions=None,
                                   options=lambda: {}, proactive_on=lambda gid: True,
                                   events=EVENTS.Events(self.temp.name, self.log), llm=self.judge,
                                   judge_llm=self.judge, round_llm_for=None, recent_context=shared,
                                   intimacy=SimpleNamespace(brief=lambda *a: "熟悉的群友"))
        self.chat = self.group("one")
        self.seq = 0

    def group(self, gid):
        store = S.Store(str(Path(self.temp.name) / gid), self.her)
        store.extra["flags"] = 2
        memory = M.Memory(store, SimpleNamespace(ready=False), self.log)
        chat = G.GroupChat(gid, self.hub, store, memory)
        chat.schedule_history = lambda: None
        chat.store.extra.update(windowUntil=NOW + 300000, windowMisses=0)
        return chat

    def row(self, text, *, role="user", read=False, **extra):
        self.seq += 1
        row = {"id": str(self.seq), "qid": "q" + str(self.seq), "role": role, "text": text,
               "ts": NOW - 100000 + self.seq * 10, "read": read,
               "uid": "90001" if role == "user" else "", "name": "测试群友", **extra}
        self.chat.msgs.append(row)
        return row

    def record(self, text, state="done", *, open=False, rows=None, cold=False):
        rows = rows or [self.row("原文-" + text, read=True)]
        memory = self.chat.round_memory
        memory.archive.sync(rows)
        record = memory._record(rows, "pack-" + str(len(memory._records)), None, "")
        record.update(state=state, summary=text if state == "done" else "", open=open, cold=cold,
                      batch=True, openedAt=NOW, lastActivityAt=NOW)
        memory._records.append(record)
        return record

    def body(self):
        return self.judge.calls[-1][0] + "\n" + self.judge.calls[-1][1][0]["content"]

    async def ask(self):
        new = [m for m in self.chat.msgs if m["role"] == "user" and not m.get("read")]
        return await self.chat.assess(self.chat.msgs, 0, "window", new)

    async def test_entire_current_multi_bubble_reply_reaches_judge(self):
        rows = [self.row("明天还是后天一起看？", role="her", read=True)]
        rows += [self.row("她的后续气泡" + str(i), role="her", read=True) for i in range(6)]
        self.record("未成包", "queued", open=True, rows=rows)
        self.row("明天吧")
        await self.chat.window_turn()
        self.assertEqual(len(self.judge.calls), 1)
        for row in rows:
            self.assertIn(row["text"], self.body())
        self.assertIn("明天吧", self.body())

    async def test_only_previous_two_local_completed_packages(self):
        for text in ("最早包应退出", "上一包保留", "最近包保留"):
            self.record(text)
        self.row("现在接着聊")
        await self.ask()
        body = self.body()
        self.assertNotIn("最早包应退出", body)
        self.assertIn("上一包保留", body)
        self.assertIn("最近包保留", body)
        self.assertLess(body.index("上一包保留"), body.index("最近包保留"))
        self.assertNotIn("原文-", body)
        self.assertFalse(self.shared_calls)

    async def test_no_random_retrieval_or_background_call(self):
        self.record("旧约定")
        self.row("我上次的具体约定呢")
        async def forbidden(*args, **kwargs):
            raise AssertionError("窗口判断不能检索、查询背景或额外总结")
        self.chat._history_note = self.chat.background = forbidden
        self.chat.round_memory.select = self.chat.round_memory.summarize = forbidden
        await self.ask()
        self.assertEqual(len(self.judge.calls), 1)

    async def test_finished_package_replaces_raw_even_after_quiet_seal(self):
        row = self.row("只在原话里的一整段问题", role="her", read=True)
        self.record("我问他明天还是后天", rows=[row])
        self.now += 91000
        self.row("明天")
        await self.ask()
        self.assertIn("我问他明天还是后天", self.body())
        self.assertNotIn(row["text"], self.body())
        self.assertIn("明天", self.body())

    async def test_skipped_round_is_not_revived_or_fabricated(self):
        self.record("较早的有效记事")
        skipped = self.record("决定不记的寒暄", "skipped")
        self.row("新的问题")
        result = await self.ask()
        self.assertNotIn("决定不记的寒暄", self.body())
        self.assertIn("较早的有效记事", self.body())
        self.assertFalse(set(skipped["ids"]).intersection(m["id"] for m in result["read"]))

    async def test_older_failed_pack_not_restored_beside_current_round(self):
        self.record("很早失败待重试", "queued")
        self.record("中间已经完成")
        current = self.row("眼前还没打包的问话", role="her", read=True)
        self.record("当前", "queued", rows=[current])
        self.row("我在回应眼前的问话")
        await self.ask()
        self.assertNotIn("很早失败待重试", self.body())
        self.assertIn(current["text"], self.body())
        self.assertIn("中间已经完成", self.body())

    async def test_open_round_survives_later_mechanical_noise_skip(self):
        current = self.row("这个问题你怎么看", role="her", read=True)
        self.record("当前", "queued", open=True, rows=[current])
        self.record("哈哈哈哈", "skipped")
        self.row("我想补充两点")
        await self.ask()
        self.assertIn(current["text"], self.body())
        self.assertNotIn("原文-哈哈哈哈", self.body())

    async def test_unregistered_current_raw_is_available_but_bounded(self):
        for i in range(35):
            self.row("未登记前文" + str(i), role="her", read=True)
        fresh = self.row("这次的新话")
        result = await self.ask()
        self.assertEqual(len(result["read"]), 31)
        self.assertIs(result["read"][-1], fresh)
        self.assertNotIn("未登记前文0\n", self.body())

    async def test_eight_messages_still_ask_instead_of_hard_exit(self):
        self.row("她刚参与的问题", role="her", read=True)
        for i in range(8):
            self.row("继续这个话题" + str(i), read=i < 7)
        await self.chat.window_turn()
        self.assertEqual(len(self.judge.calls), 1)
        self.assertTrue(self.chat.window_open(self.now))
        self.assertEqual(self.chat.store.extra["windowMisses"], 1)

    async def test_long_previous_raw_is_bounded_without_rewriting_history(self):
        old = self.row("旧原话" * 4000, role="her", read=True)
        self.record("当前还没成包", "queued", rows=[old])
        self.row("新的简短回答")
        await self.chat.window_turn()
        shown = self.judge.calls[-1][1][0]["content"]
        self.assertLess(len(shown), 6200)
        self.assertEqual(len(old["text"]), 12000)
        archived = next(m for m in self.chat.history_archive.messages if m["id"] == old["id"])
        self.assertEqual(archived["text"], old["text"])

    async def test_eight_messages_judge_can_continue_and_forward_focus(self):
        self.row("她刚参与的问题", role="her", read=True)
        rows = [self.row("继续回答这个问题" + str(i)) for i in range(9)]
        self.judge.reply = True
        turns = []
        async def turn(*args, **kwargs):
            turns.append((args, kwargs))
        self.chat._turn = turn
        await self.chat.window_turn()
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0][0][1], [rows[-1]["id"]])

    async def test_no_new_prompt_rule_for_short_answers(self):
        self.row("可以")
        await self.ask()
        self.assertNotIn("即使只有", self.body())
        self.assertNotIn("也不等于礼貌", self.body())

    async def test_caption_wait_keeps_attachment_and_new_message_order(self):
        entered, release = asyncio.Event(), asyncio.Event()
        image = self.row("看这张", attachment={"kind": "image", "file": "synthetic.png"})
        attachment = image["attachment"]
        async def caption(gid, msgs, save, deadline):
            self.assertIs(msgs[-1], image)
            self.assertIs(msgs[-1]["attachment"], attachment)
            self.assertEqual(deadline, self.now + G.WAIT_MS)
            entered.set()
            await release.wait()
            attachment["caption"] = "假的图片里有新叶"
            save()
        self.hub.captions = caption
        task = asyncio.create_task(self.chat.window_turn())
        await asyncio.wait_for(entered.wait(), 2)
        later = self.row("等待时才来的话")
        self.assertEqual(len(self.judge.calls), 0)
        release.set()
        await task
        self.assertIn("假的图片里有新叶", self.body())
        self.assertNotIn(later["text"], self.body())
        self.assertTrue(image["read"])
        self.assertFalse(later["read"])
        self.assertIs(self.chat.msgs[0]["attachment"], attachment)

    async def test_energy_is_rechecked_after_caption_wait(self):
        entered, release = asyncio.Event(), asyncio.Event()
        row = self.row("等图片", attachment={"kind": "image", "file": "synthetic.png"})
        async def caption(*args):
            entered.set()
            await release.wait()
        self.hub.captions = caption
        task = asyncio.create_task(self.chat.window_turn())
        await asyncio.wait_for(entered.wait(), 2)
        self.her.energy.update(value=10, updatedAt=self.now)
        release.set()
        await task
        self.assertFalse(self.judge.calls)
        self.assertFalse(row["read"])

    async def test_new_call_while_judge_waits_remains_for_passive_route(self):
        self.row("先进入判断的普通话")
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait():
            entered.set()
            await release.wait()
        self.judge.wait = wait
        scheduled = []
        self.chat.schedule = lambda passive: scheduled.append(passive)
        task = asyncio.create_task(self.chat.window_turn())
        await asyncio.wait_for(entered.wait(), 2)
        called = self.row("等待时叫她", called=True)
        self.chat.more_came = True
        release.set()
        await task
        self.assertFalse(called["read"])
        self.assertNotIn(called["text"], self.body())
        self.assertEqual(scheduled, [True])

    async def test_pack_consume_leaves_no_stale_window_note(self):
        record = self.record("已整理应退出的记事")
        self.chat.round_memory._consumed.update(record["ids"])
        self.chat.round_memory._records.clear()
        self.row("新话")
        await self.ask()
        self.assertNotIn("已整理应退出的记事", self.body())

    async def test_window_does_not_change_formal_reply_context_selector(self):
        old = self.record("旧失败片段", "queued")
        self.record("最新成包")
        fresh = self.row("这次来话")
        formal = self.chat._request_context(self.chat.msgs, {fresh["id"]})
        window = (await self.ask())["read"]
        self.assertTrue(set(old["ids"]).intersection(m["id"] for m in formal))
        self.assertFalse(set(old["ids"]).intersection(m["id"] for m in window))

    async def test_window_note_contains_dates_sources_and_not_style_samples(self):
        self.record("我们讨论过一件事")
        self.row("现在接着说")
        await self.ask()
        self.assertIn("【本群此前两轮的记事】", self.body())
        self.assertIn("2026-", self.body())
        self.assertIn("测试群one", self.body())
        self.assertIn("不是本轮新消息，也不是说话风格样例", self.body())


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(WindowTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    count = result.testsRun
    passed = count - len(result.failures) - len(result.errors)
    print(f"window235: {passed}/{count}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
