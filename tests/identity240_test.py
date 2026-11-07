"""On-demand identity cards, including real private/group entry points.

Fake models and temporary data only; source prompts/data are never modified.
python -I -B identity240_test.py <source> [reference] [temporary root]
"""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

spec = importlib.util.spec_from_file_location("_identity240_fixture", Path(__file__).with_name("no_reply233_test.py"))
F = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = F
spec.loader.exec_module(F)
I, P, T = [F.module(name) for name in ("identity", "persona", "tools")]
SELF = """# 自认
## 给她的
### 我是谁
我的名字是小青，因为喜欢青色。这段是名字资料标记。
### 我的身体
我有一副测试装甲。身体正在恢复，尚未痊愈。身体资料标记。
### 我的几段日子（按先后）
以前我在测试书屋工作，后来搬到了另一座城。过去资料标记。
### 我身边的人
蓝舟是我的伙伴，我们一起整理旧书。伙伴资料标记。
### 现在
我现在没有任务，在旧测试基地休养。旧时期资料标记。
### 我还不知道的
我还不知道未公开的航线。认知边界资料标记。
## 出处
HIDDEN_SOURCE_SHOULD_NEVER_APPEAR
"""
SENTENCE = "不要轻易用抛个问题的方式收尾，除非你真的觉得需要。"


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = F.NoReplyTest.asyncTearDown
    engine = F.NoReplyTest.engine
    group = F.NoReplyTest.group
    recall = F.NoReplyTest.recall
    private_message = F.NoReplyTest.private_message
    group_message = F.NoReplyTest.group_message

    async def asyncSetUp(self):
        await F.NoReplyTest.asyncSetUp(self)
        before = P._SELF
        self.addCleanup(setattr, P, "_SELF", before)
        P.load_self(SELF)

    def test_01_core_stays_long_self_leaves_normal_prompt(self):
        text = P.build_system_prompt(channel="tools")
        self.assertIn(P.identity_core(), text)
        self.assertNotIn("身体资料标记", text)
        self.assertNotIn("过去资料标记", text)
        self.assertIn("source=identity", text)
        self.assertIn(P.VOICE, text)
        self.assertIn(P.KNOWLEDGE_BOUNDARY, text)
        self.assertIn(P.FORMAT, text)

    def test_02_no_tool_prompt_does_not_advertise_unavailable_tool(self):
        text = P.build_system_prompt(channel="plain")
        self.assertNotIn("source=identity", text)
        self.assertNotIn("recall", I.hint(False))
        self.assertNotIn("身体资料标记", text)

    def test_03_user_requested_question_ending_boundary_exact(self):
        self.assertIn("**" + SENTENCE + "**", P.PROHIBITIONS)
        self.assertIn(SENTENCE, P.build_system_prompt())

    def test_04_name_lookup_is_relevant_and_bounded(self):
        result = I.query("你叫什么名字")
        self.assertIn("名字资料标记", result)
        self.assertNotIn("身体资料标记", result)
        self.assertNotIn("伙伴资料标记", result)
        self.assertLessEqual(len(result), 1800)

    def test_05_body_lookup_does_not_dump_other_chapters(self):
        result = I.query("你的身体好了吗")
        self.assertIn("身体资料标记", result)
        self.assertNotIn("过去资料标记", result)
        self.assertNotIn("名字资料标记", result)

    def test_06_companion_lookup_supports_entity_name(self):
        result = I.query("蓝舟是谁")
        self.assertIn("伙伴资料标记", result)
        self.assertNotIn("身体资料标记", result)

    def test_07_old_now_carries_historical_boundary(self):
        result = I.query("旧测试基地休养")
        self.assertIn("旧时期资料标记", result)
        self.assertIn("背景时期", result)
        self.assertIn("当前活动以本轮作息和日程为准", result)
        self.assertIn("不是本轮发言或说话风格范例", result)

    def test_08_unknown_topic_returns_topics_not_all_data(self):
        result = I.query("不存在的主题")
        self.assertIn("没有提供", result)
        self.assertIn("可查主题", result)
        self.assertNotIn("身体资料标记", result)

    def test_09_empty_query_does_not_dump_self(self):
        result = I.query("")
        self.assertIn("可查主题", result)
        self.assertNotIn("名字资料标记", result)

    def test_10_source_notes_never_reach_card(self):
        self.assertNotIn("HIDDEN_SOURCE", I.query("身份 出处"))
        self.assertNotIn("HIDDEN_SOURCE", P.identity_self())

    def test_11_schema_keeps_all_three_sources(self):
        prop = T.RECALL["input_schema"]["properties"]
        self.assertEqual(set(prop["source"]["enum"]), {"memory", "schedule", "identity"})
        self.assertIn("day", prop)
        self.assertIn("period", prop)
        self.assertIn("source=identity", T.RECALL["description"])

    def test_12_only_identity_source_intercepts(self):
        for source in (None, "memory", "schedule"):
            self.assertIsNone(I.call_result({"name": "recall", "input": {"source": source, "about": "身体"}}))
        self.assertIsNone(I.call_result({"name": "think", "input": {"source": "identity"}}))
        self.assertIsNone(I.call_result({"name": "recall", "input": []}))

    def test_13_invalid_identity_arguments_do_not_fall_through(self):
        for about in (None, [], 12, "字" * 2001):
            result = I.call_result({"name": "recall", "input": {"source": "identity", "about": about}})
            self.assertIsInstance(result, str)
            self.assertLessEqual(len(result), I.MAX_CHARS)
            self.assertNotIn("身体资料标记", result)

    def test_14_current_location_and_tasks_stay_in_schedule(self):
        for text in ("你现在在哪", "你今天有什么安排", "你昨天的任务是什么", "你刚才在忙什么"):
            self.assertIsNone(I.text_query(text), text)
            result = I.query(text)
            self.assertIn("近期日程", result)
            self.assertNotIn("旧时期资料标记", result)

    def test_15_plain_direct_body_question_gets_local_card(self):
        result = I.text_query("你的身体好了吗？")
        self.assertIn("身体资料标记", result)

    def test_16_plain_smalltalk_and_statements_do_not_retrieve(self):
        for text in ("你真好", "今天午饭不错", "我身体不好", "你的身体好了我很高兴", "你以前的故事很精彩", "我问你叫什么名字", "他说“你的身体好了吗”"):
            self.assertIsNone(I.text_query(text), text)

    def test_17_plain_only_quote_question_is_not_a_new_request(self):
        self.assertIsNone(I.messages_query([{"role": "user", "text": "嗯", "quote": {"text": "你的身体好了吗"}}]))

    def test_18_plain_filters_system_old_and_withdrawn_messages(self):
        for flag in ({"system": True}, {"answered": True}, {"recalled": True}, {"role": "her"}):
            self.assertIsNone(I.messages_query([{"role": "user", "text": "你的身体好了吗", **flag}]))

    def test_19_plain_uses_only_passed_messages(self):
        result = I.messages_query([{"role": "user", "text": "早安"}, {"role": "user", "text": "你叫什么名字"}])
        self.assertIn("名字资料标记", result)
        self.assertLessEqual(len(result), I.MAX_CHARS)

    def test_20_large_chapters_have_one_total_budget(self):
        long = "## 给她的\n### 我的身体\n" + "正文甲。" * 900 + "目标装甲。" + "正文乙。" * 900 + "\n### 我的过去\n" + "旧经历。" * 1800
        P.load_self(long)
        result = I.query("目标装甲和过去")
        self.assertLessEqual(len(result), 1800)
        self.assertIn("目标装甲", result)
        self.assertIn("（节选）", result)
        self.assertLessEqual(result.count("\n\n【"), 2)

    def test_21_very_many_topics_cannot_expand_unknown_result(self):
        P.load_self("## 给她的\n" + "\n".join(f"### 自定义章节{i}\n不同资料{i}。" for i in range(400)))
        self.assertLessEqual(len(I.query("不匹配的请求")), 1800)

    def test_22_primary_only_background_is_not_available_elsewhere(self):
        if F.PKG == "firefly":
            primary = I.query("通讯线路 后门", private=True)
            others = I.query("通讯线路 后门 账号", private=False)
            self.assertIn("后门", primary)
            self.assertNotIn("后门", others)
            self.assertNotIn("信息是空的", others)
            self.assertNotIn("后门", P.build_system_prompt(identity_line="你在群里聊天。"))
        else:
            self.assertNotIn("后门", I.query("通讯线路", private=False))
            self.assertEqual(P.identity_background(private=False), "")

    def test_23_framework_profiles_or_personal_reload_stay_isolated(self):
        if F.PKG == "multichat":
            profile = F.module("profile")
            a, b = dict(profile._DEFAULTS), dict(profile._DEFAULTS)
            a["self_block"] = P.build_self_block(SELF.replace("小青", "角色甲"))
            b["self_block"] = P.build_self_block(SELF.replace("小青", "角色乙"))
            with profile.use(a):
                self.assertIn("角色甲", I.query("你叫什么名字"))
                with profile.use(b):
                    self.assertIn("角色乙", I.query("你叫什么名字"))
                    self.assertNotIn("角色甲", I.query("你叫什么名字"))
                self.assertIn("角色甲", I.query("你叫什么名字"))
        else:
            P.load_self(SELF.replace("小青", "角色甲"))
            self.assertIn("角色甲", I.query("你叫什么名字"))
            P.load_self(SELF.replace("小青", "角色乙"))
            self.assertNotIn("角色甲", I.query("你叫什么名字"))

    async def test_24_private_tool_identity_never_calls_memory_or_knowledge(self):
        e = self.engine()
        def forbidden(*a, **kw): raise AssertionError("identity must not run retrieval")
        e._recall_all = forbidden
        result = e._tool_result({"name": "recall", "input": {"source": "identity", "about": "身体"}}, [])
        self.assertFalse(inspect.isawaitable(result))
        self.assertIn("身体资料标记", result)

    async def test_25_peer_tool_does_not_reveal_private_background(self):
        e = self.engine(peer=True)
        result = e._tool_result({"name": "recall", "input": {"source": "identity", "about": "线路 后门"}}, [])
        self.assertNotIn("后门", result)
        self.assertNotIn("信息是空的", result)

    async def test_26_group_tool_does_not_call_ordinary_recall(self):
        c = self.group()
        result = await c._tool_result({"name": "recall", "input": {"source": "identity", "about": "身体"}})
        self.assertIn("身体资料标记", result)

    async def test_27_private_plain_actual_request_contains_only_relevant_card(self):
        self.provider.provider_config["modalities"] = ["text"]
        e = self.engine()
        await self.private_message(e, "你的身体好了吗？")
        await e.run_turn()
        self.assertTrue(self.provider.requests)
        sent = str(self.provider.requests[-1])
        self.assertIn("身体资料标记", sent)
        self.assertNotIn("伙伴资料标记", sent)
        self.assertNotIn("source=identity", sent)

    async def test_28_group_plain_actual_request_contains_card(self):
        self.provider.provider_config["modalities"] = ["text"]
        c = self.group()
        row = await self.group_message(c, text="你叫什么名字？")
        await c.turn("called", [row["id"]])
        self.assertTrue(self.provider.requests)
        self.assertIn("名字资料标记", str(self.provider.requests[-1]))

    async def test_29_plain_catchup_backlog_is_still_considered(self):
        self.provider.provider_config["modalities"] = ["text"]
        e = self.engine()
        row = await self.private_message(e, "你的身体好了吗")
        row.update(ts=F.NOW - F.HOUR, undelivered=True)
        await e._catchup_now()
        self.assertTrue(self.provider.requests)
        self.assertIn("身体资料标记", str(self.provider.requests[-1]))

    async def test_30_private_smalltalk_request_has_no_self_payload(self):
        self.provider.provider_config["modalities"] = ["text"]
        e = self.engine()
        await self.private_message(e, "今天午饭还不错")
        await e.run_turn()
        self.assertTrue(self.provider.requests)
        self.assertNotIn("身体资料标记", str(self.provider.requests[-1]))
        self.assertNotIn("名字资料标记", str(self.provider.requests[-1]))

    def test_31_tool_channel_can_explicitly_disable_identity_tool_hint(self):
        normal = P.build_system_prompt(channel="tools")
        restricted = P.build_system_prompt(channel="tools", identity_tools=False)
        self.assertIn("source=identity", normal)
        self.assertNotIn("source=identity", restricted)
        self.assertIn(P.FORMAT_TOOLS, restricted)

    async def test_32_catchup_only_no_reply_does_not_advertise_recall(self):
        e = self.engine()
        row = await self.private_message(e, "你的身体好了吗")
        row["undelivered"] = True
        await e._catchup_now()
        self.assertEqual([t["name"] for t in self.provider.requests[-1]["tools"]], ["no_reply"])
        self.assertNotIn("source=identity", self.provider.requests[-1]["system"])
        self.assertIn("身体资料标记", self.provider.requests[-1]["system"])

    async def test_33_thought_wait_or_drop_does_not_advertise_recall(self):
        e = self.engine()
        e.history.append(e.store.new_msg("her", "刚才那件事", ts=F.NOW - F.HOUR))
        e.store.thought = {"id": "identity-thought", "topic": "后续", "tier": "soon", "createdAt": F.NOW - F.HOUR,
                           "dueAt": F.NOW - 1, "waited": False, "planted": True}
        self.provider.script = [F.Response(calls=[("let_go", {})])]
        await e._maybe_recheck(F.NOW)
        self.assertTrue(self.provider.requests)
        self.assertNotIn("recall", [t["name"] for t in self.provider.requests[-1]["tools"]])
        self.assertNotIn("source=identity", self.provider.requests[-1]["system"])

    async def test_34_proactive_only_let_go_does_not_advertise_recall(self):
        e = self.engine()
        e.history.append(e.store.new_msg("her", "上次那件事", ts=F.NOW - 6 * F.HOUR))
        e._should_reach_out = lambda now: True
        await e._maybe_reach_out(F.NOW)
        self.assertEqual([t["name"] for t in self.provider.requests[-1]["tools"]], ["let_go"])
        self.assertNotIn("source=identity", self.provider.requests[-1]["system"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(IdentityTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"identity240 {result.testsRun - len(result.failures) - len(result.errors)}/{result.testsRun}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
