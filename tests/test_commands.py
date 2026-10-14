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

from ctempo import commands
from ctempo.config import SEVEN_DAY

from .helpers import D, TempHome

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "bin", "ctempo")


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


if __name__ == "__main__":
    unittest.main()
