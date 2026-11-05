"""Quoted-message port checks with real Runtime, fake models and temporary data."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
from multichat import engine as engine_module, group as group_module
from multichat.model_port import ModelResponse, ScriptedModelProvider
from multichat.ports import Conversation, IncomingMessage
from multichat.runtime import Runtime


class Transport:
    def __init__(self):
        self.sent = []

    async def send_text(self, conversation, text, reply_to=None):
        self.sent.append((conversation.scope, text, reply_to))
        return True

    async def send_image(self, conversation, image_path):
        self.sent.append((conversation.scope, image_path, None))
        return True

    async def typing(self, conversation):
        pass


class QuotePortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multichat-quote-port239-")
        self.path = Path(self.temp.name)
        self.model = ScriptedModelProvider([ModelResponse(text="好的，就按刚才那个安排来。") for _ in range(10)])
        self.transport = Transport()
        self.runtime = Runtime(self.path / "runtime", self.transport, lambda conversation: self.model,
                               {"schedule_mode": "always", "primary_user_id": "net:alice"},
                               assets_dir=ROOT, logger=logging.getLogger("test.quote-port239"))
        self.patches = [patch.object(engine_module, "pace", lambda parts: [("message", text) for text in parts]),
                        patch.object(group_module, "pace", lambda parts: [("message", text) for text in parts])]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    @staticmethod
    async def stop_batch(target):
        task = target.batch_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            target.batch_task = None

    def private(self, sid="net:alice"):
        self.runtime.register("private", sid, name="来访者")
        return Conversation("private", sid), self.runtime.engine(sid)

    def group(self, sid="room:one"):
        chat = self.runtime.register("group", sid, name="讨论室", proactive=False)
        return Conversation("group", sid), chat

    @staticmethod
    def quote(role="her", **values):
        return {"qid": "old-message", "role": role, "uid": "net:character" if role == "her" else "net:other",
                "name": "小伴" if role == "her" else "其他人", "text": "周末一起阅读计划标记", "ts": 1793851200000,
                **values}

    async def test_positional_message_api_is_backward_compatible(self):
        attachments = []
        msg = IncomingMessage(Conversation("group", "room:one"), "net:alice", "A", "hello", "m", True, attachments)
        self.assertIs(msg.attachments, attachments)
        self.assertIsNone(msg.quote)

    async def test_private_quote_snapshot_is_separate_and_saved(self):
        conv, engine = self.private()
        quote = self.quote()
        self.assertTrue(await self.runtime.receive(IncomingMessage(conv, conv.id, "来访者", "就这样吧", "new", quote=quote)))
        await self.stop_batch(engine)
        quote["text"] = "host mutated object"
        self.assertEqual(len(engine.history), 1)
        row = engine.history[0]
        self.assertEqual(row["text"], "就这样吧")
        self.assertEqual(row["quote"]["text"], "周末一起阅读计划标记")
        self.assertEqual(row["qid"], "new")
        saved = json.loads(Path(engine.store.root, "messages.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["quote"], row["quote"])

    async def test_quote_only_private_reaches_model(self):
        conv, engine = self.private()
        self.assertTrue(await self.runtime.receive(IncomingMessage(conv, conv.id, "来访者", "", "new", quote=self.quote())))
        await self.stop_batch(engine)
        with self.runtime.profile_scope():
            await engine.run_turn()
        self.assertTrue(self.model.requests)
        messages = json.dumps(self.model.requests[0].messages, ensure_ascii=False)
        self.assertIn("周末一起阅读计划标记", messages)
        self.assertIn("引用", messages)
        self.assertEqual(len([m for m in engine.history if m["role"] == "user"]), 1)
        self.assertTrue(engine.history[0].get("answered"))

    async def test_quote_other_person_does_not_call_character(self):
        conv, chat = self.group()
        await self.runtime.receive(IncomingMessage(conv, "net:alice", "当前说话人", "同意", "new", quote=self.quote("user")))
        self.assertFalse(chat.msgs[0]["called"])
        self.assertEqual(chat.msgs[0]["uid"], "net:alice")
        self.assertEqual(chat.msgs[0]["quote"]["uid"], "net:other")
        self.assertIsNone(chat.batch_task)
        self.assertEqual(self.model.requests, [])
        with self.runtime.profile_scope():
            rendered = json.dumps(chat._turns(chat.msgs), ensure_ascii=False)
        self.assertIn("周末一起阅读计划标记", rendered)
        self.assertIn("其他人", rendered)

    async def test_quote_character_automatically_calls_and_reaches_model(self):
        conv, chat = self.group()
        await self.runtime.receive(IncomingMessage(conv, "net:alice", "来访者", "同意", "new", False, quote=self.quote()))
        self.assertTrue(chat.msgs[0]["called"])
        self.assertIsNotNone(chat.batch_task)
        await self.stop_batch(chat)
        with self.runtime.profile_scope():
            await chat.passive_turn()
        self.assertTrue(self.model.requests)
        self.assertIn("周末一起阅读计划标记", json.dumps(self.model.requests[0].messages, ensure_ascii=False))
        self.assertTrue(chat.msgs[0].get("answered"))

    async def test_quote_only_group_unknown_author_is_not_called(self):
        conv, chat = self.group()
        self.assertTrue(await self.runtime.receive(IncomingMessage(conv, "net:alice", "来访者", "", "new",
                                                                    quote={"qid": "unknown", "missing": True})))
        self.assertFalse(chat.msgs[0]["called"])
        self.assertTrue(chat.msgs[0]["quote"]["missing"])
        self.assertIsNone(chat.batch_task)

    async def test_explicit_call_is_preserved_when_quote_is_other_person(self):
        conv, chat = self.group()
        await self.runtime.receive(IncomingMessage(conv, "net:alice", "来访者", "你怎么看", "new", True,
                                                  quote=self.quote("user")))
        self.assertTrue(chat.msgs[0]["called"])
        await self.stop_batch(chat)

    async def test_group_multi_image_quote_only_on_first_split(self):
        conv, chat = self.group()
        image = self.path / "fake.png"
        image.write_bytes(b"synthetic image fixture")
        attachments = [self.runtime.add_image(image), self.runtime.add_image(image)]
        await self.runtime.receive(IncomingMessage(conv, "net:alice", "来访者", "两张图", "photos", False, attachments,
                                                  self.quote("user")))
        self.assertEqual(len(chat.msgs), 2)
        self.assertEqual([m["qid"] for m in chat.msgs], ["photos", "photos"])
        self.assertEqual(sum("quote" in m for m in chat.msgs), 1)
        self.assertIn("quote", chat.msgs[0])

    async def test_private_multi_image_quote_only_on_first_split(self):
        conv, engine = self.private()
        image = self.path / "fake.png"
        image.write_bytes(b"synthetic image fixture")
        attachments = [self.runtime.add_image(image), self.runtime.add_image(image)]
        await self.runtime.receive(IncomingMessage(conv, conv.id, "来访者", "两张图", "photos", attachments=attachments,
                                                  quote=self.quote()))
        await self.stop_batch(engine)
        self.assertEqual(len(engine.history), 2)
        self.assertEqual(sum("quote" in m for m in engine.history), 1)
        self.assertIn("quote", engine.history[0])

    async def test_recalled_new_message_is_dropped_with_quote(self):
        conv, engine = self.private()
        await self.runtime.recall(conv, "already-recalled")
        self.assertFalse(await self.runtime.receive(IncomingMessage(conv, conv.id, "来访者", "同意", "already-recalled",
                                                                   quote=self.quote())))
        self.assertEqual(engine.history, [])
        self.assertEqual(self.model.requests, [])

    async def test_recalled_old_id_does_not_discard_new_quote_snapshot(self):
        conv, chat = self.group()
        await self.runtime.recall(conv, "old-message")
        self.assertTrue(await self.runtime.receive(IncomingMessage(conv, "net:alice", "来访者", "这句", "new",
                                                                  quote=self.quote("user"))))
        self.assertEqual(len(chat.msgs), 1)
        self.assertEqual(chat.msgs[0]["qid"], "new")
        self.assertEqual(chat.msgs[0]["quote"]["text"], "周末一起阅读计划标记")

    async def test_missing_quote_lookup_stays_in_current_conversation(self):
        conv_a, chat_a = self.group("room:a")
        conv_b, chat_b = self.group("room:b")
        await self.runtime.receive(IncomingMessage(conv_a, "net:other", "原说话人", "只属于A的旧话标记", "old-message"))
        await self.runtime.receive(IncomingMessage(conv_b, "net:alice", "来访者", "同意", "new",
                                                  quote={"qid": "old-message", "missing": True}))
        self.assertTrue(chat_b.msgs[0]["quote"]["missing"])
        with self.runtime.profile_scope():
            rendered = json.dumps(chat_b._turns(chat_b.msgs), ensure_ascii=False)
        self.assertNotIn("只属于A的旧话标记", rendered)
        self.assertEqual(len(chat_a.msgs), 1)
        self.assertFalse(chat_a.msgs[0]["read"])

    async def test_same_conversation_missing_quote_resolves_without_reading_target(self):
        conv, chat = self.group()
        await self.runtime.receive(IncomingMessage(conv, "net:other", "原说话人", "本群旧话标记", "old-message"))
        old = chat.msgs[0]
        await self.runtime.receive(IncomingMessage(conv, "net:alice", "来访者", "同意", "new",
                                                  quote={"qid": "old-message", "missing": True}))
        self.assertEqual(chat.msgs[1]["quote"]["text"], "本群旧话标记")
        self.assertFalse(chat.msgs[1]["quote"].get("missing", False))
        self.assertFalse(old["read"])
        self.assertFalse(old.get("answered", False))
        self.assertEqual(len(chat.msgs), 2)

    async def test_malformed_quote_does_not_create_empty_messages(self):
        conv, engine = self.private()
        for malformed in ("a string", [], False, {}):
            self.assertFalse(await self.runtime.receive(IncomingMessage(conv, conv.id, "来访者", quote=malformed)))
        self.assertEqual(engine.history, [])

    async def test_unknown_metadata_and_nested_quote_are_not_retained(self):
        conv, engine = self.private()
        quote = self.quote(text="长" * 2200, file="C:/synthetic-secret", url="https://example.invalid/synthetic",
                           quote={"text": "nested message"}, attachment={"file": "synthetic-secret"})
        await self.runtime.receive(IncomingMessage(conv, conv.id, "来访者", "当前正文", "new", quote=quote))
        await self.stop_batch(engine)
        saved = engine.history[0]["quote"]
        self.assertLessEqual(len(saved["text"]), 2000)
        self.assertTrue(saved.get("truncated"))
        self.assertTrue({"file", "url", "quote", "attachment"}.isdisjoint(saved))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
