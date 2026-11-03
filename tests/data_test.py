"""Framework sample/empty-data checks; no model service, platform or user data."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from multichat.data_bundle import DataSource, read_json_resource
from multichat.embed import MODEL_ID, VECTOR_FORMAT
from multichat.kb_match import match_knowledge, prepare, strip_call
from multichat.knowledge import Knowledge
from multichat.people import People
from multichat.style import Style


class NoModel:
    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        raise AssertionError("Lexical samples must not request embeddings")


class QuietLog:
    def info(self, *args):
        pass

    def warning(self, *args):
        pass


class DataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = DataSource(ROOT / "data")
        self.model = NoModel()
        self.kb_data = self.source.read_json("knowledge/kb.json")
        self.kb = Knowledge("", self.model, QuietLog(), data=self.kb_data)

    def test_resource_api_and_counts(self):
        self.assertFalse(self.source.encrypted)
        self.assertEqual(self.kb_data, read_json_resource(ROOT / "data/knowledge/kb.json"))
        self.assertEqual((len(self.kb_data["missions"]), len(self.kb_data["keys"]), len(self.kb_data["self"])), (2, 3, 1))
        self.assertIn("我是小伴", self.source.read_text("self/她对自己的认知.md"))
        self.assertFalse(self.kb.vector_ready)

    def test_resource_path_boundary(self):
        for relative in ("../outside.json", str(ROOT / "main.py")):
            with self.assertRaises(ValueError):
                self.source.read_text(relative)

    async def test_actual_automatic_knowledge_match(self):
        text = await self.kb.lookup("图书馆的阅读角是什么样的？", 1000)
        self.assertIn("靠窗的阅读角", text)
        self.assertIn("虚构", text)
        self.assertEqual([x["m"] for x in self.kb.recent], [0])
        self.assertEqual(self.model.calls, 0)

    async def test_explicit_recall(self):
        text = await self.kb.recall("周末沿河边散步时会注意什么？")
        self.assertIn("植物的新叶", text)
        self.assertNotIn("靠窗的阅读角", text)
        self.assertEqual(self.model.calls, 0)

    async def test_deferred_lookup_does_not_commit_early(self):
        text, commit = await self.kb.lookup_deferred("图书馆阅读角", 1000)
        self.assertIn("阅读角", text)
        self.assertEqual(self.kb.recent, [])
        commit()
        self.assertEqual([x["m"] for x in self.kb.recent], [0])

    async def test_self_and_unrelated_queries(self):
        text = await self.kb.recall("小伴 自我介绍 你是谁")
        self.assertIn("我是小伴", text)
        self.assertIsNone(await self.kb.recall("量子色动力学计算公式"))

    async def test_empty_knowledge_both_metadata_modes(self):
        for data in ({}, {"model": MODEL_ID, "format": VECTOR_FORMAT, "keys": [], "missions": [], "self": [], "anchors": []}):
            kb = Knowledge("", self.model, QuietLog(), data=data)
            self.assertIsNone(await kb.lookup("图书馆", 2000))
            self.assertIsNone(await kb.recall("图书馆"))
            self.assertFalse(kb.vector_ready)
            result = match_knowledge(kb.p, [{"text": "图书馆", "vec": None}], "recall")
            self.assertEqual(result["hits"], [])
        self.assertEqual(self.model.calls, 0)

    async def test_partial_and_mismatched_vectors_fall_back(self):
        for matching in (True, False):
            data = copy.deepcopy(self.kb_data)
            data.update(model=MODEL_ID if matching else "different-model", format=VECTOR_FORMAT)
            data["keys"][0]["vec"] = "invalid-vector-that-must-not-be-used"
            kb = Knowledge("", self.model, QuietLog(), data=data)
            self.assertFalse(kb.vector_ready)
            self.assertIn("阅读角", await kb.recall("图书馆阅读角"))
        self.assertEqual(self.model.calls, 0)

    async def test_people_sample_and_empty(self):
        people = People("", self.model, data=self.source.read_json("people.json"))
        self.assertEqual(len(people.people), 1)
        self.assertFalse(people.vector_ready)
        text = await people.lookup("林舟会怎么介绍书架？")
        self.assertIn("虚构人物样例", text)
        self.assertIn("先告诉我你想了解什么", text)
        self.assertIsNone(await people.lookup("没有提到任何人"))
        self.assertIsNone(await People("", self.model, data={"people": []}).lookup("林舟"))
        self.assertEqual(self.model.calls, 0)

    async def test_style_sample_and_empty(self):
        style = Style("", self.model, data=self.source.read_json("style.json"))
        self.assertEqual(len(style.samples), 2)
        self.assertFalse(style.vector_ready)
        picked = await style.samples_for("散步看见树上新叶", k=1)
        self.assertEqual([x["id"] for x in picked], ["demo-walk-1"])
        self.assertEqual(await style.voice_for("散步"), [])
        empty = Style("", self.model, data={"samples": [], "voice": []})
        self.assertEqual(await empty.samples_for("散步"), [])
        self.assertEqual(await empty.voice_for("散步"), [])
        self.assertEqual(self.model.calls, 0)

    def test_current_character_call_only(self):
        self.assertEqual(strip_call("小伴，在吗？"), "在吗？")
        self.assertEqual(strip_call("好吗，小伴"), "好吗")
        self.assertEqual(strip_call("你名字叫小伴？"), "你名字叫小伴？")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
