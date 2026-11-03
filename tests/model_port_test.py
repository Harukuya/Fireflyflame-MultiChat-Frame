"""Offline core model contract and optional native adapter regression tests."""
from __future__ import annotations

from pathlib import Path
import asyncio
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from multichat.llm import Llm
from multichat.model_port import ModelRequest, ModelResponse, ScriptedModelProvider, ToolCall


TOOLS = [{"name": "recall", "description": "read a memory", "input_schema": {"type": "object"}}]
MESSAGES = [{"role": "user", "content": "hello"}]


class QuietLog:
    def info(self, *args):
        pass

    def warning(self, *args):
        pass


class Events:
    def __init__(self):
        self.usage, self.stages, self.outcomes = [], [], []

    def record_model_usage(self, category, data):
        self.usage.append(data)

    def record_tool_result(self, category, field):
        self.stages.append(field)

    def record_tool_outcome(self, category, outcome, models):
        self.outcomes.append((outcome, models))


class ModelPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_text_usage_and_identity(self):
        events = Events()
        provider = ScriptedModelProvider([ModelResponse(text="hello back", usage={"input_tokens": 8, "output_tokens": 3})])
        llm = Llm(lambda: provider, QuietLog(), events)
        result = await llm.chat("friendly", MESSAGES)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "hello back")
        self.assertEqual(provider.requests[0].system, "friendly")
        self.assertEqual(events.usage[0]["tokens"], {"inputTokens": 8, "outputTokens": 3})
        self.assertTrue(llm.supports("image"))

    async def test_tool_result_followup_without_platform(self):
        provider = ScriptedModelProvider([
            ModelResponse(text="discard this draft", calls=[ToolCall("c1", "recall", {"about": "books"})],
                          reasoning_content="private thought", reasoning_signature="opaque-signature"),
            ModelResponse(text="I remember the library."),
        ])
        got = []

        async def result_for(call, last):
            got.append((call, last))
            return "fictional library memory"

        result = await Llm(lambda: provider, QuietLog()).chat_with_tools(
            "friendly", MESSAGES, TOOLS, result_for,
            reads_result=lambda call: call["name"] == "recall", max_reads=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "I remember the library.")
        self.assertEqual(len(provider.requests), 2)
        history = provider.requests[1].tool_history
        self.assertEqual(len(history), 1)
        self.assertFalse(history[0].include_text)
        self.assertEqual(history[0].results[0].call_id, "c1")
        self.assertEqual(history[0].results[0].content, "fictional library memory")
        self.assertEqual(history[0].response.reasoning_signature, "opaque-signature")
        self.assertEqual(provider.requests[1].tools, [])
        self.assertEqual(got[0][1], True)

    async def test_required_repair_and_counters(self):
        events = Events()
        provider = ScriptedModelProvider([ModelResponse(text="only prose"), ModelResponse(calls=[ToolCall("c1", "recall", {})])])
        result = await Llm(lambda: provider, QuietLog(), events).chat_with_retry("return result", MESSAGES, TOOLS, "required")
        self.assertTrue(result.ok)
        self.assertEqual(len(provider.requests), 2)
        self.assertTrue(all(req.tool_choice == "auto" for req in provider.requests))
        self.assertEqual(events.stages, ["tasks", "missingResponses", "firstMissing", "repairs", "recovered"])
        self.assertEqual(events.outcomes[0][0], "recovered")
        self.assertEqual(events.outcomes[0][1], [{"provider": "scripted", "model": "offline-example"}])

    async def test_transient_retry_preserves_original_messages(self):
        provider = ScriptedModelProvider([TimeoutError("synthetic timeout"), ModelResponse(text="ready")])
        with patch.object(Llm, "_retry_wait", new=AsyncMock()) as wait:
            result = await Llm(lambda: provider, QuietLog()).chat_with_retry("test", MESSAGES)
        self.assertTrue(result.ok)
        self.assertEqual(wait.await_count, 1)
        self.assertEqual(provider.requests[0].messages, provider.requests[1].messages)

    async def test_repeated_thinking_has_finite_budget(self):
        provider = ScriptedModelProvider([ModelResponse(calls=[ToolCall(f"c{i}", "recall", {})]) for i in range(4)])
        handled = []
        result = await Llm(lambda: provider, QuietLog()).chat_with_tools(
            "test", MESSAGES, TOOLS, lambda call, last: handled.append(call["id"]) or "read",
            reads_result=lambda call: call["name"] == "recall", max_reads=2)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "")
        self.assertEqual(len(provider.requests), 4)  # max_reads + 2
        self.assertEqual(len(result.calls), 4)
        self.assertEqual(handled, ["c0", "c1", "c2"])
        self.assertEqual(provider.requests[2].tools, [])
        self.assertEqual(provider.requests[3].tools, [])

    async def test_unsupported_tools_does_not_strip_or_retry_silently(self):
        class UnsupportedTools(Exception):
            status_code = 400

        events = Events()
        provider = ScriptedModelProvider([UnsupportedTools("tools not supported")])
        result = await Llm(lambda: provider, QuietLog(), events).chat_with_retry("test", MESSAGES, TOOLS, "required")
        self.assertFalse(result.ok)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(provider.requests[0].tools, TOOLS)
        self.assertEqual(events.stages, ["tasks", "failed"])

    async def test_cancellation_propagates_without_retry(self):
        events = Events()
        provider = ScriptedModelProvider([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await Llm(lambda: provider, QuietLog(), events).chat_with_retry("test", MESSAGES)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(events.usage[0]["cancelled"], 1)
        self.assertEqual(events.usage[0]["failed"], 0)

    async def test_empty_responses_exhaust_three_requests(self):
        events = Events()
        provider = ScriptedModelProvider([ModelResponse(), ModelResponse(), ModelResponse()])
        with patch.object(Llm, "_retry_wait", new=AsyncMock()):
            result = await Llm(lambda: provider, QuietLog(), events).chat_with_retry("test", MESSAGES)
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, "empty")
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual([row["failed"] for row in events.usage], [1, 1, 1])

    async def test_invalidated_tool_round_never_dispatches_followup(self):
        provider = ScriptedModelProvider([ModelResponse(calls=[ToolCall("c1", "recall", {})])])
        result = await Llm(lambda: provider, QuietLog()).chat_with_tools(
            "test", MESSAGES, TOOLS, lambda call, last: "memory", reads_result=lambda call: True,
            request_valid=lambda: False)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "")
        self.assertEqual(len(result.calls), 1)
        self.assertEqual(len(provider.requests), 1)

    async def test_tool_handler_may_return_future(self):
        provider = ScriptedModelProvider([ModelResponse(calls=[ToolCall("c1", "recall", {})]), ModelResponse(text="done")])
        future = asyncio.get_running_loop().create_future()
        future.set_result("resolved result")
        result = await Llm(lambda: provider, QuietLog()).chat_with_tools(
            "test", MESSAGES, TOOLS, lambda call, last: future, reads_result=lambda call: True)
        self.assertTrue(result.ok)
        self.assertEqual(provider.requests[1].tool_history[0].results[0].content, "resolved result")

    def test_core_import_does_not_need_astrbot(self):
        script = '''import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "astrbot" or name.startswith("astrbot."):
        raise AssertionError("Core imported AstrBot")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from multichat.llm import Llm
from multichat.model_port import ModelRequest, ModelResponse, ScriptedModelProvider
import asyncio
async def main():
    model = ScriptedModelProvider([ModelResponse(text="portable")])
    result = await Llm(lambda: model, None).chat("", [{"role":"user", "content":"test"}])
    assert result.text == "portable"
asyncio.run(main())
'''
        child = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(ROOT)], capture_output=True)
        self.assertEqual(child.returncode, 0, child.stderr.decode("utf-8", errors="replace"))

    async def test_astrbot_adapter_converts_with_only_stubs(self):
        from adapters.astrbot_model import AstrBotModelProvider

        class Segment:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        modules = {name: ModuleType(name) for name in (
            "astrbot", "astrbot.core", "astrbot.core.agent", "astrbot.core.agent.message",
            "astrbot.core.agent.tool", "astrbot.core.provider", "astrbot.core.provider.entities")}
        for name in ("AssistantMessageSegment", "TextPart", "ThinkPart", "ToolCallMessageSegment"):
            setattr(modules["astrbot.core.agent.message"], name, type(name, (Segment,), {}))
        modules["astrbot.core.agent.tool"].FunctionTool = Segment
        modules["astrbot.core.agent.tool"].ToolSet = list
        modules["astrbot.core.provider.entities"].ToolCallsResult = Segment

        class Native:
            provider_config = {"id": "stub", "model": "stub-model", "api_key": "SYNTHETIC-DO-NOT-EXPOSE"}

            def __init__(self):
                self.requests = []

            async def text_chat(self, **kwargs):
                self.requests.append(kwargs)
                if len(self.requests) == 1:
                    return SimpleNamespace(completion_text="discard me", tools_call_name=["recall"],
                                           tools_call_args=['{"about":"books"}'], tools_call_ids=["native-c1"],
                                           reasoning_content="thought", reasoning_signature="sig",
                                           to_openai_tool_calls_model=lambda: ["native-tool-call"])
                return SimpleNamespace(completion_text="done", tools_call_name=[], tools_call_args=[], tools_call_ids=[])

        native = Native()
        adapter = AstrBotModelProvider(native)
        self.assertNotIn("api_key", adapter.provider_config)
        images = [{"role": "user", "content": [{"type": "image", "source": {"media_type": "image/png", "data": "SYNTHETIC"}}]}]
        with patch.dict(sys.modules, modules):
            result = await Llm(lambda: adapter, QuietLog()).chat_with_tools(
                "test", images, TOOLS, lambda call, last: "result", reads_result=lambda call: True)
        self.assertEqual(result.text, "done")
        self.assertEqual(native.requests[0]["contexts"][0]["content"][0]["type"], "image_url")
        rounds = native.requests[1]["tool_calls_result"]
        self.assertIsInstance(rounds, list)
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0].tool_calls_info.tool_calls, ["native-tool-call"])
        self.assertEqual([type(part).__name__ for part in rounds[0].tool_calls_info.content], ["ThinkPart"])
        self.assertEqual(rounds[0].tool_calls_result[0].tool_call_id, "native-c1")
        self.assertEqual(rounds[0].tool_calls_result[0].content, "result")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
