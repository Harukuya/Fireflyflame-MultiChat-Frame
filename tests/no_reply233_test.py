"""无需回复：实际 Llm 工具循环、私聊和群流程的离线回归。

python -I -B no_reply233_test.py <源码目录> [复刻包目录] [临时输出目录]
兼容 firefly / multichat；不读取复刻包或真实运行数据，不调用真实服务。
"""
from __future__ import annotations

import asyncio
import copy
import importlib
import inspect
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest


SOURCE = Path(sys.argv[1]).resolve()
OUTPUT = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None
if OUTPUT:
    OUTPUT.mkdir(parents=True, exist_ok=True)
PKG = "multichat" if (SOURCE / "multichat").is_dir() else "firefly"
sys.path.insert(0, str(SOURCE))


class Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


if PKG == "firefly":
    for name in ("astrbot", "astrbot.core", "astrbot.core.agent", "astrbot.core.provider",
                 "astrbot.core.agent.tool", "astrbot.core.agent.message", "astrbot.core.provider.entities"):
        sys.modules[name] = types.ModuleType(name)
    toolmod = sys.modules["astrbot.core.agent.tool"]
    toolmod.FunctionTool = lambda name, description, parameters: Obj(name=name, description=description, parameters=parameters)
    toolmod.ToolSet = lambda tools: Obj(tools=tools)
    for name in ("AssistantMessageSegment", "TextPart", "ThinkPart", "ToolCallMessageSegment"):
        setattr(sys.modules["astrbot.core.agent.message"], name, type(name, (Obj,), {}))
    sys.modules["astrbot.core.provider.entities"].ToolCallsResult = type("ToolCallsResult", (Obj,), {})


def module(name):
    return importlib.import_module(f"{PKG}.{name}")


E, G, S, W = (module(name) for name in ("engine", "group", "schedule", "winext"))
Llm, Store, Her = module("llm").Llm, module("store").Store, module("store").Her
Memory, Embedder = module("memory").Memory, module("embed").Embedder
Events, Intimacy = module("events").Events, module("intimacy").Intimacy
energy = module("energy")
NOW, MIN, HOUR, PRIMARY, PEER = 1793685600000, 60_000, 3_600_000, "10001", "10002"
logging.basicConfig(level=logging.CRITICAL)
LOG = logging.getLogger("no-reply233")
for name, mod in list(sys.modules.items()):
    if name.startswith(PKG + "."):
        if hasattr(mod, "now_ms"):
            mod.now_ms = lambda: NOW
        if hasattr(mod, "state_at"):
            mod.state_at = lambda t: S.HerState(True, "base", "night", 22.0, 0)
for mod in (E, G):
    mod.offline_after = lambda t: NOW + 5 * HOUR
    mod.online_since = lambda t: NOW - HOUR
    mod.pace = lambda parts: [("message", part) for part in parts]


class Response:
    def __init__(self, text="", calls=()):
        self.completion_text = text
        self.tools_call_name = [name for name, args in calls]
        self.tools_call_args = [args for name, args in calls]
        self.tools_call_ids = [f"call-{i}" for i in range(len(calls))]
        self.reasoning_content = self.reasoning_signature = None

    def to_openai_tool_calls_model(self):
        return []


def no_reply(kind="unnecessary", *, legacy=False, extra=(), text=""):
    args = {"reason": "事情已经说清楚了"}
    if not legacy:
        args["kind"] = kind
    return Response(text, [("no_reply", args), *extra])


