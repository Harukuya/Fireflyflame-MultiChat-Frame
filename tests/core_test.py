"""Offline checks for extracted core boundaries, identity and shared state."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from multichat import profile
from multichat.energy import fresh_energy
from multichat.group import GroupChat
from multichat.history import History
from multichat.judge import judge
from multichat.llm import ChatResult, Llm
from multichat.memory import Memory
from multichat.recent import RecentPool
from multichat.rounds import RoundMemory
from multichat.sanitize import to_messages
from multichat.store import Her, Store

NOW = 1_761_998_400_000
BOT = "星铃+"


class QuietLog:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class NoModel:
    async def embed(self, texts):
        raise AssertionError("This core check must not use an embedding model")


class FakeEvents:
    def __init__(self):
        self.notes, self.counts = [], []

    def note(self, *args, **kwargs):
        self.notes.append(args)

    def count(self, *args):
        self.counts.append(args)


def message(mid, *, qid="q-images", role="user", text="河边有新叶", read=False, answered=False, ts=NOW):
    row = {"id": mid, "qid": qid, "role": role, "text": text, "ts": ts,
           "uid": "synthetic-user", "name": "测试访客", "read": read}
    if answered:
        row["answered"] = True
    if role == "user":
        row["attachment"] = {"kind": "image", "file": mid + ".png", "caption": "一片新叶"}
    return row


class CoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="multichat-core-")
        self.work = Path(self.temporary.name).resolve()
        self.assertTrue(self.work.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.snapshot = profile.prepare({"character_name": BOT, "primary_user_name": "测试联系人", "schedule_mode": "always"}, ROOT / "prompts")
        self.profile_context = profile.use(self.snapshot)
        self.profile_context.__enter__()
        self.addCleanup(self.profile_context.__exit__, None, None, None)
        self.log = QuietLog()
        self.her = Her(str(self.work / "shared"))
        self.hub = SimpleNamespace(her=self.her, group_name=lambda gid: "测试群" + gid,
                                   embedder=None, trailblazer="", closed=False,
                                   options=lambda: {}, proactive_on=lambda gid: True,
                                   events=FakeEvents(), llm=Llm(lambda: None, self.log),
                                   round_llm_for=None)

    def tearDown(self):
        # Verify the exact task-owned absolute directory before recursive cleanup.
        self.assertTrue(self.work.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.temporary.cleanup()

    def group(self, gid, rows=()):
        store = Store(str(self.work / "groups" / gid), self.her)
        store.extra["flags"] = 2
        store.messages = list(rows)
        store.save_messages()
        store.save_state()
        memory = Memory(store, NoModel(), self.log)
        return GroupChat(gid, self.hub, store, memory)

    def assert_generic_prompt(self, text):
        for forbidden in ("流" + "萤", "萨" + "姆", "星核" + "猎手", "匹诺" + "康尼", "辉" + "夜", "小" + "伴"):
            self.assertNotIn(forbidden, text)

    def test_custom_name_sanitize_and_literal_regex(self):
        self.assertEqual(to_messages(BOT + "：晚上好。\n" + BOT + ": 看到新叶了。"), ["晚上好。", "看到新叶了。"])
        self.assertEqual(to_messages("星铃：这不是完整配置名。"), ["星铃：这不是完整配置名。"])

    def test_history_actual_unpack_uses_current_name(self):
        store = Store(str(self.work / "history"), self.her)
        archive = History(store, "测试群")
        archive.sync([message("her-1", role="her", text="我也看到了河边的新叶。", read=True)])
        prompt = archive.unpack("河边新叶")
        self.assertIn(BOT + "：", prompt)
        self.assert_generic_prompt(prompt)

    async def test_judge_actual_request_uses_current_name(self):
        captured = []

        async def chat(system, messages, tools, choice):
            captured.append((system, messages))
            return ChatResult(ok=True, calls=[{"id": "j1", "name": "judge", "input": {
                "reply": False, "interest": 0, "reason": "先听着", "focus": []}}])

        rows = [message("her-1", role="her", text="说说你看到的新叶吧。", read=True),
                {**message("u1", text="新叶是浅绿色的。"), "called": True}]
        result = await judge(SimpleNamespace(chat_with_retry=chat), profile.prompt("identity"), "window", rows, 0)
        self.assertTrue(result["read_completed"])
        prompt = captured[0][0] + "\n" + captured[0][1][0]["content"]
        self.assertIn("【" + BOT + "】", prompt)
        self.assertIn("→" + BOT, prompt)
        self.assert_generic_prompt(prompt)

    def test_recent_pool_actual_context_uses_current_name(self):
        sources = [{"scope": "g:source", "label": "来源群", "packs": [],
                    "rows": [message("her-1", role="her", text="我看到了河边的新叶。", read=True)]}]
        prompt = RecentPool(lambda: sources).context("g:target", "河边的新叶")
        self.assertIn(BOT, prompt)
        self.assertIn("别处近期内容", prompt)
        self.assert_generic_prompt(prompt)

    async def test_round_summary_actual_request_uses_current_name(self):
        store = Store(str(self.work / "rounds"), self.her)
        archive = History(store, "测试群")
        rounds = RoundMemory(store, archive, None)
        rounds.enqueue([message("her-1", role="her", text="河边的新叶让我想起散步。", read=True)])
        captured = []

        async def chat(system, messages):
            captured.append((system, messages))
            return ChatResult(ok=True, text=json.dumps({"remember": True, "summary": "我聊起河边的新叶。",
                                                       "topics": ["散步"], "anchors": ["新叶"], "unresolved": []}, ensure_ascii=False))

        self.assertTrue(await rounds.summarize(chat))
        self.assertEqual(len(captured), 1)
        prompt = captured[0][0] + "\n" + captured[0][1][0]["content"]
        self.assertIn(BOT, prompt)
        self.assert_generic_prompt(prompt)
        self.assertEqual(rounds.stats()["pending"], 0)
        self.assertEqual(rounds.stats()["packs"], 1)

    async def test_two_groups_overlap_and_share_real_energy_settlement(self):
        # These events guarantee A is inside its assessment before B starts.
        with patch("multichat.group.now_ms", return_value=NOW):
            a = self.group("alpha", [message("a1")])
            b = self.group("beta", [message("b1")])
            self.her.energy = {**fresh_energy(NOW), "value": 60}
            self.her.save()
            entered_a, entered_b, release_a, release_b = (asyncio.Event() for _ in range(4))

            def assessor(entered, release):
                async def assess(pool, start, mode, selected):
                    entered.set()
                    await release.wait()
                    return {"read": pool[start:], "reply": False, "reason": "先听着", "remember": True}
                return assess

            a.assess = assessor(entered_a, release_a)
            b.assess = assessor(entered_b, release_b)
            task_a = asyncio.create_task(a.look())
            await asyncio.wait_for(entered_a.wait(), 1)
            self.assertEqual(self.her.energy["value"], 59)
            task_b = asyncio.create_task(b.look())
            await asyncio.wait_for(entered_b.wait(), 1)
            self.assertFalse(task_a.done())
            self.assertEqual(self.her.energy["value"], 58)
            release_b.set()
            await asyncio.wait_for(task_b, 1)
            release_a.set()
            await asyncio.wait_for(task_a, 1)
        self.assertIs(a.store.her, b.store.her)
        self.assertEqual(a.store.energy["value"], 58)
        self.assertEqual(b.store.energy["value"], 58)
        self.assertEqual([m["id"] for m in a.store.messages], ["a1"])
        self.assertEqual([m["id"] for m in b.store.messages], ["b1"])
        self.assertEqual(a.store.extra["look"]["count"], 1)
        self.assertEqual(b.store.extra["look"]["count"], 1)
        self.assertEqual(Her(str(self.work / "shared")).energy["value"], 58)
        reopened = Store(a.store.root, self.her)
        self.assertEqual([m["id"] for m in reopened.messages], ["a1"])

    async def test_one_platform_id_recalls_all_unread_images(self):
        group = self.group("all", [message("i1"), message("i2"), message("i3"), message("other", qid="other-qid")])
        await group.on_recall("q-images")
        self.assertEqual([m["id"] for m in group.msgs], ["other"])
        self.assertEqual(group._recalled_ids, {"i1", "i2", "i3"})
        self.assertEqual(group.recalls, 1)
        self.assertEqual(group.history_archive.messages, [])

    async def test_answered_image_retained_unanswered_sibling_removed(self):
        group = self.group("mixed", [message("answered", read=True, answered=True), message("unanswered")])
        await group.on_recall("q-images")
        self.assertEqual([m["id"] for m in group.msgs], ["answered"])
        self.assertTrue(group.msgs[0]["recalled"])
        self.assertEqual(group._recalled_ids, {"unanswered"})
        archived = group.history_archive.messages
        self.assertEqual([m["id"] for m in archived], ["answered"])
        self.assertTrue(archived[0]["answered"])
        self.assertTrue(archived[0]["recalled"])
        self.assertTrue(Store(group.store.root, self.her).messages[0]["recalled"])

    async def test_archive_only_multimage_recall_marks_every_record(self):
        group = self.group("archived", [message("i1", read=True, answered=True),
                                         message("i2", read=True), message("i3", read=True)])
        self.assertEqual(len(group.history_archive.lookup_qid_all("q-images")), 3)
        group.msgs.clear()
        group.store.save_messages()
        await group.on_recall("q-images")
        self.assertEqual(group._recalled_ids, {"i2", "i3"})
        saved = History(group.store).messages
        self.assertEqual(len(saved), 3)
        self.assertTrue(all(row.get("recalled") for row in saved))
        self.assertEqual([row["id"] for row in saved if row.get("answered")], ["i1"])

    def test_all_core_modules_import_without_platform(self):
        script = '''import builtins, importlib, pkgutil, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name == "astrbot" or name.startswith("astrbot.") or name == "napcat" or name.startswith("napcat."):
        raise AssertionError("Platform import escaped an adapter")
    return original(name, *args, **kwargs)
builtins.__import__ = guard
import multichat
names = sorted(m.name for m in pkgutil.iter_modules(multichat.__path__))
for name in names:
    importlib.import_module("multichat." + name)
print(len(names))
'''
        child = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(ROOT)], capture_output=True)
        self.assertEqual(child.returncode, 0, child.stderr.decode("utf-8", errors="replace"))
        self.assertGreaterEqual(int(child.stdout), 40)

    def test_core_syntax_and_platform_import_boundary(self):
        files = list((ROOT / "multichat").glob("*.py"))
        self.assertGreaterEqual(len(files), 40)
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=path.name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                else:
                    continue
                self.assertFalse(any(name.split(".", 1)[0] in ("astrbot", "napcat") for name in names),
                                 f"Platform import in {path.name}:{node.lineno}")

    def test_framework_tree_contains_no_private_seed_or_encrypted_bundle(self):
        private_default = re.compile(r'"(?:primary_user_id|trailblazer_qq)"\s*:\s*"[0-9]{5,}"')
        credential = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
        checked = 0
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in {"node_modules", "third_party", "vendor", ".git", "__pycache__"} for part in path.relative_to(ROOT).parts):
                continue
            self.assertNotEqual(path.suffix.lower(), ".ffdata", f"Encrypted bundle remains: {path.relative_to(ROOT)}")
            if path.suffix.lower() not in {".py", ".json", ".md", ".yaml", ".yml", ".txt", ".js", ".html", ".css", ".toml"}:
                continue
            text = path.read_text(encoding="utf-8-sig")
            checked += 1
            self.assertFalse(private_default.search(text), f"Numeric personal default remains: {path.relative_to(ROOT)}")
            self.assertFalse(credential.search(text), f"Credential shape found: {path.relative_to(ROOT)}")
        self.assertGreater(checked, 40)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
