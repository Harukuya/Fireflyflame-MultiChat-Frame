"""Read-only universe API: synthetic records, no files/models/platform calls.

python -I -B universe_test.py <plugin or framework directory> [reference] [temp]
"""
from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
PACKAGE = "firefly" if (ROOT / "firefly").is_dir() else "multichat"
U = importlib.import_module(PACKAGE + ".universe")
M = importlib.import_module(PACKAGE + ".memory")
DAY, NOW = 86400000, 1794009600000


def forbidden(*args, **kwargs):
    raise AssertionError("Reading the universe must not invoke a mutator/service")


class FakeMemory:
    def __init__(self, data):
        self.store = NS(memory=data, save_memory=forbidden, save_memory_vec=forbidden)
        self.enabled = self.expire_low = self.fade = self.recall = self.associate = forbidden

    @property
    def m(self):
        raise AssertionError("Use the loaded storage, not an arbitrary getter")


def episode(eid=1, **changes):
    row = dict(id=eid, start=NOW-DAY, end=NOW-DAY+60000, summary="完整摘要，提到秘密细节。", gist="只记得去过书店。",
               feeling="放松", weight=3, level=0, topics=["夜空"], recalls=0, lastRecalledAt=0)
    row.update(changes)
    return row


def fixture():
    mem = {"episodes": [episode()], "facts": [{"id": 2, "content": "同名者的偏好", "category": "爱好",
        "subject": "user-a", "subjectName": "同名", "createdAt": NOW-DAY, "updatedAt": NOW-DAY, "topics": ["夜空"]}],
        "words": [{"id": 3, "text": "我会记得这片星空。", "about": "夜晚", "ts": NOW-DAY, "episode": 1}],
        "threads": [{"id": 4, "content": "还没决定的打算", "createdAt": NOW-DAY, "expiresAt": NOW+DAY, "kind": "计划", "reason": "尚未决定"}],
        "haze": [{"id": 5, "month": "2026-10", "text": "那阵子常聊书与天气。"}],
        "doneMsgId": "m-4", "doneTs": NOW-DAY, "seq": 6}
    packs = [{"id": "pack-a", "state": "done", "start": NOW-100000, "end": NOW-60000, "summary": "我听大家聊了夜空。",
              "topics": ["夜空"], "people": {"user-a": "同名", "user-b": "同名"}, "cold": False,
              "anchors": ["观星"], "unresolved": ["天气还没确定"], "createdAt": NOW-50000,
              "vector": [999999], "imagePath": "DO_NOT_EXPORT_PATH", "ids": ["m-x"]}]
    p = NS(memories={"g:demo": FakeMemory(mem)},
        sessions=NS(private={}, group={"demo": {"name": "示例群", "enabled": True, "listed": True, "memory": True}}, save=forbidden),
        hub=NS(chats={"demo": NS(round_memory=NS(_records=packs, _save=forbidden, recall_note=forbidden), memory_session=forbidden)}),
        intimacy=NS(get=forbidden), scope_label=forbidden, memory_history=forbidden,
        _sync_sessions=forbidden, universe_account_namespace="test-platform", _stopped=False)
    return p, mem, packs


