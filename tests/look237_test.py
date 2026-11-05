"""Smooth per-group reading waits; fake clock, fake model, task-owned state only."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import math
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from multichat import group as G, look_wait, profile
from multichat.energy import fresh_energy
from multichat.llm import Llm
from multichat.memory import Memory
from multichat.store import Her, Store

MIN = 60_000
HOUR = 60 * MIN
NOW = 1_762_214_400_000  # Whole hour; the synthetic clock never reads live host data.


class QuietLog:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class NoModel:
    async def embed(self, texts):
        raise AssertionError("Phone-wait tests must not request an embedding model")


class FakeEvents:
    def __init__(self):
        self.notes, self.counts = [], []

    def note(self, *args, **kwargs):
        self.notes.append(args)

    def count(self, *args):
        self.counts.append(args)


class SmoothWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multichat-look237-")
        self.work = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.assertTrue(self.work.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.context = profile.use(profile.prepare({"character_name": "星铃", "primary_user_name": "联系人",
                                                   "schedule_mode": "always"}, ROOT / "prompts"))
        self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.clock = patch.object(G, "now_ms", return_value=NOW)
        self.time = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.options = {"look_per_hour": 1.5, "look_block_minutes": 60, "look_block_max": 2}
        self.log = QuietLog()
        self.her = Her(str(self.work / "shared"))
        self.her.energy = {**fresh_energy(NOW), "value": 80}
        self.events = FakeEvents()
        self.hub = SimpleNamespace(her=self.her, group_name=lambda gid: "测试群 " + gid,
                                   embedder=None, trailblazer="", closed=False,
                                   options=lambda: self.options, proactive_on=lambda gid: True,
                                   events=self.events, llm=Llm(lambda: None, self.log), round_llm_for=None)

    def group(self, gid="alpha", *, reopen=False):
        store = Store(str(self.work / "groups" / gid), self.her)
        if not reopen:
            store.extra["flags"] = 2
            store.messages = [{"id": "m-" + gid, "role": "user", "qid": "q-" + gid,
                               "uid": "service:user:visitor", "name": "访客", "text": "今天讨论一个新话题。",
                               "ts": NOW, "read": False}]
            store.save_messages()
            store.save_state()
        chat = G.GroupChat(gid, self.hub, store, Memory(store, NoModel(), self.log))
        self.addCleanup(chat.shutdown)
        return chat

    def seed(self, chat, remaining=1.0, *, at=NOW):
        chat.store.extra["lookWait"] = {"v": 1, "remaining": remaining}
        chat.store.save_state()
        # First eligible tick re-establishes the clock, without spending offline time.
        chat._look_due(at, 80)
        return chat

    def tick(self, chat, when, energy=80):
        self.time.return_value = when
        return chat._look_due(when, energy)

    def left(self, chat):
        return chat.store.extra["lookWait"]["remaining"]

    def test_distribution_keeps_mean_but_reduces_early_clumping(self):
        rng = random.Random(237)
        with patch.object(look_wait.random, "gammavariate", side_effect=rng.gammavariate):
            waits = [look_wait.advance(None, 0, 1.5, False)["remaining"] for _ in range(12000)]
        self.assertTrue(all(math.isfinite(x) and x >= 0 for x in waits))
        self.assertAlmostEqual(sum(waits) / len(waits), 1.0, delta=.035)
        self.assertGreater(sum(x < .125 for x in waits) / len(waits), .02)
        self.assertLess(sum(x < .125 for x in waits) / len(waits), .035)
        self.assertGreater(len({round(x, 2) for x in waits}), 200)
        # Ten minutes of extra wait: Gamma2 9.02%, versus the old exponential 22.12%.
        self.assertAlmostEqual(G.look_chance(10 * MIN, 1.5, False), .09020401043104986)

    def test_wait_is_drawn_once_and_not_a_new_lottery_per_tick(self):
        chat = self.group()
        with patch.object(look_wait.random, "gammavariate", return_value=1.0) as draw:
            self.assertFalse(self.tick(chat, NOW))
            for step in range(1, 6):
                self.assertFalse(self.tick(chat, NOW + step * MIN))
            draw.assert_called_once_with(2.0, .5)
        self.assertAlmostEqual(self.left(chat), .875)

    def test_one_normalized_wait_preserves_nominal_forty_minute_mean(self):
        chat = self.seed(self.group())
        for minute in range(1, 40):
            self.assertFalse(self.tick(chat, NOW + minute * MIN))
        self.assertTrue(self.tick(chat, NOW + 40 * MIN))
        self.assertEqual(self.left(chat), 0)

    def test_tired_consumes_two_thirds_without_redrawing(self):
        chat = self.seed(self.group())
        self.tick(chat, NOW + MIN, 49)
        self.assertAlmostEqual(self.left(chat), 1 - 1 / 60)
        self.tick(chat, NOW + 2 * MIN, 50)
        self.assertAlmostEqual(self.left(chat), 1 - 1 / 60 - 1 / 40)

    def test_rate_change_keeps_remaining_progress(self):
        chat = self.seed(self.group())
        self.tick(chat, NOW + MIN)
        self.options["look_per_hour"] = 3
        with patch.object(look_wait.random, "gammavariate", side_effect=AssertionError("No redraw")):
            self.tick(chat, NOW + 2 * MIN)
        self.assertAlmostEqual(self.left(chat), .925)

    def test_groups_draw_and_advance_independently(self):
        a, b = self.group("alpha"), self.group("beta")
        with patch.object(look_wait.random, "gammavariate", side_effect=[.5, 1.5]):
            self.tick(a, NOW)
            self.tick(b, NOW)
        self.tick(a, NOW + MIN)
        self.assertAlmostEqual(self.left(a), .475)
        self.assertEqual(self.left(b), 1.5)
        self.assertIsNot(a.store.extra["lookWait"], b.store.extra["lookWait"])

    def test_cooldown_does_not_spend_wait_and_crossing_only_counts_tail(self):
        chat = self.seed(self.group())
        chat.store.extra["look"] = {"block": NOW // HOUR, "count": 1, "lastAt": NOW}
        self.assertFalse(self.tick(chat, NOW + 9 * MIN))
        self.assertEqual(self.left(chat), 1)
        self.assertFalse(self.tick(chat, NOW + 10 * MIN + 30_000))
        self.assertAlmostEqual(self.left(chat), .9875)

    def test_hour_boundary_does_not_reset_or_redraw_wait(self):
        start = NOW + 59 * MIN
        chat = self.seed(self.group(), .4, at=start)
        with patch.object(look_wait.random, "gammavariate", side_effect=AssertionError("No hour reset")):
            self.tick(chat, start + MIN)
        self.assertAlmostEqual(self.left(chat), .375)

    def test_full_quota_pauses_and_next_hour_resumes_without_credit(self):
        start = NOW + 58 * MIN
        chat = self.seed(self.group(), .4, at=start)
        chat.store.extra["look"] = {"block": NOW // HOUR, "count": 2, "lastAt": NOW}
        self.assertFalse(self.tick(chat, start + MIN))
        self.assertEqual(self.left(chat), .4)
        self.assertFalse(self.tick(chat, NOW + HOUR))
        self.assertEqual(self.left(chat), .4)
        self.tick(chat, NOW + HOUR + MIN)
        self.assertAlmostEqual(self.left(chat), .375)

    def test_low_energy_pause_has_no_resume_credit(self):
        chat = self.seed(self.group())
        self.assertFalse(self.tick(chat, NOW + MIN, 19))
        self.assertFalse(self.tick(chat, NOW + 10 * MIN, 80))
        self.assertEqual(self.left(chat), 1)
        self.tick(chat, NOW + 11 * MIN, 80)
        self.assertAlmostEqual(self.left(chat), .975)

    def test_zero_rate_pause_preserves_wait_for_reenable(self):
        chat = self.seed(self.group())
        self.options["look_per_hour"] = 0
        self.assertFalse(self.tick(chat, NOW + MIN))
        self.assertEqual(self.left(chat), 1)
        self.options["look_per_hour"] = 1.5
        self.assertFalse(self.tick(chat, NOW + HOUR))
        self.assertEqual(self.left(chat), 1)
        self.tick(chat, NOW + HOUR + MIN)
        self.assertAlmostEqual(self.left(chat), .975)

    async def test_ineligible_schedule_paths_preserve_progress(self):
        # Exercise real eligibility checks; each path must also invalidate elapsed credit.
        for reason in ("off", "busy", "no-unread", "batch", "leaving", "offline", "closed", "hub-closed"):
            with self.subTest(reason=reason):
                chat = self.seed(self.group(reason), .8)
                proactive = lambda gid: reason != "off"
                chat.in_flight = reason == "busy"
                chat.closed = reason == "closed"
                self.hub.closed = reason == "hub-closed"
                if reason == "no-unread":
                    chat.msgs[0]["read"] = True
                if reason == "batch":
                    chat.batch_task = asyncio.get_running_loop().create_future()
                self.time.return_value = NOW + MIN
                with patch.object(chat, "_leaving", return_value={"until": NOW + HOUR} if reason == "leaving" else None), \
                     patch.object(G, "state_at", return_value=SimpleNamespace(online=reason != "offline")):
                    chat.schedule_look(NOW + MIN, proactive)
                self.assertEqual(self.left(chat), .8)
                self.assertFalse(chat._look_active)
                chat.closed = False
                self.hub.closed = False
                chat.in_flight = False
                if chat.batch_task:
                    chat.batch_task.cancel()
                    chat.batch_task = None
                chat.msgs[0]["read"] = False
                self.assertFalse(self.tick(chat, NOW + 20 * MIN))
                self.assertEqual(self.left(chat), .8)

    def test_clock_jump_is_capped_and_backward_jump_earns_nothing(self):
        chat = self.seed(self.group())
        self.tick(chat, NOW + HOUR)
        self.assertAlmostEqual(self.left(chat), .95)
        self.tick(chat, NOW + 59 * MIN)
        self.assertAlmostEqual(self.left(chat), .95)

    def test_restart_restores_progress_but_not_elapsed_downtime(self):
        chat = self.seed(self.group(), .6)
        self.tick(chat, NOW + MIN)
        saved = self.left(chat)
        restored = self.group(reopen=True)
        self.assertAlmostEqual(self.left(restored), saved)
        with patch.object(look_wait.random, "gammavariate", side_effect=AssertionError("No restart redraw")):
            self.assertFalse(self.tick(restored, NOW + 6 * HOUR))
            self.assertAlmostEqual(self.left(restored), saved)
            self.tick(restored, NOW + 6 * HOUR + MIN)
        self.assertAlmostEqual(self.left(restored), saved - .025)

    def test_due_opportunity_survives_a_race_before_reading(self):
        chat = self.seed(self.group(), .01)
        self.assertTrue(self.tick(chat, NOW + MIN))
        before = dict(chat.store.extra["look"])
        chat.in_flight = True
        self.time.return_value = NOW + 2 * MIN
        chat.schedule_look(NOW + 2 * MIN, self.hub.proactive_on)
        self.assertEqual(self.left(chat), 0)
        self.assertEqual(chat.store.extra["look"], before)
        chat.in_flight = False
        with patch.object(look_wait.random, "gammavariate", side_effect=AssertionError("Due wait lost")):
            self.assertTrue(self.tick(chat, NOW + 3 * MIN))

    def test_real_start_commits_count_and_only_then_replaces_wait(self):
        chat = self.seed(self.group(), .01)
        self.assertTrue(self.tick(chat, NOW + MIN))
        chat._note_look(NOW + MIN)
        self.assertNotIn("lookWait", chat.store.extra)
        self.assertEqual(chat.store.extra["look"]["count"], 1)
        self.assertEqual(chat.store.extra["look"]["lastAt"], NOW + MIN)
        self.assertFalse(chat._look_active)
        restored = self.group(reopen=True)
        self.assertNotIn("lookWait", restored.store.extra)
        self.assertEqual(restored.store.extra["look"], chat.store.extra["look"])
        with patch.object(look_wait.random, "gammavariate", return_value=.7) as draw:
            self.assertFalse(self.tick(chat, NOW + 11 * MIN))
            draw.assert_called_once()
        self.assertAlmostEqual(self.left(chat), .7)

    def test_legacy_per_group_counts_are_not_refreshed_by_new_wait(self):
        chat = self.group()
        old = {"block": NOW // HOUR, "count": 2, "lastAt": NOW - 20 * MIN}
        chat.store.extra["look"] = old.copy()
        chat.store.save_state()
        restored = self.group(reopen=True)
        with patch.object(look_wait.random, "gammavariate", side_effect=AssertionError("Quota must win")):
            self.assertFalse(self.tick(restored, NOW))
        self.assertEqual(restored.store.extra["look"], old)

    def test_shared_legacy_quota_is_inherited_without_cross_group_mutation(self):
        self.her.look = {"block": NOW // HOUR, "count": 2, "lastAt": NOW - 15 * MIN}
        a, b = self.group("legacy-a"), self.group("legacy-b")
        self.assertEqual(a.store.extra["look"], self.her.look)
        self.assertEqual(b.store.extra["look"], self.her.look)
        a._note_look(NOW + HOUR)
        self.assertEqual(b.store.extra["look"]["count"], 2)
        self.assertEqual(self.her.look["count"], 2)

    async def test_actual_read_failure_still_costs_one_attempt(self):
        chat = self.seed(self.group(), 0)
        started = asyncio.Event()
        release = asyncio.Event()

        @asynccontextmanager
        async def hold(*args, **kwargs):
            yield

        async def fail_after_start(*args, **kwargs):
            started.set()
            await release.wait()
            raise RuntimeError("synthetic judge failure")

        chat._hold = hold
        chat.assess = fail_after_start
        task = asyncio.create_task(chat.look())
        await asyncio.wait_for(started.wait(), 1)
        self.assertEqual(chat.store.extra["look"]["count"], 1)
        self.assertNotIn("lookWait", chat.store.extra)
        self.assertEqual(self.her.energy["value"], 79)
        release.set()
        with self.assertRaisesRegex(RuntimeError, "synthetic judge failure"):
            await task
        self.assertFalse(self.tick(chat, NOW + MIN))
        self.assertEqual(chat.store.extra["look"]["count"], 1)

    def test_helper_does_not_mutate_shared_input(self):
        original = {"v": 1, "remaining": .5}
        changed = look_wait.advance(original, MIN, 1.5, False)
        self.assertEqual(original, {"v": 1, "remaining": .5})
        self.assertAlmostEqual(changed["remaining"], .475)
        self.assertIsNot(changed, original)

    def test_malformed_persisted_wait_recovers_with_one_fresh_draw(self):
        for bad in (None, "broken", {}, {"v": 99, "remaining": 1},
                    {"v": 1, "remaining": -1}, {"v": 1, "remaining": float("nan")},
                    {"v": 1, "remaining": float("inf")}):
            with self.subTest(bad=bad), patch.object(look_wait.random, "gammavariate", return_value=.75) as draw:
                result = look_wait.advance(bad, 0, 1.5, False)
                self.assertEqual(result, {"v": 1, "remaining": .75})
                draw.assert_called_once()

    def test_finite_cdf_limits_and_disabled_rate(self):
        for rate in (0, -1):
            self.assertEqual(look_wait.chance(HOUR, rate, False), 0)
        self.assertEqual(look_wait.chance(0, 1.5, False), 0)
        self.assertEqual(look_wait.chance(-MIN, 1.5, False), 0)
        self.assertAlmostEqual(look_wait.chance(40 * MIN, 1.5, False), .5939941502901619)
        self.assertAlmostEqual(look_wait.chance(60 * MIN, 1.5, True), .5939941502901619)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
