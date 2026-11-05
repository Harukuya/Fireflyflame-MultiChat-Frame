"""Daily busy intervals stay within one local clock day; no real hosts or models."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import subprocess
import sys
import unittest

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from multichat import activity, profile, schedule

MIN = 60_000


def stamp(day=5, hour=0, minute=0, second=0):
    return int(datetime(2026, 11, day, hour, minute, second).timestamp() * 1000)


class DailyBusyTests(unittest.TestCase):
    def setUp(self):
        profile.configure({"schedule_mode": "always"}, ROOT / "prompts")
        self.addCleanup(profile.configure, {}, ROOT / "prompts")

    def configure_busy(self, start, end, **extra):
        cfg = {"schedule_mode": "always", "busy_start": start, "busy_end": end, **extra}
        profile.configure(cfg, ROOT / "prompts")
        return cfg

    def test_longest_busy_period_keeps_a_daily_break(self):
        self.configure_busy("00:01", "00:00")
        rows = [schedule.state_at(t).activity == "mission"
                for t in range(stamp(4), stamp(7), MIN)]
        runs, length = [], 0
        for busy in rows + [False]:
            if busy:
                length += 1
            elif length:
                runs.append(length)
                length = 0
        self.assertEqual(runs, [1439, 1439, 1439])
        self.assertTrue(schedule.state_at(stamp(5)).online)
        self.assertFalse(schedule.state_at(stamp(5, 0, 1)).online)

    def test_shortest_interval_start_and_end(self):
        self.configure_busy("12:00", "12:01")
        self.assertTrue(schedule.state_at(stamp(5, 11, 59, 59)).online)
        self.assertEqual(schedule.state_at(stamp(5, 12)).activity, "mission")
        self.assertFalse(schedule.state_at(stamp(5, 12, 0, 59)).online)
        self.assertTrue(schedule.state_at(stamp(5, 12, 1)).online)

    def test_midnight_does_not_restart_cross_day_busy(self):
        self.configure_busy("23:00", "01:00")
        self.assertTrue(schedule.state_at(stamp(4, 22, 59)).online)
        self.assertFalse(schedule.state_at(stamp(4, 23)).online)
        self.assertFalse(schedule.state_at(stamp(5, 0, 59, 59)).online)
        self.assertTrue(schedule.state_at(stamp(5, 1)).online)
        self.assertTrue(schedule.state_at(stamp(5, 22, 59)).online)

    def test_equal_start_and_end_cannot_mean_all_day_busy(self):
        self.configure_busy("23:00", "01:00")
        before = profile.schedule_settings()
        for value in ("00:00", "12:00", "23:59"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.configure_busy(value, value)
                self.assertEqual(profile.schedule_settings(), before)

    def test_incomplete_busy_config_cannot_replace_active_schedule(self):
        self.configure_busy("23:00", "01:00")
        before = profile.schedule_settings()
        with self.assertRaises(ValueError):
            profile.configure({"busy_start": "10:00"}, ROOT / "prompts")
        self.assertEqual(profile.schedule_settings(), before)

    def test_repeated_queries_do_not_extend_busy(self):
        self.configure_busy("23:00", "01:00")
        initial = schedule.state_at(stamp(5, 0, 30))
        for point in (stamp(5, 0, 45), stamp(4, 23), stamp(6, 0, 15), stamp(5, 0, 30)):
            schedule.state_at(point)
        self.assertEqual(schedule.state_at(stamp(5, 0, 30)), initial)
        self.assertTrue(schedule.state_at(stamp(5, 1)).online)

    def test_reloading_same_config_does_not_restart_busy(self):
        cfg = self.configure_busy("23:00", "01:00")
        points = [stamp(4, 23), stamp(5, 0, 59), stamp(5, 1), stamp(5, 1, 1)]
        before = [schedule.state_at(t) for t in points]
        profile.configure({}, ROOT / "prompts")
        profile.configure(cfg, ROOT / "prompts")
        self.assertEqual([schedule.state_at(t) for t in points], before)

    def test_new_process_uses_original_clock_boundary(self):
        self.configure_busy("23:00", "01:00")
        code = """from pathlib import Path
from datetime import datetime
import json,sys
root=Path(sys.argv[1]);sys.path.insert(0,str(root))
from multichat import profile,schedule
profile.configure({'schedule_mode':'always','busy_start':'23:00','busy_end':'01:00'},root/'prompts')
points=[datetime(2026,11,5,0,59),datetime(2026,11,5,1,0)]
print(json.dumps([(schedule.state_at(int(t.timestamp()*1000)).online,schedule.state_at(int(t.timestamp()*1000)).location) for t in points]))
"""
        raw = subprocess.check_output([sys.executable, "-I", "-B", "-c", code, str(ROOT)])
        self.assertEqual(json.loads(raw), [[False, "mission"], [True, "base"]])

    def test_online_boundaries_follow_longest_busy_interval(self):
        self.configure_busy("00:01", "00:00")
        now = stamp(5, 0, 0, 30)
        self.assertEqual(schedule.online_since(now), stamp(5))
        self.assertEqual(schedule.offline_after(now), stamp(5, 0, 1))

    def test_busy_end_uses_regular_rest_when_outside_online_hours(self):
        self.configure_busy("23:00", "01:00", schedule_mode="daily",
                            online_start="08:00", online_end="22:00")
        state = schedule.state_at(stamp(5, 1))
        self.assertEqual((state.location, state.activity, state.online), ("base", "sleep", False))
        self.assertTrue(schedule.state_at(stamp(5, 8)).online)

    def test_today_and_yesterday_show_cross_midnight_parts(self):
        self.configure_busy("23:00", "01:00")
        today = activity.current_context(stamp(5, 0, 30))
        yesterday = activity.query(stamp(5, 0, 30), "yesterday", "23:30")
        self.assertIn("今天 00:00–01:00 — 忙碌时间", today)
        self.assertIn("昨天 23:00–24:00 — 忙碌时间", yesterday)
        self.assertNotIn("忙碌时间", activity.current_context(stamp(5, 1)))

    def test_busy_config_isolated_between_profiles(self):
        a = profile.prepare({"schedule_mode": "always", "busy_start": "00:01", "busy_end": "00:00"}, ROOT / "prompts")
        b = profile.prepare({"schedule_mode": "always"}, ROOT / "prompts")
        with profile.use(a):
            self.assertEqual(schedule.state_at(stamp(5, 12)).activity, "mission")
            with profile.use(b):
                self.assertTrue(schedule.state_at(stamp(5, 12)).online)
            self.assertEqual(schedule.state_at(stamp(5, 12)).activity, "mission")
        self.assertTrue(schedule.state_at(stamp(5, 12)).online)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
