"""Weekly math against hand-computed values."""

import unittest

from ctempo import profile, weekly
from ctempo.config import FIVE_HOUR, SEVEN_DAY

from .helpers import MON, H, D, TempHome

WORKWEEK = {"work_days": [0, 1, 2, 3, 4], "wake_hours": [9, 17], "off_day_factor": 0.0, "mode": "replace"}


class Weekly(TempHome):
    def view(self, now):
        return weekly.weekly(self.st, now, self.cfg)

    def test_uniform_midweek_linear_history(self):
        # Window Mon 00:00 -> next Mon 00:00, now Thu 12:00 (84 h in, E = 0.5).
        R = int(MON + 7 * D)
        now = MON + 84 * H
        for h in range(0, 85):
            self.feed(MON + h * H, week=(40.0 * h / 84.0, R))
        v = self.view(now)
        self.assertAlmostEqual(v["E"], 0.5, places=9)
        self.assertAlmostEqual(v["on_pace"], 50.0, places=6)
        self.assertAlmostEqual(v["delta"], -10.0, places=6)
        # Last 24 h: spent 40*24/84 = 11.43, profile expected 100*24/168 = 14.29.
        self.assertEqual(v["intensity_source"], "recent")
        self.assertAlmostEqual(v["intensity"], 0.8, places=6)
        self.assertAlmostEqual(v["projected"], 80.0, places=6)  # 40 + 0.8 * 100 * 0.5
        self.assertAlmostEqual(v["multiplier"], 1.5, places=6)  # (100 - 40) / 40
        self.assertAlmostEqual(v["expire"], 20.0, places=6)
        self.assertEqual(v["verdict"], "speed_up")
        self.assertIsNone(v["dry_ts"])

    def test_profile_with_nights_and_weekends(self):
        self.cfg["profile"]["override"] = dict(WORKWEEK)
        R = int(MON + 7 * D)
        now = MON + 2 * D + 13 * H  # Wednesday 13:00
        self.feed(now, week=(30, R))
        v = self.view(now)
        # 40 awake bins at 1.0; typical = mean of the top 56 bins = 40/56;
        # floor = 0.15 * 40/56 = 6/56 for the other 128 bins.
        f = 0.15 * 40 / 56
        total = 40 + 128 * f
        # Ahead: Wed 13-17 (4) + Thu (8) + Fri (8) awake hours, and 107 - 20 = 87 off hours.
        E = (20 + 87 * f) / total
        self.assertAlmostEqual(v["E"], E, places=9)
        self.assertAlmostEqual(v["on_pace"], 100 * (1 - E), places=6)
        self.assertEqual(v["awake_hours"], 40)

    def test_active_lookback_skips_nights_and_weekend(self):
        w, awake, _ = profile.Profile().weights(MON, {"profile": dict(self.cfg["profile"], override=WORKWEEK)})
        now = MON + 2 * D + 13 * H  # Wed 13:00
        # 24 awake hours back: Wed 9-13 (4), Tue (8), Mon (8), then last Fri 13-17 (4).
        t0 = profile.active_lookback(awake, now, 24, 7 * D)
        self.assertEqual(t0, MON - 3 * D + 13 * H)

    def test_dry_walk_uniform(self):
        now = MON + 4 * D
        R = int(now + 3 * D)
        for h in range(30, -1, -1):  # 1 point per hour, 90% now
            self.feed(now - h * H, week=(90 - h, R))
        v = self.view(now)
        # 24 points in 24 h vs 100*24/168 expected: intensity 1.68, i.e. 1 %/h.
        self.assertAlmostEqual(v["intensity"], 1.68, places=6)
        self.assertAlmostEqual(v["dry_ts"], now + 10 * H, places=3)
        self.assertAlmostEqual(v["projected_raw"], 90 + 72, places=6)  # 72 h left at 1 %/h
        self.assertEqual(v["severity"], "alert")
        self.assertAlmostEqual(v["multiplier"], 10 / 72, places=6)

    def test_lookback_crosses_a_reset(self):
        now = MON + 3 * D
        r_prev = int(now - 6 * H)
        r_cur = int(r_prev + 7 * D)
        self.feed(now - 30 * H, week=(70, r_prev))
        self.feed(now - 6 * H - 60, week=(80, r_prev))
        self.feed(now - 5 * H, week=(2, r_cur))
        self.feed(now, week=(5, r_cur))
        v = self.view(now)
        # Previous window: 80 - 70 = 10; current: 5 - 0 = 5. Expected 100*24/168.
        self.assertAlmostEqual(v["lookback_actual"], 15.0)
        self.assertAlmostEqual(v["intensity"], 15 / (100 * 24 / 168), places=6)

    def test_bogus_shifted_window_is_not_taken_as_the_previous_week(self):
        R1 = int(MON + 7 * D)
        for h in range(0, 167, 2):  # week 1 at 0.5 %/h, one bogus later reset in the middle
            self.feed(MON + h * H, week=(0.5 * h, R1), sid="a")
            if h == 100:
                self.feed(MON + h * H + 60, week=(50, R1 + 1200), sid="b")
        R2 = R1 + int(7 * D)
        now = R1 + 6 * H
        for h in range(0, 7):
            self.feed(R1 + h * H, week=(0.5 * h, R2), sid="a")
        v = self.view(now)
        # Lookback 24 h: 18 h of week 1 at 0.5 plus 6 h of week 2 at 0.5 = 12 points.
        self.assertAlmostEqual(v["lookback_actual"], 12.0, delta=1.01)
        self.assertGreater(v["intensity"], 0.7)

    def test_parallel_sessions_running_max(self):
        R = int(MON + 7 * D)
        now = MON + 3 * D
        for i in range(24, -1, -1):
            self.feed(now - i * H, week=(50 - i * 0.5, R), sid="busy")
            self.feed(now - i * H + 30, week=(20, R), sid="idle")
        v = self.view(now)
        self.assertEqual(v["used"], 50)
        self.assertAlmostEqual(v["lookback_actual"], 12.0)
        self.assertEqual(len(self.windows(SEVEN_DAY)), 1)

    def test_lookback_reaching_window_start_uses_zero_there(self):
        R = int(MON + 7 * D)
        now = MON + D
        self.feed(now, week=(10, R))  # first sample, but the window began 24 h ago at 0%
        v = self.view(now)
        self.assertEqual(v["intensity_source"], "recent")
        self.assertAlmostEqual(v["intensity"], 10 / (100 * 24 / 168), places=6)

    def test_window_fallback_when_lookback_start_unknown(self):
        R = int(MON + 7 * D)
        now = MON + 3 * D
        self.feed(now, week=(10, R))  # first sample ever, 3 days into the window
        v = self.view(now)
        self.assertEqual(v["intensity_source"], "window")
        self.assertAlmostEqual(v["intensity"], 10 / (100 * 3 / 7), places=6)

    def test_missing_weekly_window(self):
        self.feed(MON + H, five=(10, int(MON + 3 * H)))
        self.assertFalse(self.view(MON + H)["present"])

    def test_exchange_rate(self):
        R5 = int(MON + 5 * H)
        R7 = int(MON + 7 * D)
        for i in range(61):
            self.feed(MON + i * 240, five=(i * 1.0, R5), week=(i * 0.1, R7))
        ppw, rf, rs = weekly.exchange_rate(self.st, MON + D)
        self.assertAlmostEqual(rf, 60)
        self.assertAlmostEqual(rs, 6)
        self.assertAlmostEqual(ppw, 10.0)  # a full 5h window = 10% of the week


if __name__ == "__main__":
    unittest.main()
