"""Readings from Claude Code's /usage (ctempo/usagecmd.py)."""

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from ctempo import config, menubar, usagecmd

from .helpers import TempHome, _zone_info

# The person's own output (Claude Code 2.1.302, Asia/Tbilisi, UTC+4).
OUTPUT = """You are currently using your subscription to power your Claude Code usage

Current session: 11% used · resets Oct 17 at 6:20pm (Asia/Tbilisi)
Current week (all models): 20% used · resets Oct 23 at 6am (Asia/Tbilisi)
Current week (Fable): 0% used · resets Oct 23 at 6am (Asia/Tbilisi)

What's contributing to your limits usage?
Last 24h · 2 requests · 2 sessions
"""
NOW = 1792233000.0  # 2026-10-17 13:10 in Tbilisi (09:10 UTC)
R5 = 1792246800  # 2026-10-17 18:20 Tbilisi
R7 = 1792720800  # 2026-10-23 06:00 Tbilisi


def needs_zones(test):
    return unittest.skipIf(_zone_info("Asia/Tbilisi") is None, "no zoneinfo or zone database")(test)


class Parse(unittest.TestCase):
    @needs_zones
    def test_the_real_output(self):
        self.assertEqual(usagecmd.parse(OUTPUT, NOW),
                         {config.FIVE_HOUR: (11.0, R5), config.SEVEN_DAY: (20.0, R7)})

    @needs_zones
    def test_times_without_a_date_and_unknown_lines(self):
        self.assertEqual(usagecmd.reset_epoch("6:20pm", "Asia/Tbilisi", NOW), R5)
        self.assertEqual(usagecmd.reset_epoch("6am", "Asia/Tbilisi", NOW),
                         usagecmd.reset_epoch("Oct 18 at 6am", "Asia/Tbilisi", NOW))  # the next 6am
        self.assertIsNone(usagecmd.reset_epoch("tomorrow", "Asia/Tbilisi", NOW))
        self.assertIsNone(usagecmd.reset_epoch("6am", "Not/AZone", NOW))
        self.assertEqual(usagecmd.parse("Current session: 11% used\nsomething else\n", NOW), {})


class Fetch(TempHome):
    def run_fetch(self, text, rc=0, now=NOW):
        return usagecmd.fetch(now, run=lambda: (text, rc))

    @needs_zones
    def test_records_both_windows_from_its_own_session(self):
        ok, msg = self.run_fetch(OUTPUT)
        self.assertTrue(ok, msg)
        rows = self.st.db.execute("SELECT window, used_pct, resets_at, source, session_id FROM samples ORDER BY window")
        self.assertEqual(rows.fetchall(), [(config.FIVE_HOUR, 11.0, R5, "usage", "usage-cmd"),
                                           (config.SEVEN_DAY, 20.0, R7, "usage", "usage-cmd")])
        self.assertEqual(self.st.meta_get("usage_cmd_ok_at"), NOW)
        self.assertTrue(os.path.exists(config.stamp_path()))  # a new window wakes the bands

    def test_a_failure_records_nothing(self):
        self.assertEqual(self.run_fetch("Error: not logged in", rc=1)[0], False)
        self.assertEqual(self.st.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0)

    def test_due_runs_at_most_every_so_many_minutes(self):
        self.assertFalse(usagecmd.due(self.st, NOW, 0))
        self.assertTrue(usagecmd.due(self.st, NOW, 10))
        self.assertFalse(usagecmd.due(self.st, NOW + 300, 10))
        self.assertTrue(usagecmd.due(self.st, NOW + 601, 10))


class MenuBar(TempHome):
    @needs_zones
    def test_the_menu_bar_fetches_when_asked_and_says_so(self):
        config.set_value(("menubar", "fetch_usage_min"), 10)
        cfg = config.load()
        out, real = io.StringIO(), usagecmd.fetch
        with contextlib.redirect_stdout(out), mock.patch("time.time", return_value=NOW + 60), \
                mock.patch.object(usagecmd, "fetch", side_effect=lambda now: real(now, run=lambda: (OUTPUT, 0))):
            menubar.show(cfg)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("11% · 20% | image="), out.getvalue())
        self.assertIn("Updated just now, checked just now | size=11 color=%s" % menubar.DIM, lines)

    def test_off_by_default(self):
        self.assertEqual(config.load()["menubar"]["fetch_usage_min"], 0)
        with mock.patch.object(usagecmd, "fetch") as fetch, contextlib.redirect_stdout(io.StringIO()):
            menubar.show(config.load())
        fetch.assert_not_called()

    def test_set_value_keeps_the_rest_of_the_file(self):
        path = config.config_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"target_5h": 90, "menubar": {"other": 1}}, f)
        config.set_value(("menubar", "fetch_usage_min"), 5)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"target_5h": 90, "menubar": {"other": 1, "fetch_usage_min": 5}})
