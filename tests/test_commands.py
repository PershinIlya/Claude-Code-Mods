"""Smoke tests: every command runs on seeded data without a traceback."""

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ctempo import commands, weekly
from ctempo.config import SEVEN_DAY

from .helpers import MON, H, D, TempHome

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "bin", "ctempo")
M = 60.0


class Commands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="ctempo-cmd-")
        cls.env = dict(os.environ, CTEMPO_HOME=cls.home, TZ="UTC", CLAUDE_CONFIG_DIR=os.path.join(cls.home, "claude"))
        cls.env.pop("NO_COLOR", None)
        now = time.time()
        # Two closed 5h windows and one closed week, then the current ones.
        series = []
        for k in range(12):
            t = now - 8 * 86400 + k * 3600
            series.append((t, {"five_hour": {"used_percentage": 10 + k, "resets_at": int(now - 8 * 86400 + 5 * 3600)},
                               "seven_day": {"used_percentage": 50 + k, "resets_at": int(now - 7 * 86400 + 3600)}}))
        for k in range(25):
            t = now - 24 * 60 + k * 60
            series.append((t, {"five_hour": {"used_percentage": 20 + k * 0.5, "resets_at": int(now + 3 * 3600)},
                               "seven_day": {"used_percentage": 30 + k * 0.1, "resets_at": int(now + 6 * 86400)}}))
        for t, rl in series:
            subprocess.run([sys.executable, ENTRY, "statusline", "--now", str(t)], input=json.dumps(
                {"session_id": "s", "rate_limits": {
                    w: v for w, v in rl.items() if v["resets_at"] > t}}).encode(), env=cls.env, capture_output=True, check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def run_cmd(self, *args, ok=(0,)):
        p = subprocess.run([sys.executable, ENTRY] + list(args), env=self.env, capture_output=True, text=True, timeout=60)
        self.assertNotIn("Traceback", p.stdout + p.stderr, p.stderr)
        self.assertIn(p.returncode, ok, p.stdout + p.stderr)
        return p.stdout

    def test_now(self):
        out = self.run_cmd("now")
        self.assertIn("5-hour window", out)
        self.assertIn("Weekly window", out)
        json.loads(self.run_cmd("now", "--json"))
        self.assertIn("5-hour window", self.run_cmd())  # bare ctempo = now

    def test_report(self):
        out = self.run_cmd("report", "--days", "30", "-v")
        self.assertIn("Weekly windows closed: 1", out)
        json.loads(self.run_cmd("report", "--json"))

    def test_profile(self):
        self.assertIn("Hour-of-week profile", self.run_cmd("profile"))
        self.assertIn("Rebuilt", self.run_cmd("profile", "--rebuild"))
        json.loads(self.run_cmd("profile", "--json"))

    def test_check(self):
        d = json.loads(self.run_cmd("check", "--tail", "5h=4h,week=7d", "--min-free", "5", ok=(0, 1)))
        self.assertIn("ok", d)
        d = json.loads(self.run_cmd("check", "--tail", "bogus", ok=(1,)))
        self.assertFalse(d["ok"])

    def test_doctor_and_config(self):
        settings = os.path.join(self.home, "settings.json")
        with open(settings, "w") as f:
            json.dump({"statusLine": {"type": "command", "command": "x/bin/ctempo statusline", "refreshInterval": 60}}, f)
        out = self.run_cmd("doctor", "--settings", settings, ok=(0, 1))
        self.assertIn("statusLine runs ctempo", out)
        self.assertIn("database", out)
        json.loads(self.run_cmd("doctor", "--json", "--settings", settings, ok=(0, 1)))
        self.assertIn("target_5h", self.run_cmd("config"))
        self.assertTrue(self.run_cmd("config", "path").strip().endswith("config.json"))

    def test_bootstrap_without_transcripts(self):
        out = self.run_cmd("bootstrap", "--root", os.path.join(self.home, "none"), "--dry-run", ok=(1,))
        self.assertIn("No usable messages", out)


class ShiftedWindow(TempHome):
    """Review 4 (spec-docs#2): one reading with a bogus later resets_at opens a
    window that the real resets_at then closes as 'shift' (D-10). That window
    never ended, so report and profile must not count its unused points."""

    def setUp(self):
        super().setUp()
        now = time.time()
        r7 = int(now + 3 * D)
        self.feed(now - 1800, week=(40, r7))
        self.feed(now - 1500, week=(41, r7 + 7200))  # bogus later reset: rollover
        self.feed(now - 1200, week=(42, r7))  # the real reset again: anomaly
        self.feed(now - 900, week=(43, r7))  # sustained: shift, the real week reopens
        closed = [(i["close_reason"], i["unused_at_close"]) for i in self.windows(SEVEN_DAY) if i["closed_at"] is not None]
        self.assertEqual(closed, [("shift", 59.0)])

    def call(self, fn, **args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(fn(argparse.Namespace(**args), self.cfg), 0)
        return out.getvalue()

    def test_report_leaves_out_a_shift_closure(self):
        self.assertEqual(json.loads(self.call(commands.cmd_report, days=30, json=True, verbose=False))[SEVEN_DAY], [])
        out = self.call(commands.cmd_report, days=30, json=False, verbose=True)
        self.assertIn("Weekly windows closed: 0", out)
        self.assertIn("no weekly window has closed yet", out)

    def test_profile_leaves_out_a_shift_closure(self):
        out = self.call(commands.cmd_profile, rebuild=False, json=False)
        self.assertIn("needs at least one closed weekly window", out)
        self.assertNotIn("Points vs pace", out)


class NowText(TempHome):
    """Review 5: `ctempo now` gives the reasoning behind every number, so
    its reasons and its data age must be the ones that apply."""

    def now(self, now, as_json=False):
        out = io.StringIO()
        with mock.patch.object(commands.time, "time", return_value=now), contextlib.redirect_stdout(out):
            self.assertEqual(commands.cmd_now(argparse.Namespace(json=as_json), self.cfg), 0)
        return out.getvalue()

    def test_no_5h_rate_names_its_cause(self):
        # Review 5 (A-pace-display#1, X-integration#1): since the fit starts at
        # the window's first real reading (D-11), a window opened hours ago
        # has no rate for 5 minutes after work starts. `now` said "the window
        # opened less than 5 min ago" next to "resets 15:00, in 48m".
        self.known("s1")
        cases = [  # (window start, readings after it, now after it, the reason)
            (MON + 10 * H, ((4 * H + 10 * M, 80), (4 * H + 11 * M, 82)), 4 * H + 12 * M,
             "less than 5 min since work started in this window, at 14:10"),
            (MON + D + 10 * H, ((50 * M, 3), (52 * M, 5)), 54 * M,
             "less than 5 min since work started in this window, at 10:50"),
            (MON + 2 * D + 10 * H, ((2 * M, 1),), 3 * M, "the window opened less than 5 min ago"),  # it did
        ]
        for start, readings, now, why in cases:
            for t, used in readings:
                self.feed(start + t, five=(used, int(start + 5 * H)))
            out = self.now(start + now)
            self.assertIn("burn rate      unknown (%s)" % why, out)

    def test_weekly_window_fallback_names_its_cause(self):
        # Review 5 (X-integration#1): first seen 3 days into the week at 30%,
        # the level holds for 2 h. The lookback was cut at the first timed
        # reading and has seen no step (D-15), but `now` said "recent span too
        # short" although 2 h is 1.19% of the week, above the 1% minimum.
        R = int(MON + 7 * D)
        T = MON + 3 * D  # Thursday 00:00
        self.known("s1")
        for k in range(25):
            self.feed(T + k * 300, week=(30, R))
        out = self.now(T + 2 * H)
        self.assertIn("whole window so far (the level has not moved since the first timed reading, Thu 00:00)", out)
        self.assertNotIn("recent span too short", out)
        # The next week, a new session's first reading only (D-06): no timed reading yet.
        self.feed(T + 7 * D, week=(30, R + int(7 * D)), sid="s2")
        self.assertIn("whole window so far (no timed reading yet)", self.now(T + 7 * D + 2 * H))

    def test_data_age_ignores_heartbeats(self):
        # Review 5 (X-integration#2): check measures data age from readings
        # that carry information (D-20), but `now` said "last 0m ago" from an
        # idle session's heartbeats while check called the data 3 hours old.
        now = MON + 3 * D + 10 * H
        r5 = int(now + 40 * M)
        r7 = int(MON + 7 * D)
        for i in range(11):  # work from 4 h to 3 h ago
            self.feed(now - 4 * H + i * 6 * M, five=(3 * i, r5), week=(20, r7))
        for i in range(179, -1, -1):  # then idle, re-rendering every minute
            self.feed(now - i * M, five=(30, r5), week=(20, r7))
        self.assertIn("last reading 3h 00m ago, last heartbeat 0m ago.", self.now(now))
        self.assertAlmostEqual(json.loads(self.now(now, as_json=True))["data_age_s"], 3 * H)


class ProfileScale(TempHome):
    def test_profile_uses_the_lines_weekly_total(self):
        # Review 5 (X-integration#3): `ctempo profile` (and --rebuild) built its
        # weights without the typical week (D-13). After a closed week that
        # peaked at 70%, learned only from its 10 busiest hours at 1 %/h, it
        # took the ratio estimate, 10 / (10/168) = 168, and printed "typical
        # week 168% of the limit" while the line used 70.
        now = time.time()
        R1 = int(now - 2 * D)
        self.known("s1")
        for k in range(41):  # 60 -> 70% over 10 h, ending 2 h before the reset
            self.feed(R1 - 12 * H + k * 900, week=(60 + 0.25 * k, R1))
        self.feed(now - H, week=(5, R1 + int(7 * D)), sid="s2")  # the new week, a first reading
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(commands.cmd_profile(argparse.Namespace(rebuild=True, json=False), self.cfg), 0)
        self.assertIn("typical week 70% of the limit", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(commands.cmd_profile(argparse.Namespace(rebuild=False, json=True), self.cfg), 0)
        info = json.loads(out.getvalue())["info"]
        self.assertAlmostEqual(info["weekly_total"], 70.0)
        self.assertAlmostEqual(weekly.weekly(self.st, time.time(), self.cfg)["weekly_typical"], 70.0)


if __name__ == "__main__":
    unittest.main()