class Provider:
    """两种边界都使用同一脚本，记录实际送给 Provider 的工具 schema。"""
    def __init__(self):
        self.provider_config = {"id": "offline-test", "model": "scripted",
                                "modalities": ["text", "image", "tool_use"]}
        self.requests, self.script = [], []

    async def next(self):
        step = self.script.pop(0) if self.script else Response("收到新的问题了。")
        if callable(step):
            step = step()
        if inspect.isawaitable(step):
            step = await step
        if isinstance(step, Exception):
            raise step
        return step

    async def text_chat(self, **kwargs):
        tools = getattr(kwargs.get("func_tool"), "tools", [])
        self.requests.append({"tools": [{"name": t.name, "input_schema": t.parameters} for t in tools],
                              "messages": kwargs.get("contexts", []), "system": kwargs.get("system_prompt", "")})
        return await self.next()

    async def complete(self, request):
        self.requests.append({"tools": request.tools, "messages": request.messages, "system": request.system})
        response = await self.next()
        port = module("model_port")
        return port.ModelResponse(response.completion_text, [
            port.ToolCall(id_, name, args) for id_, name, args in zip(
                response.tools_call_ids, response.tools_call_name, response.tools_call_args)])


class Sender:
    def __init__(self):
        self.sent, self.images = [], []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return True

    async def send_image(self, *args, **kwargs):
        self.images.append((args, kwargs))
        return True

    async def typing(self):
        pass


class Stickers:
    def can_send(self):
        return True

    def tool(self):
        return {"name": "send_sticker", "description": "发送表情",
                "input_schema": {"type": "object", "properties": {"emotion": {"type": "string"}}}}

    def choose(self, emotion):
        raise AssertionError("选择无需回复时不应挑选或发送表情")


class NoReplyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="no-reply233-", dir=str(OUTPUT) if OUTPUT else None)
        self.root = Path(self.tmp.name)
        self.her = Her(str(self.root))
        self.her.energy = {**energy.fresh_energy(NOW), "value": 75}
        self.events = Events(str(self.root), LOG)
        self.intimacy = Intimacy(str(self.root), PRIMARY)
        self.provider = Provider()
        self.llm = Llm(lambda: self.provider, LOG, self.events)
        self.emb = Embedder(LOG)
        self.sender = Sender()
        self.targets, self.tasks = [], []
        self.opts = {"decline_trailblazer": False, "trailblazer": PRIMARY, "look_per_hour": 0}
        self.hub = G.GroupHub(self.her, self.llm, self.llm, None, None, None, Stickers(), self.intimacy,
                             self.sender, self.events, PRIMARY, lambda: self.opts, lambda gid: "测试群" + gid,
                             lambda delta: None, self.recall)

    async def recall(self, memory, about, msgs):
        raise AssertionError("无需回复的终止工具不应再调用回忆")

    async def asyncTearDown(self):
        active = set(self.tasks)
        for target in self.targets:
            active.update(target._running)
            active.update(t for t in (getattr(target, "batch_task", None), getattr(target, "req_task", None),
                                     getattr(target, "window_task", None)) if t)
            target.shutdown()
        self.hub.shutdown()
        for task in active:
            if not task.done():
                task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self.tmp.cleanup()

    def engine(self, peer=False):
        store = Store(str(self.root / f"p{len(self.targets)}"), self.her)
        result = E.Engine(store, self.llm, Memory(store, self.emb, LOG, enabled=lambda: False),
                          None, None, None, self.sender, LOG, events=self.events, stickers=Stickers(),
                          intimacy=self.intimacy, options=lambda: self.opts, scope="p:" + (PEER if peer else PRIMARY),
                          peer={"qq": PEER, "name": "测试群友"} if peer else None)
        result.schedule_turn = lambda: None  # 不等真实合并时钟；每一轮由测试显式触发。
        result.mem_busy = True
        result._should_reach_out = lambda now: False
        self.targets.append(result)
        return result

    def group(self):
        gid = str(20000 + len(self.targets))
        store = Store(str(self.root / ("g" + gid)), self.her)
        result = G.GroupChat(gid, self.hub, store, Memory(store, self.emb, LOG, enabled=lambda: False))
        result.schedule = lambda passive=True: None
        result.mem_busy = True
        self.targets.append(result)
        self.hub.chats[gid] = result
        return result

    async def private_message(self, engine, text="好的，事情说完了"):
        await engine.on_user_message(text, [], "p-message-" + str(len(engine.history)))
        return engine.history[-1]

    async def group_message(self, chat, primary=False, text="知道了，谢谢"):
        await chat.on_message(PRIMARY if primary else PEER, "测试用户", text, [], "g-message-" + str(len(chat.msgs)), True)
        return chat.msgs[-1]

    def kinds(self):
        tool = next(t for t in self.provider.requests[-1]["tools"] if t["name"] == "no_reply")
        return tool["input_schema"]["properties"]["kind"]["enum"]

    def quiet(self, before_energy, before_mood):
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.sender.images, [])
        self.assertEqual(self.her.energy, before_energy)
        self.assertEqual(self.her.mood, before_mood)

    async def test_01_primary_disabled_still_has_unnecessary(self):
        e = self.engine()
        await self.private_message(e)
        self.provider.script = [no_reply()]
        await e.run_turn()
        self.assertEqual(self.kinds(), ["unnecessary"])
        self.assertEqual(self.events.today().get("无需回复"), 1)
        self.assertEqual(self.events.today().get("不想回", 0), 0)

    async def test_02_peer_private_allows_both(self):
        e = self.engine(peer=True)
        await self.private_message(e)
        self.provider.script = [no_reply()]
        await e.run_turn()
        self.assertEqual(self.kinds(), ["unnecessary", "unwilling"])

    async def test_03_group_peer_allows_both(self):
        c = self.group()
        m = await self.group_message(c)
        self.provider.script = [no_reply()]
        self.assertTrue(await c.turn("called", [m["id"]]))
        self.assertEqual(self.kinds(), ["unnecessary", "unwilling"])

    async def test_04_group_primary_disabled_keeps_unnecessary(self):
        c = self.group()
        m = await self.group_message(c, primary=True)
        self.provider.script = [no_reply()]
        await c.turn("called", [m["id"]])
        self.assertEqual(self.kinds(), ["unnecessary"])

    async def test_05_private_final_suppresses_all_competing_actions(self):
        e = self.engine()
        m = await self.private_message(e)
        before = copy.deepcopy((self.her.energy, self.her.mood))
        self.provider.script = [no_reply(extra=[("think", {"thought": "再想想"}),
            ("send_sticker", {"emotion": "开心"}), ("keep_thought", {"topic": "追问", "when": "马上"}),
            ("round_feeling", {"energy": "累了", "real": True, "curious": "还要追问吗"})], text="不该发送的正文")]
        await e.run_turn()
        self.quiet(*before)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertTrue(m.get("seen"))
        self.assertFalse(m.get("answered"))
        self.assertEqual(m.get("noReplyKind"), "unnecessary")
        self.assertIsNone(e.store.thought)
        self.assertFalse(e.last_meaningful)
        self.assertEqual(e._pending(), [])
        shared = next(row for row in e.recent_snapshot()["rows"] if row["id"] == m["id"])
        self.assertEqual(shared.get("noReplyKind"), "unnecessary")
        self.assertIsInstance(shared["noReplyKind"], str)

    async def test_06_old_answered_message_is_not_redeclined(self):
        e = self.engine()
        old = await self.private_message(e, "以前的问题")
        old.update(seen=True, answered=True)
        e.history.append(Store.new_msg("her", "以前已经答过"))
        latest = await self.private_message(e)
        self.provider.script = [no_reply()]
        await e.run_turn()
        self.assertTrue(old["answered"])
        self.assertNotIn("declined", old)
        self.assertNotIn("noReplyKind", old)
        self.assertEqual(latest.get("noReplyKind"), "unnecessary")

    async def test_07_legacy_reason_only_retains_unwilling_counter(self):
        e = self.engine(peer=True)
        m = await self.private_message(e)
        self.provider.script = [no_reply(legacy=True)]
        await e.run_turn()
        self.assertEqual(m.get("noReplyKind"), "unwilling")
        self.assertEqual(self.events.today().get("不想回"), 1)
        self.assertEqual(self.events.today().get("无需回复", 0), 0)

    async def test_08_catchup_clears_batch_and_carried_thought(self):
        e = self.engine()
        m = await self.private_message(e)
        m["undelivered"] = True
        m["ts"] = NOW - HOUR
        e.store.thought = {"id": "old-thought", "topic": "已说清楚", "tier": "later",
                           "createdAt": NOW - MIN, "dueAt": NOW + MIN, "waited": False}
        before = copy.deepcopy((self.her.energy, self.her.mood))
        self.provider.script = [no_reply(text="不该补发的正文")]
        await e._catchup_now()
        self.assertEqual([t["name"] for t in self.provider.requests[0]["tools"]], ["no_reply"])
        self.assertIn("【这轮是否开口】", self.provider.requests[0]["system"])
        self.assertIn("只在你决定回复时适用", self.provider.requests[0]["system"])
        self.assertTrue(self.provider.requests[0]["system"].endswith("不必为了交代刚上线而另发一句。"))
        self.assertFalse(m.get("undelivered"))
        self.assertTrue(m.get("seen"))
        self.assertFalse(m.get("answered"))
        self.assertIsNone(e.store.thought)
        self.assertEqual(m.get("noReplyKind"), "unnecessary")
        await e.heartbeat()
        await e.heartbeat()
        self.assertEqual(len(self.provider.requests), 1)
        self.quiet(*before)

    async def test_09_new_message_during_request_survives_and_can_reply(self):
        e = self.engine()
        old = await self.private_message(e)
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold():
            entered.set()
            await release.wait()
            return no_reply()

        self.provider.script = [hold, Response("新的问题我看到了。")]
        task = asyncio.create_task(e.run_turn())
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 3)
        self.assertTrue(e.req_task is not None and not e.req_task.done())
        e.restarted = True  # 测试第二次请求不再被新话取消的真实分支。
        new = await self.private_message(e, "等等，我还有一个新问题")
        release.set()
        await asyncio.wait_for(task, 3)
        self.assertEqual(old.get("noReplyKind"), "unnecessary")
        self.assertFalse(new.get("seen"))
        self.assertFalse(new.get("declined"))
        self.assertFalse(new.get("answered"))
        self.assertEqual(len(self.provider.requests), 1)
        await e.run_turn()
        self.assertTrue(new.get("answered"))
        self.assertFalse(new.get("declined"))
        self.assertEqual(len(self.provider.requests), 2)
        self.assertTrue(self.sender.sent)

    async def test_10_group_end_topic_closes_only_current_window(self):
        c, other = self.group(), self.group()
        for chat in (c, other):
            chat.store.extra.update(windowUntil=NOW + 5 * MIN, windowMisses=0)
        m = await self.group_message(c)
        self.provider.script = [no_reply(extra=[("end_topic", {})])]
        await c.turn("window", [m["id"]])
        self.assertFalse(c.window_open(NOW))
        self.assertEqual(c.store.extra.get("windowMisses"), 0)
        self.assertTrue(other.window_open(NOW))
        self.assertEqual(other.store.extra.get("windowMisses"), 0)
        self.assertEqual(len(self.provider.requests), 1)

    async def test_11_group_no_reply_alone_keeps_first_miss_window(self):
        c = self.group()
        c.store.extra.update(windowUntil=NOW + 5 * MIN, windowMisses=0)
        m = await self.group_message(c)
        self.provider.script = [no_reply()]
        await c.turn("window", [m["id"]])
        self.assertTrue(c.window_open(NOW))
        self.assertEqual(c.store.extra.get("windowMisses"), 1)

    async def test_12_group_catchup_clears_left_and_promised(self):
        c = self.group()
        m = await self.group_message(c, primary=True)
        m["left"] = True
        c.store.extra["promised"] = NOW - HOUR
        self.provider.script = [no_reply()]
        await c.catchup()
        self.assertFalse(m.get("left"))
        self.assertFalse(c.store.extra.get("promised"))
        self.assertTrue(m.get("read"))
        self.assertTrue(m.get("shown"))
        self.assertFalse(m.get("answered"))
        await c.catchup()
        self.assertEqual(len(self.provider.requests), 1)

    async def test_13_group_final_suppresses_text_sticker_and_settlement(self):
        c = self.group()
        m = await self.group_message(c)
        before = copy.deepcopy((self.her.energy, self.her.mood))
        self.provider.script = [no_reply(extra=[("think", {"thought": "想一想"}),
            ("send_sticker", {"emotion": "开心"}), ("round_feeling", {"energy": "累了", "real": True})],
            text="不该发到群的正文")]
        await c.turn("called", [m["id"]])
        self.quiet(*before)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertTrue(m.get("read"))
        self.assertTrue(m.get("shown"))
        self.assertFalse(m.get("answered"))
        self.assertEqual(m.get("noReplyKind"), "unnecessary")
        self.assertEqual(self.events.today().get("无需回复"), 1)

    async def test_14_private_without_tools_uses_plain_reply(self):
        self.provider.provider_config["modalities"] = ["text", "image"]
        e = self.engine()
        m = await self.private_message(e, "这件事怎么办？")
        self.provider.script = [Response("可以这样处理。")]
        await e.run_turn()
        self.assertEqual(self.provider.requests[0]["tools"], [])
        self.assertTrue(m.get("answered"))
        self.assertTrue(self.sender.sent)

    async def test_15_group_without_tools_uses_plain_reply(self):
        self.provider.provider_config["modalities"] = ["text", "image"]
        c = self.group()
        m = await self.group_message(c, text="这个问题怎么办？")
        self.provider.script = [Response("我觉得可以这样做。")]
        await c.turn("called", [m["id"]])
        self.assertEqual(self.provider.requests[0]["tools"], [])
        self.assertTrue(m.get("answered"))
        self.assertTrue(self.sender.sent)

    async def test_16_legacy_disabled_primary_coerces_to_unnecessary(self):
        e = self.engine()
        m = await self.private_message(e)
        self.provider.script = [no_reply(legacy=True)]
        await e.run_turn()
        self.assertEqual(m.get("noReplyKind"), "unnecessary")
        self.assertEqual(self.events.today().get("无需回复"), 1)
        self.assertEqual(self.events.today().get("不想回", 0), 0)

    async def test_17_unwilling_and_unnecessary_have_distinct_persistent_marks(self):
        self.opts["decline_trailblazer"] = True
        e = self.engine()
        m = await self.private_message(e)
        self.provider.script = [no_reply("unwilling")]
        await e.run_turn()
        self.assertEqual(self.kinds(), ["unnecessary", "unwilling"])
        self.assertEqual(self.events.today().get("不想回"), 1)
        restored = Store(e.store.root, self.her)
        self.assertEqual(restored.messages[-1].get("noReplyKind"), "unwilling")
        self.assertIn("你没回", W.no_reply_mark(restored.messages[-1]))
        self.assertIn("无需回复", W.no_reply_mark({"declined": True, "noReplyKind": "unnecessary"}))
        self.assertIn("你没回", W.no_reply_mark({"declined": True}))
        self.assertEqual(W.no_reply_mark({}), "")

    async def test_18_group_legacy_reason_only_and_second_miss(self):
        c = self.group()
        c.store.extra.update(windowUntil=NOW + 5 * MIN, windowMisses=1)
        m = await self.group_message(c)
        self.provider.script = [no_reply(legacy=True)]
        await c.turn("window", [m["id"]])
        self.assertEqual(m.get("noReplyKind"), "unwilling")
        self.assertEqual(self.events.today().get("不想回"), 1)
        self.assertFalse(c.window_open(NOW))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NoReplyTest)
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    passed = result.testsRun - len(result.failures) - len(result.errors)
    print(f"no_reply233: {passed}/{result.testsRun} ({PKG})")
    raise SystemExit(0 if result.wasSuccessful() else 1)
