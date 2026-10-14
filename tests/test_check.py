"""`ctempo check`: the scavenger's go / no-go."""

import unittest

from ctempo import commands
from ctempo.config import FIVE_HOUR, SEVEN_DAY
from ctempo.store import HEARTBEAT

from .helpers import MON, H, D, TempHome

M = 60.0


class Check(TempHome):
    def result(self, now, min_free=10, tail="5h=60m,week=2d", max_age="30m"):
        return commands.check_result(self.st, self.cfg, now, min_free, commands.parse_tail(tail, self.cfg),
                                     commands.parse_duration(max_age))

    def test_parse(self):
        t = commands.parse_tail("5h=45m,week=1.5d", self.cfg)
        self.assertEqual(t[FIVE_HOUR], 45 * 60)
        self.assertEqual(t[SEVEN_DAY], 1.5 * D)
        self.assertEqual(commands.parse_duration("90s"), 90)
        with self.assertRaises(ValueError):
            commands.parse_tail("month=2d", self.cfg)

    def test_5h_tail_with_surplus_is_go(self):
        now = MON + 3 * D + 10 * H
        r5 = int(now + 40 * M)
        r7 = int(MON + 7 * D)
        for i in range(0, 21, 2):  # idle at 30% for 20 minutes
            self.feed(now - (20 - i) * M, five=(30, r5), week=(20, r7))
        res = self.result(now)
        self.assertTrue(res["ok"], res["reasons"])
        self.assertTrue(res["five_hour"]["harvestable"])
        self.assertAlmostEqual(res["five_hour"]["projected_unused"], 70)
        self.assertFalse(res["seven_day"]["harvestable"])

    def test_not_in_tail_is_no_go(self):
        now = MON + 3 * D + 10 * H
        self.feed(now, five=(30, int(now + 3 * H)), week=(20, int(MON + 7 * D)))
        res = self.result(now)
        self.assertFalse(res["ok"])
        self.assertIn("no window is in its tail", res["reasons"][0])

    def test_stale_data_is_no_go(self):
        now = MON + 3 * D + 10 * H
        self.feed(now - 2 * H, five=(30, int(now + 40 * M)), week=(20, int(MON + 7 * D)))
        res = self.result(now)
        self.assertFalse(res["ok"])
        self.assertTrue(res["stale"])

    def test_weekly_tail_go_and_tight_week_blocks_5h(self):
        r7 = int(MON + 7 * D)
        now = r7 - D  # one day before the weekly reset
        self.known("s1")  # a timed 40, not a first reading: a FIRST never anchors the weekly lookback
        for i in range(25, -1, -1):
            self.feed(now - i * H, week=(40, r7))  # idle week at 40%
        self.feed(now, five=(5, int(now + 4 * H)), week=(40, r7))
        res = self.result(now)
        self.assertTrue(res["ok"])
        self.assertTrue(res["seven_day"]["harvestable"])
        self.assertAlmostEqual(res["seven_day"]["projected_unused"], 60)

    def test_weekly_harvest_waits_while_5h_runs_dry(self):
        r7 = int(MON + 7 * D)
        now = r7 - D
        for i in range(25, -1, -1):
            self.feed(now - i * H, week=(40, r7))
        r5 = int(now + 3 * H)
        for i in range(0, 21):  # burning 60 %/h in the 5h window
            self.feed(now - (20 - i) * M, five=(30 + i, r5), week=(40, r7))
        res = self.result(now)
        self.assertFalse(res["seven_day"]["harvestable"])
        self.assertFalse(res["ok"])
        self.assertTrue(any("no room" in r for r in res["reasons"]))

    def test_5h_leftover_blocked_when_week_runs_dry(self):
        r7 = int(MON + 7 * D)
        now = MON + 4 * D
        for i in range(30, -1, -1):  # weekly at 1 point per hour: runs dry
            self.feed(now - i * H, week=(90 - i, r7))
        r5 = int(now + 30 * M)
        for i in range(0, 21, 2):
            self.feed(now - (20 - i) * M + 1, five=(20, r5), week=(90, r7))
        res = self.result(now)
        self.assertFalse(res["five_hour"]["harvestable"])
        self.assertFalse(res["ok"])
        self.assertTrue(any("tight" in r for r in res["reasons"]))

    def test_heartbeats_do_not_make_old_data_fresh(self):
        # Review 4 (runtime-ui#2): an open idle session re-renders its last
        # reading every 60 s. Those heartbeats say nothing about the level, so
        # a level last read 3 hours ago is 3 hours old, not 1 minute.
        now = MON + 3 * D + 10 * H
        r5 = int(now + 40 * M)
        r7 = int(MON + 7 * D)
        for i in range(11):  # work from 4 h to 3 h ago: 5h 0 -> 30%
            self.feed(now - 4 * H + i * 6 * M, five=(3 * i, r5), week=(20, r7))
        for i in range(179, -1, -1):  # then idle, re-rendering every minute
            self.feed(now - i * M, five=(30, r5), week=(20, r7))
        beats = self.st.db.execute("SELECT COUNT(*) FROM samples WHERE flag=?", (HEARTBEAT,)).fetchone()[0]
        self.assertGreaterEqual(beats, 360)  # 180 per window
        res = self.result(now)
        self.assertTrue(res["stale"])
        self.assertFalse(res["ok"])
        self.assertAlmostEqual(res["data_age_s"], 3 * H)
        self.assertIn("no fresh reading in the last 30m", res["reasons"])

    def test_unknown_exchange_rate_prices_5h_leftover_at_its_maximum(self):
        # Review 4 (runtime-ui#3): until the exchange rate is published (the
        # 5h side must rise 50 points), a 5h leftover was priced at 0 weekly
        # points. Here the week ends near 99%, and spending the 65 points left
        # in the 5h window costs about 5 weekly points at the rate seen so far.
        now = MON + 3 * D + 10 * H
        r5 = int(now + 45 * M)
        r7 = int(now + 4 * H)
        t0 = now - 3.5 * H
        for k in range(26):  # 5h 10 -> 35%, week 95 -> 97%, then idle for 1 h
            self.feed(t0 + k * 6 * M, five=(10 + k, r5), week=(95 + 2 * k // 25, r7))
        res = self.result(now, max_age="2h")
        self.assertIsNone(res["exchange_rate"])
        self.assertTrue(res["exchange_rate_assumed"])
        self.assertAlmostEqual(res["five_hour"]["projected_unused"], 65)
        self.assertIsNone(res["seven_day"]["dry_at"])
        self.assertLess(res["seven_day"]["projected"], 100)
        self.assertAlmostEqual(res["weekly_cost_of_5h_leftover"], 32.5)  # 65 points at 50 per full 5h window
        self.assertFalse(res["five_hour"]["harvestable"])
        self.assertFalse(res["ok"])
        self.assertTrue(any("tight" in r for r in res["reasons"]), res["reasons"])

    def test_known_exchange_rate_is_not_flagged_as_assumed(self):
        # Review 4 (runtime-ui#3): once published (50 paired 5h points, 3
        # weekly), the observed rate prices the leftover: 3 per 50 is 6 per window.
        now = MON + 3 * D + 10 * H
        r5 = int(now + 45 * M)
        r7 = int(MON + 7 * D)
        t0 = now - 4 * H
        for k in range(51):  # 5h 0 -> 50%, week 20 -> 23%, then idle for 1 h
            self.feed(t0 + k * 3.6 * M, five=(k, r5), week=(20 + 3 * k // 50, r7))
        res = self.result(now, max_age="2h")
        self.assertAlmostEqual(res["exchange_rate"], 6.0)
        self.assertFalse(res["exchange_rate_assumed"])
        self.assertAlmostEqual(res["weekly_cost_of_5h_leftover"], 3.0)  # 50 points left at 6 per window
        self.assertTrue(res["five_hour"]["harvestable"])


if __name__ == "__main__":
    unittest.main()
