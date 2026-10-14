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
from ctempo.store import Store

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


class MovedReset(TempHome):
    def test_report_counts_a_week_whose_reset_moved_once(self):
        # Review 8 (long-horizon#3): a week whose resets_at moved 1 h later on
        # its fourth day, at a rising level. The move closed the running week
        # as a rollover, so `report` listed two finished weeks and 95 unused
        # points where one week left 33.
        now = time.time()
        R = int(now - 2 * D)  # the reset after the move
        start = R - H - 7 * D
        t = start + 2 * H
        while t < R - 2 * H:
            self.feed(t, week=(round(60 * (t - start) / (7 * D), 1), R - int(H) if t < start + 4 * D else R))
            t += 2 * H
        self.feed(R - H, week=(67, R))
        self.feed(now - H, week=(5, R + int(7 * D)))  # the next week
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(commands.cmd_report(argparse.Namespace(days=30, json=False, verbose=False), self.cfg), 0)
        self.assertIn("Weekly windows closed: 1\n", out.getvalue())
        self.assertIn("Totals: 33 weekly points unused over 1 weeks", out.getvalue())


class ProfileText(TempHome):
    def profile(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(commands.cmd_profile(argparse.Namespace(rebuild=False, json=False), self.cfg), 0)
        return out.getvalue()

    def test_reset_column_note_follows_the_weekly_target(self):
        # Review 8 (mod-and-texts#1): the points-vs-pace columns are the level
        # minus the on-pace level for target_week, so at the reset it is peak
        # minus target. At target 90 a week that peaked at 89 printed "reset
        # -1" as "what expired", while `report` said 11 points expired.
        now = time.time()
        R = int(now - 2 * D)
        for k in range(1, 84):
            self.feed(R - 7 * D + k * 2 * H, week=(round(89 * k / 83, 1), R))
        self.feed(now - H, week=(5, R + int(7 * D)))  # the next week closes it
        out = self.profile()
        self.assertIn("reset -11\n", out)
        self.assertIn("(negative = surplus building up; the reset column is what expired)", out)
        self.cfg["target_week"] = 90
        out = self.profile()
        self.assertIn("reset -1\n", out)
        self.assertIn("(negative = surplus building up; the reset column is the peak versus your 90% target; "
                      "`ctempo report` shows what expired)", out)

    def test_lightest_awake_blocks_are_awake(self):
        # Review 8 (mod-and-texts#2): a block counted as awake on its weight
        # alone. A flat profile listed "Mon 00-03, Mon 03-06, Mon 06-09" (a
        # tie), and a regular day listed blocks two-thirds asleep, light only
        # because of the hours asleep.
        self.assertNotIn("Lightest awake blocks", self.profile())
        self.cfg["profile"]["override"] = {"work_days": [0, 1, 2, 3, 4], "wake_hours": [8, 22],
                                           "off_day_factor": 0.6, "mode": "replace"}
        out = self.profile()
        self.assertIn("Awake hours per week: 98 of 168", out)
        self.assertIn("Lightest awake blocks: Sat 09-12, Sat 12-15, Sat 15-18\n", out)


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

    def test_typical_and_assumed_fallbacks_name_their_cause(self):
        # Review 6 (E-display-commands-weekly#3): a cut lookback whose window is
        # still at 0 falls back to the typical week, then 1.0 (D-15), but `now`
        # said "your typical week (no recent data)" or "assumed (no data yet)"
        # above "Data: 25 samples, last reading 2h 00m ago". The week reset
        # Saturday 02:00 and the readings from Monday 09:00 are 0.
        R0 = int(MON - 2 * D + 2 * H)
        R1 = R0 + int(7 * D)
        still = "the level has not moved since the first timed reading, Mon 09:00; the window is still at 0%"
        cases = [  # (closed week at 70%, timed readings, the label)
            (True, True, "your typical week (%s)" % still),
            (False, True, "assumed (%s)" % still),
            (False, False, "assumed (no timed reading yet; the window is still at 0%)"),  # a new session's first reading
        ]
        for i, (closed, timed, label) in enumerate(cases):
            self.use_home("zero-%d" % i)
            if timed:
                self.known("s1")
            if closed:
                self.feed(R0 - 12 * H, week=(69, R0))
                self.feed(R0 - 2 * H, week=(70, R0))
            for k in range(25):
                self.feed(MON + 9 * H + k * 300, week=(0, R1))
            out = self.now(MON + 11 * H)
            self.assertIn("intensity       %.2f     %s\n" % (0.7 if closed else 1.0, label), out)
            self.assertNotIn("no recent data", out)
            self.assertNotIn("no data yet", out)
        # A week that opened at 10:00, read at 1% and 2% since: no earlier week,
        # and an hour is under 2% of a week's expected spend.
        self.use_home("young")
        self.known("s1")
        R2 = int(MON + 10 * H + 7 * D)
        self.feed(MON + 10 * H + 10 * M, week=(1, R2))
        self.feed(MON + 10 * H + 40 * M, week=(2, R2))
        out = self.now(MON + 11 * H)
        self.assertIn("intensity       1.00     assumed (recent span too short; the window is too new, opened Mon 10:00)\n", out)

    def use_home(self, name):
        """A fresh store in its own CTEMPO_HOME under the test's directory."""
        self.st.close()
        home = os.path.join(self.dir, name)
        os.makedirs(home)
        os.environ["CTEMPO_HOME"] = home
        self.st = Store(os.path.join(home, "tempo.db"))

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
