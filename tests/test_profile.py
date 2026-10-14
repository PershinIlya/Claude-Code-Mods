import copy
import os
import time
import unittest

from ctempo import config, profile
from ctempo.config import SEVEN_DAY

from .helpers import MON, H, D, TempHome, utc


class Shape(unittest.TestCase):
    def setUp(self):
        utc()
        self.cfg = copy.deepcopy(config.DEFAULTS)

    def test_empty_profile_is_uniform(self):
        w, awake, info = profile.Profile().weights(MON, self.cfg)
        self.assertAlmostEqual(sum(w), 1.0)
        self.assertTrue(all(abs(x - 1 / 168) < 1e-12 for x in w))
        self.assertEqual(info["source"], "uniform")
        self.assertEqual(sum(awake), 168)

    def test_ewma_half_life_by_hand(self):
        p = profile.Profile()
        hl = 14 * D
        p.observe(MON + 9 * H, 1.0, 1.0, hl)
        p.observe(MON + 14 * D + 9 * H, 0.0, 1.0, hl)  # same bin, 14 days later
        b = profile.bin_of(MON + 9 * H)
        # Weights 0.5 (two weeks old) and 1: mean = (0.5*1 + 1*0) / 1.5 = 1/3.
        self.assertAlmostEqual(p.S[b] / p.W[b], 1 / 3)
        m, n = p.observed(MON + 14 * D + 9 * H, hl)[b]
        self.assertAlmostEqual(n, 1.5)

    def test_shrinkage_by_hand(self):
        p = profile.Profile()
        hl = 14 * D
        b1, b2 = 10, 20
        p.S[b1], p.W[b1], p.T[b1] = 2.0, 1.0, MON
        p.S[b2], p.W[b2], p.T[b2] = 0.0, 1.0, MON
        r, M, src = p.rates(MON, self.cfg)  # uniform prior: k = 0.25
        # M = (2 + 0) / (1/168 + 1/168) = 168 points per week, so M p = 1.
        self.assertAlmostEqual(M, 168.0)
        self.assertAlmostEqual(r[b1], (2 + 0.25) / 1.25)  # (n m + k M p) / (n + k) = 1.8
        self.assertAlmostEqual(r[b2], (0 + 0.25) / 1.25)  # 0.2
        self.assertAlmostEqual(r[0], 1.0)  # unobserved: M p
        w, _, _ = p.weights(MON, self.cfg)
        self.assertAlmostEqual(w[b1], 1.8 / 168)
        # An informative prior (transcripts) uses k = 1: (2 + 1) / 2 = 1.5.
        p.prior = profile.uniform()
        r, M, src = p.rates(MON, self.cfg)
        self.assertEqual(src, "transcripts+observed")
        self.assertAlmostEqual(r[b1], 1.5)
        # A known weekly total replaces the ratio estimate: M p = 84/168.
        r, M, _ = p.rates(MON, self.cfg, weekly_total=84.0)
        self.assertAlmostEqual(r[b1], (2 + 0.5) / 2)
        self.assertAlmostEqual(r[0], 0.5)

    def test_floor_keeps_off_hours_alive(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["profile"]["override"] = {"work_days": [0, 1, 2, 3, 4], "wake_hours": [9, 17], "off_day_factor": 0.0, "mode": "replace"}
        w, awake, _ = profile.Profile().weights(MON, cfg)
        on = w[9]  # Mon 09:00
        off = w[3]  # Mon 03:00
        self.assertAlmostEqual(off / on, 0.15 * 40 / 56)
        self.assertEqual(sum(awake), 40)

    def test_walk_to_and_share(self):
        w = profile.uniform()
        self.assertAlmostEqual(profile.share_between(w, MON, MON + 84 * H), 0.5)
        self.assertAlmostEqual(profile.walk_to(w, MON, MON + 7 * D, 168.0, 10.0), MON + 10 * H)
        self.assertIsNone(profile.walk_to(w, MON, MON + 5 * H, 168.0, 10.0))


class DST(unittest.TestCase):
    def tearDown(self):
        utc()

    def test_segments_across_fall_back(self):
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        # Sunday 2026-11-01: clocks go back at 02:00, the day has 25 hours.
        start = 1793505600  # 2026-11-01 00:00 EDT (04:00 UTC)
        segs = list(profile.segments(start, start + 25 * H))
        self.assertEqual(len(segs), 25)
        self.assertTrue(all(abs((e - s) - H) < 1e-6 for s, e, _, _ in segs))
        self.assertEqual(time.localtime(start + 25 * H).tm_hour, 0)
        self.assertEqual(segs[1][3], segs[2][3])  # both 01:00 hours share Sunday 01:00


class Learn(TempHome):
    def test_learns_work_hours_from_weekly_percent(self):
        # Three weeks: +1 point per hour Mon-Fri 9-17, nothing otherwise,
        # fresh readings every 15 minutes while it moves.
        used, t = 0.0, MON
        R = int(MON + 3 * D + 9 * H)  # weekly reset Thursday 09:00
        end = MON + 21 * D
        while t < end:
            if t >= R:
                R += int(7 * D)
                used = 0.0
            lt = time.gmtime(t)
            if lt.tm_wday < 5 and 9 <= lt.tm_hour < 17:
                used += 0.25
                self.feed(t + 15 * 60, week=(round(used, 2), R))
            t += 15 * 60
        prof = profile.load(self.st)
        n = profile.learn(self.st, prof, end, self.cfg, profile.uniform())
        self.assertGreater(n, 300)
        w, awake, info = prof.weights(end, self.cfg)
        work = w[2 * 24 + 11]  # Wed 11:00
        night = w[2 * 24 + 3]  # Wed 03:00
        weekend = w[5 * 24 + 11]  # Sat 11:00
        self.assertGreater(work / night, 5)
        self.assertGreater(work / weekend, 5)
        self.assertAlmostEqual(sum(w), 1.0)
        # Ratio estimate of the weekly total: 40 points a week.
        self.assertAlmostEqual(info["weekly_total"], 40.0, delta=8.0)
        # With closed windows the scale comes from their peaks instead.
        # Closed windows reset Thu 09:00 with peaks 24 (data began mid-window),
        # 40 and 40, weighted 0.5 ** (age / 14 days).
        typical = profile.typical_week(self.st, end, 14 * D)
        resets = [MON + 3 * D + 9 * H + k * 7 * D for k in range(3)]
        a = [0.5 ** ((end - r) / (14 * D)) for r in resets]
        self.assertAlmostEqual(typical, (a[0] * 24 + a[1] * 40 + a[2] * 40) / sum(a), places=6)
        # Learning again finds nothing new.
        self.assertEqual(profile.learn(self.st, prof, end, self.cfg, w), 0)


if __name__ == "__main__":
    unittest.main()
