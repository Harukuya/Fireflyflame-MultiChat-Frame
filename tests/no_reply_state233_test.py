"""不回复分类的持久化、共享及整理材料回归；三份源码通用，全程假数据。

用法：python -I -B no_reply_state233_test.py <源码根目录> <复刻包目录> <临时目录>
独立框架用法：python -I -B no_reply_state233_test.py <源码根目录> [临时目录]
源码根目录下可为 firefly（个人/发布版）或 multichat（框架版）。
不加载插件入口、不读 AstrBot 配置、不调用真实模型。
"""

import asyncio
import copy
import importlib
import json
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest


if not 2 <= len(sys.argv) <= 4:
    raise SystemExit("usage: no_reply_state233_test.py <source-root> [<replica-root>] [<temp-root>]")
plugin = sys.argv[1]
output = sys.argv[-1] if len(sys.argv) >= 3 else str(Path(tempfile.gettempdir()) / "multichat-no-reply-state")
sys.path.insert(0, plugin)
package = "multichat" if (Path(plugin) / "multichat").is_dir() else "firefly"
H = importlib.import_module(package + ".history")
M = importlib.import_module(package + ".memory")
R = importlib.import_module(package + ".rounds")
W = importlib.import_module(package + ".winext")
Path(output).mkdir(parents=True, exist_ok=True)
STAMP = 1793678400000


def message(key, kind=None, *, declined=True, text=None, role="user"):
    row = {"id": key, "role": role, "text": text or ("测试原话" + key),
           "ts": STAMP + int(key[1:]) * 1000, "uid": "90001", "name": "测试账号", "read": True}
    if declined:
        row["declined"] = True
    if kind is not None:
        row["noReplyKind"] = kind
    return row


