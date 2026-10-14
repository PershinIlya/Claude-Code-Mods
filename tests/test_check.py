"""`ctempo check`: the scavenger's go / no-go."""

import unittest

from ctempo import commands
from ctempo.config import FIVE_HOUR, SEVEN_DAY

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


if __name__ == "__main__":
    unittest.main()