class UniverseTests(unittest.TestCase):
    def setUp(self):
        self.p, self.m, self.packs = fixture()

    def call(self, operation="items", params=None, now=NOW):
        return U.handle(self.p, operation, params, now=now)

    def items(self, **query):
        result = self.call(params=query)
        self.assertTrue(result["ok"], result)
        return result["data"]["items"]

    def detail(self, kind):
        node = self.items(kinds=kind)[0]
        result = self.call("item", {"id": node["id"]})
        self.assertTrue(result["ok"], result)
        return result["data"]

    def test_01_full_schema_and_six_types(self):
        result = self.call("meta")
        self.assertEqual(result["schemaVersion"], 1)
        self.assertEqual(result["generatedAt"], NOW)
        self.assertIsInstance(result["utcOffsetMinutes"], int)
        self.assertEqual(result["data"]["kinds"], list(U.KINDS))
        self.assertEqual({n["kind"] for n in self.items()}, set(U.KINDS))
        self.assertEqual(result["data"]["capabilities"], dict(readOnly=True, rawChat=False, requestHistory=False, mutations=False, personImpression=False))

    def test_02_reads_do_not_write_or_call_services(self):
        before = copy.deepcopy((self.m, self.packs, self.p.sessions.group))
        self.call("meta")
        for n in self.items():
            self.call("item", {"id": n["id"]})
        self.assertEqual((self.m, self.packs, self.p.sessions.group), before)

    def test_03_return_values_are_detached_from_storage(self):
        data = self.detail("episode")
        data["node"]["source"]["label"] = "changed"
        data["node"]["topics"][0]["label"] = "changed"
        data["original"]["summary"] = "changed"
        self.assertNotIn("changed", json.dumps(self.m, ensure_ascii=False))
        self.assertEqual(self.p.sessions.group["demo"]["name"], "示例群")

    def test_04_clock_does_not_invalidate_cursor(self):
        first = self.call(params={"limit": "2"})
        next_ = self.call(params={"limit": "2", "cursor": first["data"]["page"]["nextCursor"]}, now=NOW+60000)
        self.assertTrue(next_["ok"], next_)
        self.assertEqual(next_["data"]["page"]["matched"], 6)
        self.assertTrue({n["id"] for n in first["data"]["items"]}.isdisjoint(n["id"] for n in next_["data"]["items"]))

    def test_05_modified_record_invalidates_cursor(self):
        first = self.call(params={"limit": 2})
        self.m["facts"][0]["content"] = "更新了内容"
        r = self.call(params={"limit": 2, "cursor": first["data"]["page"]["nextCursor"]})
        self.assertEqual(r["error"]["code"], "STALE_CURSOR")

    def test_06_changed_filters_reject_cursor(self):
        first = self.call(params={"limit": 2, "from": NOW-7*DAY})
        r = self.call(params={"limit": 2, "from": NOW-7*DAY+1000, "cursor": first["data"]["page"]["nextCursor"]})
        self.assertEqual(r["error"]["code"], "INVALID_QUERY")

    def test_07_deleted_item_is_not_found(self):
        node = self.items(kinds="episode")[0]
        self.m["episodes"].clear()
        r = self.call("item", {"id": node["id"]})
        self.assertEqual(r["error"]["code"], "ITEM_NOT_FOUND")

    def test_08_cross_scope_ids_cannot_collide(self):
        self.p.memories["p:demo"] = FakeMemory(copy.deepcopy(self.m))
        self.p.sessions.private["demo"] = {"name": "示例私聊", "enabled": False, "memory": False}
        rows = self.items()
        self.assertEqual(len({n["id"] for n in rows}), len(rows))
        scopes = self.call("meta")["data"]["sources"]
        self.assertEqual(len(scopes), 2)
        self.assertFalse(next(s for s in scopes if s["kind"] == "private")["enabled"])

    def test_09_ids_survive_name_change(self):
        nodes = {n["kind"]: n["id"] for n in self.items()}
        self.p.sessions.group["demo"]["name"] = "新名字"
        self.assertEqual(nodes, {n["kind"]: n["id"] for n in self.items()})

    def test_10_same_account_across_sources_same_person(self):
        a = self.items(kinds="fact")[0]["people"][0]
        people = self.items(kinds="pack")[0]["people"]
        self.assertEqual(a["id"], people[0]["id"])
        self.assertNotEqual(people[0]["id"], people[1]["id"])
        self.assertNotEqual(a["basis"], people[0]["basis"])

    def test_11_missing_subject_never_inferred(self):
        del self.m["facts"][0]["subject"]
        self.assertEqual(self.items(kinds="fact")[0]["people"], [])

    def test_12_topic_normalization_and_filter(self):
        self.m["episodes"][0]["topics"] = [" 夜空 ", "夜空", "ＡＢＣ"]
        self.packs[0]["topics"].append("abc")
        episode_node = self.items(kinds="episode")[0]
        self.assertEqual(len(episode_node["topics"]), 2)
        topic = episode_node["topics"][1]["id"]
        self.assertEqual({n["kind"] for n in self.items(topicId=topic)}, {"episode", "pack"})

    def test_13_person_filter_is_explicit(self):
        person = self.items(kinds="fact")[0]["people"][0]["id"]
        self.assertEqual({n["kind"] for n in self.items(personId=person)}, {"fact", "pack"})

    def test_14_low_weight_expires_at_thirty_days(self):
        self.m["episodes"][0].update(end=NOW-30*DAY, lastRecalledAt=NOW-60000, recalls=10)
        self.assertEqual(self.items(kinds="episode"), [])
        self.assertEqual(len(self.m["episodes"]), 1)
        self.assertEqual(len(self.items(kinds="word,fact")), 2)

    def test_15_low_weight_before_boundary_remains(self):
        self.m["episodes"][0].update(start=NOW-31*DAY, end=NOW-30*DAY+1)
        self.assertEqual(self.items(kinds="episode")[0]["expiresAt"], NOW+1)

    def test_16_high_weight_not_hard_expired(self):
        self.m["episodes"][0].update(start=NOW-90*DAY, end=NOW-90*DAY+1, weight=5)
        self.assertIsNone(self.items(kinds="episode")[0]["expiresAt"])

    def test_17_tainted_haze_hidden_without_mutating(self):
        self.m["episodes"][0].update(hazeId=5, level=3, start=NOW-31*DAY, end=NOW-30*DAY)
        self.m["episodes"].append(episode(9, weight=5, level=3, hazeId=5))
        self.assertEqual(self.items(kinds="haze"), [])
        d = self.detail("episode")
        self.assertNotIn("那阵子", d["rememberedText"])
        self.assertEqual(d["links"], [])
        self.assertEqual(len(self.m["haze"]), 1)

    def test_18_rough_keeps_summary(self):
        self.m["episodes"][0]["level"] = 1
        d = self.detail("episode")
        self.assertIn("完整摘要", d["rememberedText"])
        self.assertIn("完整摘要", d["node"]["preview"])

    def test_19_fuzzy_hides_original_from_list_search(self):
        self.m["episodes"][0]["level"] = 2
        n = self.items(kinds="episode")[0]
        self.assertNotIn("秘密细节", json.dumps(n, ensure_ascii=False))
        self.assertEqual(self.items(q="秘密细节"), [])
        self.assertIn("秘密细节", self.detail("episode")["original"]["summary"])
        self.assertIn("放松", n["preview"])

    def test_20_very_fuzzy_haze_and_links(self):
        self.m["episodes"][0].update(level=3, hazeId=5)
        d = self.detail("episode")
        self.assertIn("那阵子常聊", d["rememberedText"])
        self.assertEqual(d["links"][0]["type"], "faded_into")
        self.assertEqual(d["links"][0]["target"]["kind"], "haze")

    def test_21_current_text_uses_real_formatter(self):
        for level in range(4):
            self.m["episodes"][0]["level"] = level
            self.assertEqual(self.detail("episode")["rememberedText"], M._episode_line(self.m["episodes"][0], NOW))

    def test_22_word_link_only_when_target_exists(self):
        self.assertEqual(self.detail("word")["links"][0]["type"], "word_of_episode")
        self.m["words"][0]["episode"] = 9876
        self.assertEqual(self.detail("word")["links"], [])

    def test_23_only_done_nonempty_group_packs(self):
        for i, state in enumerate(("queued", "skipped", "retired")):
            self.packs.append({**self.packs[0], "id": str(i), "state": state})
        self.packs.append({**self.packs[0], "id": "empty", "summary": ""})
        self.assertEqual(len(self.items(kinds="pack")), 1)
        self.packs[0]["cold"] = True
        self.assertEqual(self.items(kinds="pack")[0]["status"], "cold")

    def test_24_retired_facts_and_closed_concerns_hidden(self):
        self.m["facts"][0]["retiredAt"] = NOW-1
        self.m["threads"][0]["closedAt"] = NOW-1
        self.assertEqual(self.items(kinds="fact,thread"), [])

    def test_25_thread_seven_day_max_and_boundary(self):
        self.m["threads"][0].update(createdAt=NOW-7*DAY, expiresAt=NOW+99*DAY)
        self.assertEqual(self.items(kinds="thread"), [])
        self.m["threads"][0]["createdAt"] += 1
        self.assertEqual(self.items(kinds="thread")[0]["expiresAt"], NOW+1)

    def test_26_threads_max_six(self):
        self.m["threads"] = [dict(id=i, content=str(i), createdAt=NOW-1) for i in range(15)]
        self.assertEqual(len(self.items(kinds="thread")), 6)

    def test_27_unknown_time_does_not_become_today(self):
        self.m["episodes"][0].pop("start")
        n = self.items(kinds="episode")[0]
        self.assertEqual(n["time"]["basis"], "unknown")
        self.assertIsNone(n["time"]["start"])
        self.assertEqual(self.items(kinds="episode", **{"from": NOW-7*DAY}), [])
        self.assertEqual(len(self.items(kinds="episode", **{"from": NOW-7*DAY, "includeUndated": "true"})), 1)

    def test_28_month_only_is_explicit_precision(self):
        n = self.items(kinds="haze")[0]
        self.assertEqual(n["time"]["precision"], "month")
        self.assertEqual(n["time"]["label"], "印象月份")
        self.assertEqual(n["time"]["end"]-n["time"]["start"], 31*DAY)
        self.assertEqual(self.items(kinds="haze", **{"from": n["time"]["end"]}), [])

    def test_29_malformed_month_is_unknown(self):
        self.m["haze"][0]["month"] = "2026-99"
        self.assertEqual(self.items(kinds="haze")[0]["time"]["basis"], "unknown")

    def test_30_time_filter_inclusive_from_exclusive_to(self):
        stamp = self.m["words"][0]["ts"]
        self.assertEqual(len(self.items(kinds="word", **{"from": stamp, "to": stamp+1})), 1)
        self.assertEqual(self.items(kinds="word", **{"to": stamp}), [])
        self.assertEqual(self.items(kinds="word", **{"from": stamp+1}), [])

    def test_31_overlap_query_uses_whole_conversation(self):
        e = self.m["episodes"][0]
        self.assertEqual(len(self.items(kinds="episode", **{"from": e["start"]+1, "to": e["end"]})), 1)

    def test_32_limit_validation(self):
        for limit in (0, 201, -1, "1.5", True, [], {}):
            self.assertEqual(self.call(params={"limit": limit})["error"]["code"], "INVALID_QUERY")

    def test_33_parameter_validation(self):
        for params in ({"scope": "../../secret"}, {"kinds": "episode,UNKNOWN"}, {"q": "x"*81}, {"includeUndated": "yes"},
                       {"from": "nan"}, {"from": NOW, "to": NOW-1}, {"from": []}, {"id": "ignored"}):
            self.assertEqual(self.call(params=params)["error"]["code"], "INVALID_QUERY", params)

    def test_34_bad_cursor_never_crashes(self):
        for cursor in ("%%%", "e30", "bm9uZQ", "W10", "x"*1025):
            self.assertEqual(self.call(params={"cursor": cursor})["error"]["code"], "INVALID_QUERY")

    def test_35_empty_is_success_not_not_ready(self):
        self.p.memories.clear()
        r = self.call()
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"], {"items": [], "page": {"matched": 0, "returned": 0, "nextCursor": None}})

    def test_36_unready_and_stopped_are_explicit(self):
        for p in (NS(), NS(memories={}), NS(memories={}, sessions=NS(), _stopped=True)):
            self.assertEqual(U.handle(p, "meta", now=NOW)["error"]["code"], "NOT_READY")

    def test_37_no_vectors_paths_or_raw_messages_exported(self):
        text = json.dumps([self.call(), self.detail("pack")], ensure_ascii=False)
        for hidden in ("999999", "DO_NOT_EXPORT_PATH", '"ids"', '"vector"', '"fromMsg"'):
            self.assertNotIn(hidden, text)

    def test_38_plain_text_is_not_interpreted(self):
        malicious = '<img src=x onerror="alert(1)">'
        self.m["facts"][0]["content"] = malicious
        self.assertEqual(self.detail("fact")["rememberedText"], malicious)
        self.assertEqual(len(self.items(q="onerror")), 1)

    def test_39_null_fields_and_legacy_records(self):
        self.m["episodes"] = [{"id": 1}]
        self.m["facts"] = [{"id": 2, "content": "旧事实"}]
        d = self.detail("episode")
        self.assertIsNone(d["recall"]["lastCountedAt"])
        self.assertEqual(d["node"]["title"], "未命名记忆")
        self.assertEqual(self.items(kinds="fact")[0]["people"], [])

    def test_40_invalid_numbers_are_not_json_nan(self):
        self.m["episodes"][0].update(level=float("nan"), weight=float("inf"), start=float("nan"), recalls=float("nan"))
        encoded = json.dumps(self.call(), allow_nan=False)
        self.assertNotIn("NaN", encoded)

    def test_41_duplicate_storage_ids_not_duplicate_nodes(self):
        self.m["episodes"].append(episode())
        rows = self.items(kinds="episode")
        self.assertEqual(len(rows), 1)

    def test_42_metadata_does_not_know_pending_is_zero(self):
        source = self.call("meta")["data"]["sources"][0]
        self.assertIsNone(source["pendingCount"])
        self.assertTrue(source["enabled"])

    def test_43_namespace_distinguishes_platform_accounts(self):
        first = self.items(kinds="fact")[0]["people"][0]["id"]
        self.p.universe_account_namespace = "another-platform"
        self.assertNotEqual(first, self.items(kinds="fact")[0]["people"][0]["id"])

    def test_44_paginate_all_without_duplicates(self):
        self.m["episodes"] = [episode(i+10, start=NOW-i*1000, end=NOW-i*1000+1) for i in range(210)]
        ids, cursor = [], None
        while True:
            params = {"limit": 37}
            if cursor:
                params["cursor"] = cursor
            result = self.call(params=params)
            self.assertTrue(result["ok"], result)
            data = result["data"]
            ids.extend(n["id"] for n in data["items"])
            cursor = data["page"]["nextCursor"]
            if not cursor:
                break
        self.assertEqual(len(ids), 215)
        self.assertEqual(len(ids), len(set(ids)))

    def test_45_large_legacy_integer_is_not_float_overflow(self):
        self.m["episodes"][0].update(start=10**500, end=10**500, level=10**500, weight=10**500)
        node = self.items(kinds="episode")[0]
        self.assertEqual(node["time"]["basis"], "unknown")
        self.assertEqual(node["importance"], 5)

    def test_46_expiry_invalidates_cursor_without_mutating(self):
        self.m["episodes"][0].update(start=NOW-30*DAY, end=NOW-30*DAY+1000)
        first = self.call(params={"limit": 2})
        r = self.call(params={"limit": 2, "cursor": first["data"]["page"]["nextCursor"]}, now=NOW+1000)
        self.assertEqual(r["error"]["code"], "STALE_CURSOR")
        self.assertEqual(len(self.m["episodes"]), 1)

    def test_47_preserved_word_loses_only_expired_event_link(self):
        self.m["episodes"][0].update(start=NOW-31*DAY, end=NOW-30*DAY)
        word = self.detail("word")
        self.assertEqual(word["links"], [])
        self.assertEqual(word["rememberedText"], self.m["words"][0]["text"])

    def test_48_empty_search_and_scope_keep_counts_exact(self):
        r = self.call(params={"q": "不存在的完全不同内容", "scope": "g:demo"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["page"], {"matched": 0, "returned": 0, "nextCursor": None})


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(UniverseTests))
    passed = result.testsRun - len(result.failures) - len(result.errors)
    print(f"universe {passed}/{result.testsRun}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
