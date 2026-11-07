"""Real Runtime and dashboard API; only fake providers, transport and temp data."""
import asyncio
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
from multichat.dashboard_api import DashboardService
from multichat import engine as engine_module, group as group_module
from multichat.model_port import ModelResponse, ScriptedModelProvider, ToolCall
from multichat.model_routes import ROUTES, model_scope
from multichat.ports import Conversation, IncomingMessage
from multichat.runtime import Runtime


class Transport:
    async def send_text(self, conversation, text, reply_to=None):
        return True

    async def send_image(self, conversation, image_path):
        return True

    async def typing(self, conversation):
        pass


def model(name, modalities=("text", "image", "tool_use")):
    p = ScriptedModelProvider([ModelResponse(text="收到") for _ in range(20)], provider_id=name)
    p.provider_config["modalities"] = list(modalities)
    return p


class Directory:
    def __init__(self):
        self.models = {"chosen-a": model("chosen-a"), "chosen-b": model("chosen-b"),
                       "text-only": model("text-only", ("text",)), "unknown": model("unknown", ())}
        self.disabled = set()
        self.calls = []
        self.extra = []
        self.failure = False

    def enabled_chat_models(self, conversation):
        if self.failure:
            raise RuntimeError("synthetic-private-detail")
        return [{"id": key, "label": "模型 " + key, "model": "fake",
                 "modalities": p.provider_config["modalities"],
                 "capabilitiesKnown": key != "unknown"}
                for key, p in self.models.items() if key not in self.disabled] + self.extra

    def provider_by_id(self, ident, conversation):
        self.calls.append((ident, conversation.scope if conversation else ""))
        return None if ident in self.disabled else self.models.get(ident)


class DirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multichat-model-directory-")
        self.instances = []
        self.default = model("host-default")
        self.judge = model("host-judge")
        self.caption = model("host-caption")
        self.directory = Directory()
        self.patches = [patch.object(engine_module, "pace", lambda parts: [("message", x) for x in parts]),
                        patch.object(group_module, "pace", lambda parts: [("message", x) for x in parts])]
        for p in self.patches:
            p.start()
        self.runtime = self.make("one", directory=self.directory)
        self.api = DashboardService(self.runtime)

    def make(self, folder, directory=None, config=None):
        r = Runtime(Path(self.temp.name) / folder, Transport(), lambda c: self.default,
                    {"schedule_mode": "always", "primary_user_id": "service:primary",
                     "dashboard": {"enabled": False}, **(config or {})},
                    ROOT, lambda c: self.judge, lambda c: self.caption,
                    logger=logging.getLogger("test.directory"), model_directory=directory)
        self.instances.append(r)
        return r

    async def asyncTearDown(self):
        for r in self.instances:
            await r.close()
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    async def cancel_batch(self, owner):
        if owner.batch_task:
            owner.batch_task.cancel()
            await asyncio.gather(owner.batch_task, return_exceptions=True)
            owner.batch_task = None

    async def update(self, routes):
        return await self.api.handle("POST", "models/update", body={"routes": routes})

    async def test_legacy_host_has_read_only_routes(self):
        r = self.make("legacy")
        view = r.models_view()
        self.assertFalse(view["supported"])
        self.assertEqual(len(view["routes"]), 15)
        self.assertEqual(view["providers"], [])
        self.assertIs(r.chat_provider("p:other"), self.default)
        self.assertIs(r.judge_provider("g:room"), self.judge)
        self.assertIs(r.caption_provider("p:other", r.llm_for("p:other"), force_text=True), self.caption)
        reply = await DashboardService(r).handle("POST", "models/update", body={"routes": {"summary": "chosen-a"}})
        self.assertEqual(reply["error"]["code"], "model_selection_unsupported")

    async def test_get_contract_and_public_whitelist(self):
        self.directory.extra = [{"id": "disabled", "enabled": False}, {"id": "embedding", "kind": "embedding"},
                                {"id": "chosen-a", "label": "duplicate"}, None,
                                {"id": "extra", "label": "公开", "api_key": "SYNTHETIC_SECRET", "url": "private-host"}]
        view = await self.api.handle("GET", "models")
        self.assertTrue(view["ok"] and view["supported"])
        self.assertEqual(len(view["providers"]), 5)
        self.assertEqual(set(view["providers"][0]), {"id", "label", "model", "modalities", "capabilitiesKnown"})
        self.assertEqual(set(view["routes"][0]), {"id", "label", "hint", "requires", "selected", "effective", "available", "defaultLabel"})
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(view))
        self.assertNotIn("private-host", json.dumps(view))

    async def test_partial_update_does_not_change_host_config(self):
        before = json.dumps(self.runtime.config, sort_keys=True)
        response = await self.update({"private_reply": "chosen-a", "summary": "chosen-b"})
        self.assertTrue(response["ok"])
        self.assertEqual(before, json.dumps(self.runtime.config, sort_keys=True))
        self.assertIs(self.runtime.model_provider("p:one", "private_reply"), self.directory.models["chosen-a"])
        self.assertIs(self.runtime.model_provider("g:one", "summary"), self.directory.models["chosen-b"])
        self.assertIs(self.runtime.model_provider("g:one", "memory"), self.default)
        self.assertIs(self.runtime.model_provider("g:one", "group_reply"), self.default)
        self.assertIs(self.runtime.model_provider("g:one", "window_judge"), self.judge)

    async def test_all_purposes_resolve_and_validate(self):
        response = await self.update({row["id"]: "chosen-a" for row in ROUTES})
        self.assertTrue(response["ok"])
        for row in ROUTES:
            self.assertIs(self.runtime.model_provider("g:room", row["id"]), self.directory.models["chosen-a"])

    async def test_invalid_partial_batch_changes_nothing(self):
        await self.update({"summary": "chosen-a"})
        path = Path(self.runtime.data_root) / "model_selection.json"
        before = path.read_bytes()
        response = await self.update({"summary": "chosen-b", "memory": "not-enabled"})
        self.assertEqual(response["error"]["code"], "invalid_model")
        self.assertEqual(path.read_bytes(), before)
        self.assertIs(self.runtime.model_provider("", "summary"), self.directory.models["chosen-a"])

    async def test_capability_validation_and_unknown_capability(self):
        for purpose in ("thought", "window_judge", "look_judge", "memory", "memory_tags", "caption"):
            response = await self.update({purpose: "text-only"})
            self.assertEqual(response["error"]["code"], "model_capability")
        self.assertTrue((await self.update({"memory_haze": "text-only", "memory": "unknown"}))["ok"])

    async def test_bad_request_shapes(self):
        for routes in (None, [], {}, {"no-purpose": "chosen-a"}, {"summary": None},
                       {"summary": True}, {"summary": " chosen-a"}, {"summary": "x\n"}, {"summary": "x" * 201}):
            result = await self.update(routes)
            self.assertFalse(result["ok"], repr(routes))
        self.assertFalse((await self.api.handle("POST", "models/update", body={"routes": {"summary": ""}, "key": "no"}))["ok"])

    async def test_save_failure_rolls_back(self):
        await self.update({"summary": "chosen-a"})
        with patch("multichat.model_selection._write", side_effect=OSError("synthetic-private-detail")):
            result = await self.update({"summary": "chosen-b"})
        self.assertFalse(result["ok"])
        self.assertNotIn("synthetic-private-detail", json.dumps(result))
        self.assertIs(self.runtime.model_provider("", "summary"), self.directory.models["chosen-a"])

    async def test_persisted_empty_overrides_initial_config(self):
        r = self.make("restart", directory=self.directory, config={"request_models": {"summary": "chosen-a", "memory": "chosen-b"}})
        r.update_model_routes({"summary": ""})
        await r.close()
        again = self.make("restart", directory=self.directory, config={"request_models": {"summary": "chosen-a", "memory": "chosen-b"}})
        self.assertIs(again.model_provider("", "summary"), self.default)
        self.assertIs(again.model_provider("", "memory"), self.directory.models["chosen-b"])
        saved = json.loads((Path(r.data_root) / "model_selection.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, {"version": 1, "routes": {"summary": ""}})

    async def test_disabled_selection_is_unavailable_without_fallback(self):
        await self.update({"private_reply": "chosen-a"})
        self.directory.disabled.add("chosen-a")
        self.assertIsNone(self.runtime.chat_provider("p:one"))
        row = next(r for r in self.runtime.models_view()["routes"] if r["id"] == "private_reply")
        self.assertEqual(row["selected"], "chosen-a")
        self.assertFalse(row["available"])
        result = await self.runtime.llm_for("p:one").chat("system", [{"role": "user", "content": "hello"}])
        self.assertFalse(result.ok)
        self.assertEqual(len(self.default.requests), 0)

    async def test_directory_failure_is_sanitized(self):
        await self.update({"private_reply": "chosen-a"})
        self.directory.failure = True
        self.assertIsNone(self.runtime.chat_provider("p:one"))
        result = await self.api.handle("GET", "models")
        self.assertTrue(result["ok"])
        self.assertEqual(result["providers"], [])
        self.assertNotIn("synthetic-private-detail", json.dumps(result))
        self.assertFalse((await self.update({"summary": "chosen-a"}))["ok"])

    async def test_existing_scope_pins_then_next_scope_changes(self):
        await self.update({"private_reply": "chosen-a"})
        with model_scope("private_reply"):
            self.assertTrue(self.runtime.llm_for("p:one").supports("image"))
            await self.update({"private_reply": "text-only"})
            self.assertIs(self.runtime.chat_provider("p:one"), self.directory.models["chosen-a"])
            self.assertTrue(self.runtime.llm_for("p:one").supports("image"))
        with model_scope("private_reply"):
            self.assertIs(self.runtime.chat_provider("p:one"), self.directory.models["text-only"])
            self.assertFalse(self.runtime.llm_for("p:one").supports("image"))

    async def test_unavailable_provider_is_also_pinned(self):
        await self.update({"summary": "chosen-a"})
        self.directory.disabled.add("chosen-a")
        with model_scope("summary"):
            self.assertIsNone(self.runtime.chat_provider("g:room"))
            self.directory.disabled.clear()
            self.assertIsNone(self.runtime.chat_provider("g:room"))
        with model_scope("summary"):
            self.assertIs(self.runtime.chat_provider("g:room"), self.directory.models["chosen-a"])

    async def test_judge_and_chat_share_pin_but_conversations_do_not(self):
        await self.update({"look_judge": "chosen-a"})
        with model_scope("look_judge"):
            self.assertIs(self.runtime.chat_provider("g:first"), self.directory.models["chosen-a"])
            await self.update({"look_judge": "chosen-b"})
            self.assertIs(self.runtime.judge_provider("g:first"), self.directory.models["chosen-a"])
            self.assertIs(self.runtime.judge_provider("g:second"), self.directory.models["chosen-b"])
        self.assertIn(("chosen-b", "g:second"), self.directory.calls)

    async def test_async_concurrent_scopes_are_isolated(self):
        await self.update({"private_reply": "chosen-a"})
        ready, released = asyncio.Event(), asyncio.Event()
        async def first():
            with model_scope("private_reply"):
                original = self.runtime.chat_provider("p:one")
                ready.set()
                await released.wait()
                return original is self.runtime.chat_provider("p:one")
        task = asyncio.create_task(first())
        await ready.wait()
        await self.update({"private_reply": "chosen-b"})
        with model_scope("private_reply"):
            self.assertIs(self.runtime.chat_provider("p:one"), self.directory.models["chosen-b"])
        released.set()
        self.assertTrue(await task)

    async def test_tool_followup_keeps_original_provider(self):
        a, b = self.directory.models["chosen-a"], self.directory.models["chosen-b"]
        a.responses = [ModelResponse(calls=[ToolCall("call-1", "think", {"reason": "test"})]), ModelResponse(text="结束")]
        await self.update({"private_reply": "chosen-a"})
        async def result_for(call, last):
            await self.update({"private_reply": "chosen-b"})
            return "思考完成"
        with model_scope("private_reply"):
            result = await self.runtime.llm_for("p:one").chat_with_tools(
                "system", [{"role": "user", "content": "question"}],
                [{"name": "think", "description": "think", "parameters": {"type": "object"}}],
                result_for, reads_result=lambda call: call.get("name") == "think")
        self.assertTrue(result.ok)
        self.assertEqual(len(a.requests), 2)
        self.assertEqual(len(b.requests), 0)
        self.assertEqual(len(a.requests[1].tool_history), 1)

    async def test_force_text_caption_uses_independent_purpose(self):
        await self.update({"caption": "chosen-b", "private_reply": "chosen-a"})
        with model_scope("private_reply"):
            llm = self.runtime.llm_for("p:one")
            self.assertIsNone(self.runtime.caption_provider("p:one", llm))
            self.assertIs(self.runtime.caption_provider("p:one", llm, force_text=True), self.directory.models["chosen-b"])
        calls = []
        async def ready(provider, msgs, saved, deadline=None):
            calls.append((provider, deadline))
        with patch.object(self.runtime.captioner, "ready", ready):
            await self.runtime._private_captions("one")([], lambda: None, 123, force_text=True)
            await self.runtime._group_captions("room", [], lambda: None, 456, force_text=True)
        self.assertEqual(calls, [(self.directory.models["chosen-b"], 123), (self.directory.models["chosen-b"], 456)])

    async def test_original_callbacks_remain_per_conversation(self):
        seen = []
        def select(c):
            seen.append(c.scope if c else "")
            return self.default
        self.runtime._provider_for = select
        self.runtime.model_provider("p:one", "memory")
        self.runtime.model_provider("g:two", "summary")
        self.runtime.model_provider(None, "private_reply")
        self.assertEqual(seen, ["p:one", "g:two", ""])

    async def test_closed_runtime_rejects_update(self):
        await self.runtime.close()
        result = await self.update({"summary": "chosen-a"})
        self.assertEqual(result["error"]["code"], "closed")

    async def test_api_methods_are_restricted(self):
        self.assertEqual((await self.api.handle("GET", "models/update"))["error"]["code"], "method_not_allowed")
        self.assertEqual((await self.api.handle("POST", "models"))["error"]["code"], "method_not_allowed")

    async def test_actual_private_entry_routes_and_supplies_plain_identity(self):
        await self.update({"private_reply": "text-only"})
        self.runtime.register("private", "service:primary")
        await self.runtime.receive(IncomingMessage(Conversation("private", "service:primary"),
                                   "service:primary", "来访者", "你是谁？", "incoming-private"))
        owner = self.runtime.engine("service:primary")
        await self.cancel_batch(owner)
        with self.runtime.profile_scope():
            await owner.run_turn()
        requests = self.directory.models["text-only"].requests
        self.assertEqual(len(requests), 1)
        self.assertIn("【按需查看的身份资料】", requests[0].system)
        self.assertIn("小伴", requests[0].system)
        self.assertEqual(len(self.default.requests), 0)

    async def test_actual_group_reply_then_summary_use_distinct_purposes(self):
        await self.update({"group_reply": "chosen-a", "summary": "chosen-b"})
        chat = self.runtime.register("group", "room:actual", name="讨论室")
        await self.runtime.receive(IncomingMessage(Conversation("group", chat.gid),
                                   "service:visitor", "来访者", "我们周末一起读新书，先看看目录如何？", "incoming-group", True))
        await self.cancel_batch(chat)
        with self.runtime.profile_scope():
            await chat.passive_turn()
            chat.round_memory.seal_open()
            await chat.run_history()
        self.assertEqual(len(self.directory.models["chosen-a"].requests), 1)
        self.assertEqual(len(self.directory.models["chosen-b"].requests), 1)
        self.assertEqual(len(self.default.requests), 0)
        self.assertIn("周末", str(self.directory.models["chosen-b"].requests[0].messages))

    async def test_actual_memory_capability_uses_memory_choice_not_plain_chat(self):
        self.default.provider_config["modalities"] = ["text"]
        await self.update({"memory": "chosen-a"})
        self.runtime.register("private", "service:primary")
        owner = self.runtime.engine("service:primary")
        row = {"id": "remember-one", "role": "user", "text": "重要的约定", "ts": 1000, "seen": True}
        async def consolidate(chat, session, now, **kwargs):
            reply = await chat("memory system", [{"role": "user", "content": "重要的约定"}])
            return "ok" if reply.ok else "failed"
        with self.runtime.profile_scope(), patch.object(owner.memory, "pending_session", return_value=[row]), \
                patch.object(owner.memory, "consolidate", consolidate):
            result = await owner.run_memory(force=True)
        self.assertEqual(result, "整理好了")
        self.assertEqual(len(self.directory.models["chosen-a"].requests), 1)
        self.assertEqual(len(self.default.requests), 0)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DirectoryTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"model-directory240 {result.testsRun - len(result.failures) - len(result.errors)}/{result.testsRun}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
