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


class LearnEdges(TempHome):
    def learned(self, now):
        prof = profile.load(self.st)
        profile.learn(self.st, prof, now, self.cfg, profile.uniform())
        return prof

    def test_moved_reset_is_a_continuation_not_new_spend(self):
        R = int(MON + 7 * D)
        t = MON + 2 * D + 9 * H  # Wednesday 09:00, 1 point an hour from 50%
        for k in range(13):
            self.feed(t + k * 900, week=(50 + k * 0.25, R))
        R2 = R + 1800  # resets_at moves 30 minutes later (more than the jitter)
        for k in range(13, 25):
            self.feed(t + k * 900, week=(50 + k * 0.25, R2))
        prof = self.learned(t + 8 * H)
        b = profile.bin_of(t + 3 * H)  # the hour of the move
        self.assertAlmostEqual(prof.S[b] / prof.W[b], 1.0, places=6)

    def test_learning_keeps_a_prior_committed_after_the_profile_was_read(self):
        from ctempo import statusline
        from ctempo.store import Store

        self.feed(MON + H, week=(5, int(MON + 7 * D)))
        stale = profile.load(self.st)  # what a render read before learning
        other = Store(os.path.join(self.dir, "tempo.db"))  # bootstrap, in another process
        fresh = profile.load(other)
        fresh.prior = profile.uniform()
        fresh.prior_meta = {"trust": 1.0, "messages": 3}
        other.begin()
        profile.save(other, fresh)
        other.commit()
        other.close()
        kept = statusline.maybe_learn(self.st, stale, MON + 3 * H, self.cfg)
        self.assertIsNotNone(kept.prior)
        self.assertIsNotNone(profile.load(self.st).prior)

    def test_batch_and_incremental_learning_agree_with_parallel_dips(self):
        from ctempo import ingest
        from ctempo.store import Store

        R = int(MON + 7 * D)
        rows = []
        for k in range(32):  # a: +0.5 at :05 :20 :35 :50; b lags 0.5 at :58
            t = MON + 9 * H + 5 * 60 + k * 900
            rows.append((t, 10 + k * 0.5, "a"))
            if k % 4 == 3:
                rows.append((t + 8 * 60, 10 + k * 0.5 - 0.5, "b"))
        end = MON + 18 * H
        for t, used, sid in rows:
            self.feed(t, week=(used, R), sid=sid)
        batch = self.learned(end)
        st = Store(os.path.join(self.dir, "incremental.db"))
        inc = profile.Profile()
        for t, used, sid in rows:  # learn after every reading, as the statusline would
            r, rej, k = ingest.parse_payload({"rate_limits": {"seven_day": {"used_percentage": used, "resets_at": R}}}, t)
            ingest.record(st, r, t, "test", sid, self.cfg, rej, k)
            profile.learn(st, inc, t, self.cfg, profile.uniform())
        st.close()
        self.assertEqual(batch.processed_until, inc.processed_until)
        self.assertAlmostEqual(sum(batch.S), sum(inc.S), places=6)
        self.assertAlmostEqual(sum(batch.W), sum(inc.W), places=6)


if __name__ == "__main__":
    unittest.main()
