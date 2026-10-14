"""Smoke tests: every command runs on seeded data without a traceback."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
