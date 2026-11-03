"""离线主联系人短应答：三版共用，假模型/时间/发送屏障，无真实宿主或密钥。"""
import asyncio
import copy
import importlib
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
PKG = "multichat" if (ROOT / "multichat").is_dir() else "firefly"
def load(name):
    return importlib.import_module(PKG + "." + name)
E, G, O, W, S = [load(x) for x in ("engine", "group", "offline", "winext", "schedule")]
Store, Her = load("store").Store, load("store").Her
Memory, Embedder = load("memory").Memory, load("embed").Embedder
Events, Intimacy = load("events").Events, load("intimacy").Intimacy
fresh_energy = load("energy").fresh_energy
MIN = 60_000
BASE = 1793678400000
PRIMARY = "900001"  # 人造测试账号，同时适用发布版数字账号校验。
LOG = logging.getLogger("offline236")


class Model:
    def __init__(self):
        self.calls = []
        self.reply = "我这会儿不方便，晚点认真回你。\n多的一句不发。"
        self.error = None
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.block = False
        self.cancelled = False

    def provider(self):
        return self

    def supports(self, feature):
        return False

    async def chat_with_retry(self, system, turns, **kwargs):
        self.calls.append((system, turns, kwargs))
        self.entered.set()
        try:
            if self.block:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error:
            raise self.error
        return SimpleNamespace(ok=True, text=self.reply, kind="", detail="")


class Sender:
    def __init__(self):
        self.sent, self.attempts = [], 0
        self.accept = True

    async def send(self, *args):
        self.attempts += 1
        if self.accept:
            self.sent.append(args[-1] if len(args) == 1 else args[1])
        return self.accept

    async def typing(self, *args):
        pass


class OfflineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="offline236-")
        self.now, self.mode = BASE, "sleep"
        self.all = []
        self.seq = 0
        self.patches = []
        for module in (E, G):
            self.patches += [patch.object(module, "state_at", self.state), patch.object(module, "now_ms", lambda: self.now),
                             patch.object(module, "online_since", lambda t: self.now if self.mode == "online" else None),
                             patch.object(module, "pace", lambda parts: [("message", p) for p in parts])]
        for p in self.patches:
            p.start()

    def state(self, t):
        online = self.mode == "online" or t >= BASE + 60 * MIN
        return S.HerState(online, "mission" if self.mode == "mission" else "base",
                          "night" if online else self.mode, 12, 0)

    def objects(self):
        self.seq += 1
        root = Path(self.tmp.name) / str(self.seq)
        her = Her(str(root))
        her.energy = fresh_energy(self.now)
        st = Store(str(root / "chat"), her)
        return root, her, st, Memory(st, Embedder(LOG), LOG), Model(), Sender(), Events(str(root), LOG)

    def private(self, peer=False, enabled=True, store=None):
        root, her, st, memory, model, sender, events = self.objects()
        if store is not None:
            st = Store(store.root, store.her)
            memory = Memory(st, Embedder(LOG), LOG)
        e = E.Engine(st, model, memory, None, None, None, sender, LOG, events=events,
                     options=lambda: {"offline_reply": enabled},
                     peer={"qq": "other", "name": "测试群友"} if peer else None)
        e.mem_busy = True
        self.all.append(e)
        return e, model, sender

    def group(self):
        root, her, st, memory, model, sender, events = self.objects()
        hub = G.GroupHub(her, model, model, None, None, None, None, Intimacy(str(root), PRIMARY), sender,
                         events, PRIMARY, lambda: {"offline_reply": True, "look_per_hour": 0},
                         lambda gid: "测试群", lambda mood: None, None)
        c = G.GroupChat(str(self.seq), hub, st, memory)
        hub.chats[c.gid] = c
        c.schedule_history = lambda: None
        c.schedule_memory = lambda: None
        self.all.append(c)
        return c, model, sender

    async def drain(self, e):
        for _ in range(15):
            await asyncio.sleep(0)
            tasks = [t for t in e._running if not t.done() and t is not asyncio.current_task()]
            if not tasks:
                return
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        self.fail("offline queue did not drain")

    async def receive(self, e, qid="u1"):
        await e.on_user_message("现在能回我吗？", [], qid)
        await self.drain(e)

    def pending(self, e):
        self.assertTrue(all(m.get("undelivered") and not m.get("seen") and not m.get("answered")
                            for m in e.history if m["role"] == "user"))

    async def asyncTearDown(self):
        for obj in self.all:
            obj.shutdown()
        tasks = [t for obj in self.all for t in obj._running if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def test_primary_private_rest_and_mission_one_message(self):
        for mode in ("sleep", "mission"):
            self.mode = mode
            e, model, sender = self.private()
            energy, mood = copy.deepcopy(e.store.energy), copy.deepcopy(e.store.mood)
            await self.receive(e)
            await self.receive(e, "u2")
            self.assertEqual(len(model.calls), 1)
            self.assertEqual(sender.sent, [model.reply.splitlines()[0]])
            self.assertEqual(len(e.history), 2)
            self.assertEqual((e.store.energy, e.store.mood), (energy, mood))
            self.assertIn(sender.sent[0], O.notice_context(e.store, self.now))
            self.pending(e)

    async def test_plain_contacts_and_disabled_never_call_model(self):
        for peer, enabled in ((True, True), (False, False)):
            e, model, sender = self.private(peer=peer, enabled=enabled)
            await self.receive(e)
            self.assertEqual(model.calls, [])
            self.assertEqual(len(sender.sent), int(enabled))
            self.assertNotIn("offlineNoticeText", e.store.extra)
            self.pending(e)

    async def test_short_prompt_current_only_no_tools(self):
        e, model, sender = self.private()
        e.store.messages.append(e.store.new_msg("user", "老聊天原话不该带", ts=self.now - 3 * MIN))
        await self.receive(e)
        system, turns, kwargs = model.calls[0]
        self.assertIn("【你这边的安排】", system)
        self.assertIn("只简短回一句", system)
        self.assertNotIn("老聊天原话不该带", repr((system, turns)))
        self.assertNotIn("tools", kwargs)
        self.assertEqual(len(turns), 1)

    async def test_failure_fallback_and_no_credential_in_logs(self):
        e, model, sender = self.private()
        model.error = RuntimeError("Authorization: Bearer SYNTHETIC_ONLY_ABC123\napi_key='FAKE_TEST_SECRET'")
        await self.receive(e)
        self.assertEqual(sender.sent, [O.REST_TEXT])
        self.assertEqual(e.events.today().get("模型失败"), 1)
        self.assertEqual(e.store.extra["offlineNoticeText"]["text"], O.REST_TEXT)
        self.assertNotIn("SYNTHETIC_ONLY_ABC123", repr(e.events.items))
        self.assertNotIn("FAKE_TEST_SECRET", repr(e.events.items))
        self.pending(e)

    async def test_timeout_cancels_request_then_fallback(self):
        e, model, sender = self.private()
        model.block = True
        with patch.object(O, "REQUEST_TIMEOUT_S", 0.01):
            await self.receive(e)
        self.assertTrue(model.cancelled)
        self.assertEqual(sender.sent, [O.REST_TEXT])
        self.pending(e)

    async def test_recall_during_request_suppresses_send(self):
        e, model, sender = self.private()
        model.block = True
        await e.on_user_message("找你", [], "gone")
        await asyncio.wait_for(model.entered.wait(), 1)
        await e.on_recall("gone")
        await self.drain(e)
        self.assertTrue(model.cancelled)
        self.assertFalse(model.release.is_set())
        self.assertEqual(sender.sent, [])
        self.assertNotIn("offlineNotice", e.store.extra)
        self.assertIsNone(e._offline_notice_request)
        self.assertEqual(e._offline_notice_ids, set())

    async def test_recalled_notification_releases_lock_without_model_release(self):
        e, model, sender = self.private()
        model.block = True
        await e.on_user_message("找你", [], "gone")
        await asyncio.wait_for(model.entered.wait(), 1)
        await e.on_recall("gone")
        await self.drain(e)
        self.assertTrue(model.cancelled)
        self.assertFalse(model.release.is_set())
        self.assertFalse(e.in_flight)
        self.assertFalse(sender.sent)
        model.block = False
        await self.receive(e, "later")
        self.assertEqual((len(model.calls), len(sender.sent)), (2, 1))
        self.assertEqual({m.get("qid") for m in e.history}, {"later"})
        self.pending(e)

    async def test_partial_batch_recall_cuts_old_notice_but_later_message_runs(self):
        e, model, sender = self.private()
        model.block = True
        async with e._speaking():
            await e.on_user_message("这条保留", [], "keep")
            await e.on_user_message("这条撤回", [], "gone")
        await asyncio.wait_for(model.entered.wait(), 1)
        self.assertEqual(len(model.calls[0][1]), 1)
        self.assertIn("这条撤回", model.calls[0][1][0]["content"])
        await e.on_recall("gone")
        await e.on_user_message("之后来的", [], "later")
        model.block = False  # 已在等的旧请求仍不会被放行；新请求可以立即完成。
        await self.drain(e)
        self.assertTrue(model.cancelled)
        self.assertFalse(model.release.is_set())
        self.assertEqual((len(model.calls), len(sender.sent)), (2, 1))
        self.assertIn("之后来的", model.calls[1][1][0]["content"])
        self.assertEqual({m.get("qid") for m in e.history}, {"keep", "later"})
        self.pending(e)

    async def test_empty_and_image_only_notice_have_nonempty_trigger(self):
        for attachments in ([], [{"path": "fake-image-only-not-read"}]):
            e, model, sender = self.private()
            await e.on_user_message("", attachments, "only")
            await self.drain(e)
            self.assertTrue(model.calls[0][1][0]["content"])
            self.assertNotIn("fake-image-only-not-read", repr(model.calls))
            self.pending(e)

    async def test_online_during_request_suppresses_stale_notice(self):
        e, model, sender = self.private()
        model.block = True
        await e.on_user_message("找你", [], "u1")
        await asyncio.wait_for(model.entered.wait(), 1)
        self.mode = "online"
        model.release.set()
        await self.drain(e)
        self.assertFalse(sender.sent)
        self.pending(e)

    async def test_state_changes_while_request_only_new_segment_replies(self):
        e, model, sender = self.private()
        model.block = True
        await e.on_user_message("找你", [], "u1")
        await asyncio.wait_for(model.entered.wait(), 1)
        self.mode = "mission"
        await e.on_user_message("还有一句", [], "u2")
        model.release.set()
        await self.drain(e)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(sender.sent), 1)
        self.assertTrue(e.store.extra["offlineNotice"].startswith("mission:"))
        self.pending(e)

    async def test_more_messages_do_not_restart_or_duplicate(self):
        e, model, sender = self.private()
        model.block = True
        await e.on_user_message("找你", [], "u1")
        await asyncio.wait_for(model.entered.wait(), 1)
        await e.on_user_message("补充", [], "u2")
        self.assertFalse(model.cancelled)
        model.release.set()
        await self.drain(e)
        self.assertEqual((len(model.calls), len(sender.sent)), (1, 1))
        self.pending(e)

    async def test_parent_cancel_never_falls_back(self):
        e, model, sender = self.private()
        model.block = True
        await e.on_user_message("找你", [], "u1")
        await asyncio.wait_for(model.entered.wait(), 1)
        e.shutdown()
        await self.drain(e)
        self.assertTrue(model.cancelled)
        self.assertFalse(sender.sent)
        self.assertNotIn("offlineNotice", e.store.extra)

    async def test_send_failure_does_not_use_allowance(self):
        e, model, sender = self.private()
        sender.accept = False
        await self.receive(e)
        self.assertNotIn("offlineNotice", e.store.extra)
        sender.accept = True
        await self.receive(e, "u2")
        self.assertEqual((len(model.calls), len(sender.sent)), (2, 1))
        self.pending(e)

    async def test_restart_preserves_allowance_and_notice(self):
        e, model, sender = self.private()
        await self.receive(e)
        e.shutdown()
        e2, model2, sender2 = self.private(store=e.store)
        await self.receive(e2, "u2")
        self.assertEqual((model2.calls, sender2.sent), ([], []))
        self.assertIn(sender.sent[0], O.notice_context(e2.store, self.now))
        self.pending(e2)

    async def test_group_primary_rest_and_mission_only_once(self):
        for mode in ("sleep", "mission"):
            self.mode = mode
            c, model, sender = self.group()
            before = copy.deepcopy(c.hub.her.energy)
            await c.on_message(PRIMARY, "测试联系人", "叫你", [], "u1", True)
            await c.on_message(PRIMARY, "测试联系人", "又叫你", [], "u2", True)
            self.assertEqual((len(model.calls), len(sender.sent)), (1, 1))
            self.assertEqual(c.hub.her.energy, before)
            self.assertEqual(c.store.extra["windowUntil"], 0)
            self.assertTrue(all(m.get("left") and not m.get("answered") for m in c.msgs if m["role"] == "user"))
            self.assertEqual([m["text"] for m in c.msgs if m["role"] == "her"], sender.sent)
            target = next(m for m in c.msgs if m.get("qid") == "u1")
            archive = {m["id"]: m for m in c.history_archive.messages}
            material = {m["id"]: m for m in c.memory_session()}
            self.assertTrue(target.get("read"))
            self.assertEqual(len(archive), 2)
            self.assertEqual(set(archive), set(material))
            self.assertEqual(material[target["id"]]["text"], target["text"])
            self.assertEqual(c.round_memory.stats()["pending"], 0)

    async def test_contact_impression_is_shared_without_score_or_extra_calls(self):
        e, model, sender = self.private()
        c, group_model, group_sender = self.group()
        intimacy = c.hub.intimacy
        intimacy.apply(PRIMARY, 0, "", "测试联系人", "测试共享印象：相处坦诚，喜欢看海。", observed_at=self.now)
        e.intimacy = intimacy
        await self.receive(e)
        await c.on_message(PRIMARY, "测试联系人", "叫你", [], "g1", True)
        for calls in (model.calls, group_model.calls):
            self.assertEqual(len(calls), 1)
            self.assertIn("测试共享印象", calls[0][0])
            self.assertNotIn("90", calls[0][0])

    async def test_group_send_failure_does_not_archive_target_or_answer(self):
        c, model, sender = self.group()
        sender.accept = False
        await c.on_message(PRIMARY, "测试联系人", "叫你", [], "u1", True)
        target = c.msgs[0]
        self.assertFalse(target.get("read"))
        self.assertTrue(target.get("left"))
        self.assertFalse(target.get("answered"))
        self.assertEqual(c.history_archive.messages, [])
        self.assertEqual(c.memory_session(), [])
        self.assertEqual(c.round_memory.stats()["pending"], 0)

    async def test_group_target_is_protected_while_new_messages_trim_buffer(self):
        c, model, sender = self.group()
        model.block = True
        with patch.object(G, "BUFFER_MAX", 3):
            task = asyncio.create_task(c.on_message(PRIMARY, "测试联系人", "要回复的原话", [], "u1", True))
            await asyncio.wait_for(model.entered.wait(), 1)
            for i in range(8):
                await c.on_message("other", "其他人", "普通聊天" + str(i), [], "p" + str(i), False)
            self.assertTrue(any(m.get("qid") == "u1" for m in c.msgs))
            model.release.set()
            await task
        self.assertEqual(len(sender.sent), 1)
        self.assertTrue(any(m.get("qid") == "u1" for m in c.history_archive.messages))

    async def test_group_recall_during_send_does_not_revive_target(self):
        c, model, sender = self.group()
        entered, release = asyncio.Event(), asyncio.Event()
        real_send = sender.send
        async def paused_send(*args):
            entered.set()
            await release.wait()
            return await real_send(*args)
        sender.send = paused_send
        task = asyncio.create_task(c.on_message(PRIMARY, "测试联系人", "要撤回", [], "gone", True))
        await asyncio.wait_for(entered.wait(), 1)
        target = c.msgs[0]
        await c.on_recall("gone")
        release.set()
        await task
        self.assertEqual(len(sender.sent), 1)  # 发送已交给传输层，实际送出的通知仍需保留。
        self.assertFalse(target.get("read"))
        self.assertFalse(any(m.get("qid") == "gone" for m in c.msgs))
        self.assertEqual([m["role"] for m in c.history_archive.messages], ["her"])
        self.assertEqual([m["role"] for m in c.memory_session()], ["her"])
        self.assertFalse(c.store.extra.get("promised"))

    async def test_group_peers_fixed_ordinary_silent(self):
        c, model, sender = self.group()
        await c.on_message(PRIMARY, "测试联系人", "闲聊", [], "u1", False)
        await c.on_message("other", "别人", "叫你", [], "u2", True)
        self.assertEqual(model.calls, [])
        self.assertEqual(len(sender.sent), 1)

    async def test_group_recall_during_model(self):
        c, model, sender = self.group()
        model.block = True
        task = asyncio.create_task(c.on_message(PRIMARY, "测试联系人", "叫你", [], "gone", True))
        await asyncio.wait_for(model.entered.wait(), 1)
        await c.on_recall("gone")
        self.assertFalse(model.cancelled)  # 群撤回不掐请求，只在完成后阻止发送。
        model.release.set()
        await task
        self.assertFalse(model.cancelled)
        self.assertFalse(sender.sent)
        self.assertFalse(c.store.extra.get("missionAck"))
        self.assertEqual(c.history_archive.messages, [])
        self.assertEqual(c.memory_session(), [])

    async def test_group_online_during_model(self):
        c, model, sender = self.group()
        c.catchup = AsyncMock(return_value=False)  # 本例只测过时通知；正式补看由旧流程回归覆盖。
        model.block = True
        task = asyncio.create_task(c.on_message(PRIMARY, "测试联系人", "叫你", [], "u1", True))
        await asyncio.wait_for(model.entered.wait(), 1)
        self.mode = "online"
        model.release.set()
        await task
        self.assertFalse(sender.sent)
        self.assertTrue(c.msgs[0].get("left"))

    async def test_group_rest_to_mission_independent_allowance(self):
        c, model, sender = self.group()
        await c.on_message(PRIMARY, "测试联系人", "叫你", [], "u1", True)
        self.mode = "mission"
        await c.on_message(PRIMARY, "测试联系人", "再叫你", [], "u2", True)
        self.assertEqual((len(model.calls), len(sender.sent)), (2, 2))

    async def test_group_pending_recalled_first_keeps_second(self):
        c, model, sender = self.group()
        model.block = True
        task = asyncio.create_task(c.on_message(PRIMARY, "测试联系人", "叫你", [], "gone", True))
        await asyncio.wait_for(model.entered.wait(), 1)
        await c.on_message(PRIMARY, "测试联系人", "补一句", [], "keep", True)
        await c.on_recall("gone")
        model.release.set()
        await task
        await self.drain(c)
        self.assertEqual((len(model.calls), len(sender.sent)), (2, 1))
        self.assertTrue(next(m for m in c.msgs if m.get("qid") == "keep").get("left"))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
