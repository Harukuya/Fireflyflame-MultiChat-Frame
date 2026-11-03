"""End-to-end core checks: fake model and transport, real stores and state machines."""
import asyncio
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
from multichat import engine as engine_module, group as group_module, profile, persona
from multichat.runtime import Runtime
from multichat.ports import Conversation, IncomingMessage
from multichat.model_port import ScriptedModelProvider, ModelResponse
from multichat.schedule import state_at


class Transport:
    def __init__(self):
        self.sent = []
        self.accept = True

    async def send_text(self, conversation, text, reply_to=None):
        self.sent.append((conversation.scope, text, reply_to))
        return self.accept

    async def send_image(self, conversation, image_path):
        self.sent.append((conversation.scope, image_path, None))
        return self.accept

    async def typing(self, conversation):
        pass


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multichat-runtime-test-")
        self.path = Path(self.temp.name)
        self.transport = Transport()
        self.model = ScriptedModelProvider([ModelResponse(text="我看到了，稍后一起看。") for _ in range(20)])
        self.instances = []
        self.patches = [patch.object(engine_module, "pace", lambda parts: [("message", x) for x in parts]),
                        patch.object(group_module, "pace", lambda parts: [("message", x) for x in parts])]
        for p in self.patches:
            p.start()
        self.runtime = self.make("one")

    def make(self, folder, **config):
        instance = Runtime(self.path / folder, self.transport, lambda conversation: self.model,
                           {"schedule_mode": "always", "primary_user_id": "net:alice", **config},
                           assets_dir=ROOT, logger=logging.getLogger("test.multichat"))
        self.instances.append(instance)
        return instance

    async def asyncTearDown(self):
        for instance in self.instances:
            await instance.close()
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    async def private_turn(self, runtime=None, sid="net:alice", text="周末一起读这本书吗？"):
        r = runtime or self.runtime
        r.register("private", sid, name="读者")
        await r.receive(IncomingMessage(Conversation("private", sid), sid, "读者", text, "m1"))
        e = r.engine(sid)
        if e.batch_task:
            e.batch_task.cancel()
            await asyncio.gather(e.batch_task, return_exceptions=True)
            e.batch_task = None
        with r.profile_scope():
            await e.run_turn()
        return e

    async def test_private_delivery_and_model_prompt(self):
        e = await self.private_turn()
        self.assertEqual(len(self.transport.sent), 1)
        self.assertTrue(e.history[0]["answered"])
        self.assertEqual(e.history[-1]["role"], "her")
        system = self.model.requests[0].system
        self.assertIn("小伴", system)
        self.assertNotIn("流萤", system)
        self.assertFalse(any(s in system for s in ("亲密度90", "亲密度 90", "星核猎手")))

    async def test_called_group_runs_actual_turn(self):
        r = self.runtime
        chat = r.register("group", "room:called", name="小组")
        await r.receive(IncomingMessage(Conversation("group", chat.gid), "net:bob", "B", "来聊聊阅读计划", "call1", True))
        if chat.batch_task:
            chat.batch_task.cancel()
            await asyncio.gather(chat.batch_task, return_exceptions=True)
            chat.batch_task = None
        with r.profile_scope():
            await chat.passive_turn()
        self.assertEqual(self.transport.sent[0][0], "g:room:called")
        self.assertTrue(chat.msgs[0]["answered"])
        self.assertTrue(chat.msgs[0]["read"])
        self.assertNotIn("开拓者", self.model.requests[0].system)

    async def test_caption_updates_same_record_and_persists(self):
        r = self.runtime
        caption = ScriptedModelProvider([ModelResponse(text="一张阅读角的照片。")])
        r._caption_provider_for = lambda conversation: caption
        self.model.provider_config["modalities"] = ["text"]
        r.register("private", "net:bob")
        image = self.path / "caption.png"
        image.write_bytes(b"synthetic model fixture")
        attachment = r.add_image(image)
        await r.receive(IncomingMessage(Conversation("private", "net:bob"), "net:bob", "B", "照片", "image1",
                                       attachments=[attachment]))
        e = r.engine("net:bob")
        e.batch_task.cancel()
        await asyncio.gather(e.batch_task, return_exceptions=True)
        e.batch_task = None
        await asyncio.gather(*list(r.captioner._tasks.values()))
        self.assertEqual(e.history[0]["attachment"]["caption"], "一张阅读角的照片。")
        saved = json.loads(Path(e.store.root, "messages.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["attachment"]["caption"], e.history[0]["attachment"]["caption"])
        self.assertEqual(len(caption.requests), 1)

    async def test_failed_data_reload_keeps_live_resources(self):
        r = self.runtime
        assets = self.path / "alternate-assets"
        shutil.copytree(ROOT / "prompts", assets / "prompts")
        shutil.copytree(ROOT / "data", assets / "data")
        old_knowledge, old_profile = r.knowledge, r.profile_state
        r.assets_dir = assets
        (assets / "data" / "knowledge" / "kb.json").write_text("{invalid", encoding="utf-8")
        with self.assertRaises(Exception):
            await r.reload_data()
        self.assertIs(r.knowledge, old_knowledge)
        self.assertIs(r.profile_state, old_profile)

    async def test_failed_delivery_does_not_answer(self):
        self.transport.accept = False
        e = await self.private_turn()
        self.assertFalse(e.history[0].get("answered", False))
        self.assertFalse(any(m["role"] == "her" for m in e.history))

    async def test_unknown_and_mismatched_private_rejected(self):
        c = Conversation("private", "net:bob")
        self.assertFalse(await self.runtime.receive(IncomingMessage(c, c.id, "B", "hello")))
        self.runtime.register("private", c.id)
        with self.assertRaises(ValueError):
            await self.runtime.receive(IncomingMessage(c, "other", "B", "hello"))

    async def test_empty_and_pre_recalled_messages_dropped(self):
        r = self.runtime
        r.register("private", "net:alice")
        c = Conversation("private", "net:alice")
        self.assertFalse(await r.receive(IncomingMessage(c, c.id, "A", "")))
        await r.recall(c, "gone")
        self.assertFalse(await r.receive(IncomingMessage(c, c.id, "A", "hello", "gone")))
        self.assertEqual(r.engine(c.id).history, [])

    async def test_group_multi_image_recall(self):
        r = self.runtime
        chat = r.register("group", "matrix:room:/abc", proactive=False)
        source = self.path / "fake.png"
        source.write_bytes(b"fake local fixture; no vision request")
        images = [r.add_image(source), r.add_image(source)]
        c = Conversation("group", chat.gid)
        await r.receive(IncomingMessage(c, "other:user", "B", "两张图", "photos", False, images))
        self.assertEqual(len(chat.msgs), 2)
        self.assertIsNot(chat.msgs[0]["attachment"], chat.msgs[1]["attachment"])
        await r.recall(c, "photos")
        self.assertEqual(chat.msgs, [])
        self.assertEqual(len(chat._recalled_ids), 2)

    async def test_id_path_is_hashed(self):
        c = self.runtime.register("group", "../../outside:room", name="不可作为路径")
        path = Path(c.store.root).resolve()
        self.assertTrue(path.is_relative_to(Path(self.runtime.data_root)))
        self.assertEqual(len(path.name), 64)

    async def test_primary_can_be_disabled_and_reenabled(self):
        r = self.runtime
        previous = r.register("private", "net:alice")
        await r.update_session("private", "net:alice", "enabled", False)
        self.assertTrue(previous.closed)
        self.assertNotIn("net:alice", r.engines)
        self.assertFalse(await r._send(Conversation("private", "net:alice"), text="no"))
        await r.update_session("private", "net:alice", "enabled", True)
        self.assertFalse(r.engine("net:alice").closed)

    async def test_restart_restores_unseen_and_shared_her(self):
        r = self.runtime
        r.register("private", "net:alice")
        r.register("group", "room:one")
        await r.receive(IncomingMessage(Conversation("private", "net:alice"), "net:alice", "A", "待回复", "pending"))
        await r.close()
        restored = self.make("one")
        self.assertIn("room:one", restored.hub.chats)
        self.assertEqual(restored.engine("net:alice")._pending()[0]["text"], "待回复")
        self.assertIs(restored.engine("net:alice").store.her, restored.chat("room:one").store.her)
        self.assertIsNot(restored.engine("net:alice").store, restored.chat("room:one").store)

    async def test_sharing_switches(self):
        e = await self.private_turn()
        r = self.runtime
        r.register("group", "room:one")
        self.assertTrue(r._others_for("g:room:one"))
        with r.profile_scope():
            self.assertIn("私聊", r._recent_context("g:room:one", "读书"))
        r.config["share_memory"] = r.config["share_recent"] = False
        self.assertEqual(r._others_for("g:room:one"), [])
        self.assertEqual(r._recent_context("g:room:one", "读书"), "")

    async def test_two_runtime_identity_contexts(self):
        second = self.make("two", character_name="另一个角色")
        with self.runtime.profile_scope():
            self.assertEqual(profile.bot_name(), "小伴")
            self.assertIn("你是小伴", persona.build_system_prompt())
        with second.profile_scope():
            self.assertEqual(profile.bot_name(), "另一个角色")
        self.assertIsNot(self.runtime.her, second.her)

    async def test_sharing_off_keeps_own_group_notes(self):
        r = self.runtime
        own = r.register("group", "room:one")
        other = r.register("group", "room:two")
        own.recent_snapshot = lambda: {"rows": [], "packs": [{"id": "own", "summary": "本群约好读书", "start": 1000, "end": 2000}]}
        other.recent_snapshot = lambda: {"rows": [], "packs": [{"id": "other", "summary": "另一个群的约定", "start": 1000, "end": 2000}]}
        r.config["share_recent"] = False
        with r.profile_scope():
            result = r._recent_context("g:room:one", "约定")
        self.assertIn("本群约好读书", result)
        self.assertNotIn("另一个群的约定", result)

    async def test_attachment_rejects_traversal(self):
        r = self.runtime
        r.register("private", "net:alice")
        with self.assertRaises(ValueError):
            await r.receive(IncomingMessage(Conversation("private", "net:alice"), "net:alice", "A", "",
                                           attachments=[{"kind": "image", "file": "../outside.png"}]))

    async def test_close_cancels_tasks_and_rejects_work(self):
        r = self.runtime
        r.register("private", "net:alice")
        await r.receive(IncomingMessage(Conversation("private", "net:alice"), "net:alice", "A", "pending"))
        task = r.engine("net:alice").batch_task
        await r.close()
        self.assertTrue(task.done())
        self.assertFalse(await r.receive(IncomingMessage(Conversation("private", "net:alice"), "net:alice", "A", "new")))
        with self.assertRaises(RuntimeError):
            r.register("group", "new")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"runtime {result.testsRun - len(result.failures) - len(result.errors)}/{result.testsRun}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