class Store:
    def __init__(self, root):
        self.root = str(root)
        self.memory = M.empty_memory()
        self.memory_vec = {}
        self.saved = 0

    def save_memory(self):
        self.saved += 1


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="no-reply-state-", dir=output)
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.history = H.History(self.store)
        self.rounds = R.RoundMemory(self.store, self.history)
        self.rows = [message("m1", "unnecessary"), message("m2", "unwilling"),
                     message("m3"), message("m4", "unnecessary", declined=False)]
        self.history.sync(self.rows)

    def indexed(self, rows):
        return {row["id"]: row for row in rows}

    def test_history_kind_survives_restart_as_string(self):
        rows = self.indexed(H.History(self.store).messages)
        self.assertEqual(rows["m1"]["noReplyKind"], "unnecessary")
        self.assertEqual(rows["m2"]["noReplyKind"], "unwilling")
        self.assertIsInstance(rows["m1"]["noReplyKind"], str)

    def test_legacy_declined_survives_restart_without_reinterpretation(self):
        row = self.indexed(H.History(self.store).messages)["m3"]
        self.assertTrue(row["declined"])
        self.assertNotIn("noReplyKind", row)
        self.assertEqual(W.no_reply_mark(row), "（你没回）")

    def test_only_metadata_contains_kind_and_original_is_unchanged(self):
        saved = json.loads(Path(self.history.path).read_text(encoding="utf-8"))
        self.assertEqual(saved["meta"]["m1"]["noReplyKind"], "unnecessary")
        originals = self.indexed(saved["messages"])
        self.assertEqual(originals["m1"]["text"], self.rows[0]["text"])
        for row in saved["messages"]:
            self.assertNotIn("noReplyKind", row)
            self.assertNotIn("declined", row)

    def test_invalid_or_orphan_kind_is_not_saved(self):
        self.history.sync([message("m5", True), message("m6", "other")])
        rows = self.indexed(H.History(self.store).messages)
        for key in ("m4", "m5", "m6"):
            self.assertNotIn("noReplyKind", rows[key])

    def test_invalid_or_orphan_disk_kind_is_not_loaded(self):
        path = Path(self.history.path)
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["meta"]["m1"]["noReplyKind"] = True
        saved["meta"]["m2"]["noReplyKind"] = "unknown"
        saved["meta"]["m4"] = {"noReplyKind": "unnecessary"}
        path.write_text(json.dumps(saved), encoding="utf-8")
        rows = self.indexed(H.History(self.store).messages)
        for key in ("m1", "m2", "m4"):
            self.assertNotIn("noReplyKind", rows[key])

    def test_plain_source_resync_keeps_existing_status(self):
        self.history.sync([H._clean_message(self.rows[0])])
        row = self.indexed(H.History(self.store).messages)["m1"]
        self.assertTrue(row["declined"])
        self.assertEqual(row["noReplyKind"], "unnecessary")

    def test_shared_snapshot_uses_later_lifecycle_metadata(self):
        row = message("m5", declined=False, text="这一轮已经结束")
        self.history.sync([row])
        self.rounds.remember_shared([row])
        later = {**row, "text": "后来存档修订的文字", "declined": True, "noReplyKind": "unnecessary"}
        self.history.sync([later])
        shared = self.rounds.recent_snapshot({"m5"})["rows"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["noReplyKind"], "unnecessary")
        self.assertTrue(shared[0]["declined"])
        self.assertEqual(shared[0]["text"], row["text"])

    def test_shared_snapshot_keeps_kind_after_restart(self):
        self.rounds.remember_shared(self.rows)
        reopened = R.RoundMemory(self.store, H.History(self.store))
        snapshot = self.indexed(reopened.recent_snapshot({row["id"] for row in self.rows})["rows"])
        self.assertEqual(snapshot["m1"]["noReplyKind"], "unnecessary")
        self.assertEqual(snapshot["m2"]["noReplyKind"], "unwilling")
        self.assertTrue(snapshot["m3"]["declined"])
        self.assertNotIn("noReplyKind", snapshot["m3"])
        self.assertNotIn("noReplyKind", snapshot["m4"])

    def test_unread_or_skipped_message_cannot_enter_archive(self):
        unread = {**message("m5", "unnecessary"), "read": False}
        skipped = {**message("m6", "unnecessary"), "skipped": True}
        self.history.sync([unread, skipped])
        self.rounds.remember_shared([unread, skipped])
        self.assertFalse(self.rounds.recent_snapshot({"m5", "m6"})["rows"])
        self.assertNotIn("m5", self.indexed(self.history.messages))
        self.assertNotIn("m6", self.indexed(self.history.messages))

    def test_mark_labels_and_no_label_without_declined(self):
        self.assertEqual(W.no_reply_mark(self.rows[0]), "（这轮无需回复）")
        self.assertEqual(W.no_reply_mark(self.rows[1]), "（你没回）")
        self.assertEqual(W.no_reply_mark(self.rows[2]), "（你没回）")
        self.assertEqual(W.no_reply_mark(self.rows[3]), "")

    def note(self, rows):
        record = R.RoundMemory._record(rows[:3], "test-pack", None, "")
        record.update(state="done", summary="我记得大家已经把安排确认好了。")
        self.rounds._records = [record]
        return self.rounds.memory_note(rows)

    def test_pack_index_separates_unnecessary_unwilling_and_legacy(self):
        before = copy.deepcopy(self.rows)
        note = self.note(self.rows)
        self.assertIn("#1（这轮无需回复）", note)
        self.assertIn("#2、#3（你没回）", note)
        self.assertNotIn("#1、#2、#3（你没回）", note)
        self.assertEqual(self.rows, before)

    def test_unpackaged_consolidation_line_has_correct_label(self):
        rows = self.rows[:3] + [message("m4", "unnecessary", text="好，这件事已经完成")]
        note = self.note(rows)
        line = next(line for line in note.splitlines() if line.startswith("#4 "))
        self.assertTrue(line.endswith("好，这件事已经完成（这轮无需回复）"))

    def test_regular_raw_quote_does_not_expose_runtime_marker(self):
        for row in self.history.messages:
            line = H._line(row)
            self.assertNotIn("无需回复", line)
            self.assertNotIn("你没回", line)
            self.assertNotIn("noReplyKind", line)

    def capture_consolidation(self, peer=False, summary=None):
        requests = []

        async def fake_chat(system, messages, **kwargs):
            requests.append((system, messages, kwargs))
            return types.SimpleNamespace(ok=False, calls=[], kind="synthetic", detail="")

        logger = logging.getLogger("no-reply-state-test")
        logger.disabled = True
        memory = M.Memory(self.store, None, logger)
        rows = self.rows[:3] + [message("m4", declined=False, text="我知道啦", role="her")]
        before = copy.deepcopy(rows)
        if peer:
            asyncio.run(memory.consolidate_peer(fake_chat, rows, STAMP + 60000, "测试群", "90002",
                                                summary_dialogue=summary))
        else:
            asyncio.run(memory.consolidate(fake_chat, rows, STAMP + 60000))
        self.assertEqual(rows, before)
        self.assertEqual(len(requests), 1)
        return requests[0][1][0]["content"]

    def assert_dialogue_marks(self, text):
        lines = {line.split(" ", 1)[0]: line for line in text.splitlines() if line.startswith("#")}
        self.assertTrue(lines["#1"].endswith("测试原话m1（这轮无需回复）"))
        self.assertTrue(lines["#2"].endswith("测试原话m2（你没回）"))
        self.assertTrue(lines["#3"].endswith("测试原话m3（你没回）"))
        self.assertTrue(lines["#4"].endswith("我知道啦"))

    def test_private_consolidation_marks_without_extra_request(self):
        self.assert_dialogue_marks(self.capture_consolidation())

    def test_peer_group_consolidation_marks_without_extra_request(self):
        self.assert_dialogue_marks(self.capture_consolidation(peer=True))

    def test_packed_consolidation_preserves_index_meaning(self):
        note = self.note(self.rows)
        text = self.capture_consolidation(peer=True, summary=note)
        self.assertIn(note, text)
        self.assertIn("#1（这轮无需回复）", text)
        self.assertIn("#2、#3（你没回）", text)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(StateTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len({test.id() for test, _ in result.failures + result.errors})
    print(f"no_reply_state233 {result.testsRun - failed}/{result.testsRun}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
