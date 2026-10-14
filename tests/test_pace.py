"""5-hour math against hand-computed values."""

import unittest

from ctempo import pace
from ctempo.config import FIVE_HOUR

from .helpers import MON, H, TempHome

M = 60.0


class Slope(unittest.TestCase):
    def test_lsq_slope_by_hand(self):
        # (0 h, 0), (1 h, 10), (2 h, 20): 10 %/h exactly.
        self.assertAlmostEqual(pace.lsq_slope([(0, 0), (3600, 10), (7200, 20)]), 10.0)
        # x = 0, 1/12, 1/6 h; y = 40, 40, 50:
        # Sxy = (1/12)(10/3) + (1/12)(20/3) = 10/12, Sxx = 2/144, slope = 60 %/h.
        self.assertAlmostEqual(pace.lsq_slope([(0, 40), (300, 40), (600, 50)]), 60.0)
        self.assertIsNone(pace.lsq_slope([(5, 1)]))
        self.assertIsNone(pace.lsq_slope([(5, 1), (5, 2)]))

    def test_running_series_ignores_heartbeat_values(self):
        rows = [(1, 10, 0), (2, 5, 1), (3, 12, 0), (4, 3, 1)]
        self.assertEqual(pace.running_series(rows), [(1, 10), (2, 10), (3, 12), (4, 12)])


class FiveHour(TempHome):
    def setUp(self):
        super().setUp()
        self.t = MON + 10 * H
        self.R = int(self.t + 2 * H)

    def view(self):
        return pace.five_hour(self.st, self.t, self.cfg)

    def test_steady_burn(self):
        # 0.1 point per minute = 6 %/h for 20 minutes, ending at 50%.
        for i in range(21):
            self.feed(self.t - (20 - i) * M, five=(round(48 + 0.1 * i, 1), self.R))
        v = self.view()
        self.assertAlmostEqual(v["rate"], 6.0, places=6)
        self.assertEqual(v["rate_source"], "lsq")
        self.assertAlmostEqual(v["allowed"], 25.0)  # (100 - 50) / 2 h
        self.assertAlmostEqual(v["multiplier"], 25.0 / 6.0)
        self.assertAlmostEqual(v["projected"], 62.0)  # 50 + 6 * 2
        self.assertIsNone(v["cap_ts"])
        self.assertEqual(v["verdict"], "speed_up")
        self.assertEqual(v["severity"], "dim")

    def test_burst_then_cap_time(self):
        self.known("s1", "s2")
        self.feed(self.t - 20 * M, five=(40, self.R))
        self.feed(self.t - 10 * M, five=(40, self.R), sid="s2")
        self.st.add_sample(self.t - 5 * M, FIVE_HOUR, 70, self.R, "test", "idle", 1, 1)  # a heartbeat: no level info
        self.feed(self.t, five=(50, self.R))
        v = self.view()
        # Level path: 40 at -20 min (s2's 40 is no new level), 50 now: 10 points
        # in 1/3 h = 30 %/h; three fresh readings in the window, so "lsq".
        self.assertAlmostEqual(v["rate"], 30.0, places=6)
        self.assertEqual(v["rate_source"], "lsq")
        self.assertAlmostEqual(v["multiplier"], 25.0 / 30.0)  # allowed (100 - 50) / 2 h
        self.assertEqual(v["verdict"], "slow_down")
        self.assertAlmostEqual(v["projected_raw"], 110.0)
        self.assertEqual(v["projected"], 100.0)
        self.assertAlmostEqual(v["cap_ts"], self.t + 100 * M)  # (100 - 50) / 30 h
        self.assertEqual(v["severity"], "alert")

    def test_heartbeats_never_make_a_burst(self):
        # An idle session re-renders its stale 52 after a sleep; the busy one read 60 and 61.
        self.feed(self.t - 60 * M, five=(60, self.R), sid="busy")
        self.feed(self.t - 59 * M, five=(52, self.R), sid="idle")  # its first, lower reading
        self.feed(self.t - 8 * M, five=(52, self.R), sid="idle")  # heartbeat after a 40-minute sleep
        self.feed(self.t - 7 * M, five=(61, self.R), sid="busy")
        for k in range(1, 8):
            self.feed(self.t - 7 * M + k * 60, five=(61, self.R), sid="busy")
        v = self.view()
        # Level path: 60 an hour ago, linear to 61 seven minutes ago, then flat:
        # a few %/h at most; never a burst from the idle session's stale 52.
        self.assertLess(v["rate"], 4.0)
        self.assertIsNone(v["cap_ts"])

    def test_open_session_heartbeats_do_not_squeeze_a_gap(self):
        self.feed(self.t - 2 * H, five=(30, self.R))
        for k in range(1, 120):  # the session stays open: a heartbeat a minute at 30
            self.feed(self.t - 2 * H + k * 60, five=(30, self.R))
        self.feed(self.t, five=(70, self.R), sid="s2")  # 40 points spent on claude.ai meanwhile
        v = self.view()
        self.assertAlmostEqual(v["rate"], 20.0, places=6)  # spread over the 2 h gap
        self.assertIsNone(v["cap_ts"] if v["projected_raw"] < 100 else None)

    def test_idle_is_zero_rate_and_shows_allowed(self):
        self.feed(self.t - 30 * M, five=(50, self.R))
        for i in range(1, 31, 2):
            self.feed(self.t - 30 * M + i * M, five=(50, self.R))  # heartbeats every 2 min
        v = self.view()
        self.assertEqual(v["rate"], 0.0)
        self.assertTrue(v["idle"])
        self.assertEqual(v["verdict"], "idle")
        self.assertIsNone(v["multiplier"])
        self.assertAlmostEqual(v["allowed"], 25.0)
        self.assertEqual(v["projected"], 50)

    def test_burst_fades_out_of_the_fit_window(self):
        self.feed(self.t - 40 * M, five=(30, self.R))
        self.feed(self.t - 38 * M, five=(45, self.R))  # burst 40 minutes ago
        for i in range(37, -1, -1):
            self.feed(self.t - i * M, five=(45, self.R))
        self.assertEqual(self.view()["rate"], 0.0)

    def test_one_reading_gives_the_window_average(self):
        r = int(self.t + 3 * H)  # window started 2 h ago, at 0
        self.feed(self.t, five=(30, r))
        v = self.view()
        self.assertEqual(v["rate_source"], "interpolated")
        self.assertAlmostEqual(v["rate"], 15.0)  # the path rises 30 points over 2 h
        self.assertAlmostEqual(v["allowed"], 70.0 / 3.0)
        self.assertAlmostEqual(v["projected"], 75.0)

    def test_unobserved_gap_is_averaged_not_squeezed_into_20_minutes(self):
        self.feed(self.t - 2 * H, five=(30, self.R))  # last read 2 h ago
        self.feed(self.t, five=(70, self.R))  # claude.ai use in between
        v = self.view()
        self.assertEqual(v["rate_source"], "interpolated")
        self.assertAlmostEqual(v["rate"], 20.0)  # 40 points over 2 h, not 120 %/h
        # 70 + 20 * 2 h = 110: the cap comes after 30 / 20 = 1.5 h, not in 15 minutes.
        self.assertAlmostEqual(v["projected_raw"], 110.0)
        self.assertAlmostEqual(v["cap_ts"], self.t + 1.5 * H)

    def test_idle_after_early_burst_is_idle_not_dry(self):
        r = int(self.t + 30 * M)  # window opened 4.5 h ago
        for i in range(0, 61, 5):  # 90 points in its first hour
            self.feed(r - 5 * H + i * M, five=(min(90, 1.5 * i), r))
        self.feed(self.t - 120, five=(90, r))  # first reading after 3.5 idle hours
        self.feed(self.t - 60, five=(90, r), sid="s2")
        self.feed(self.t, five=(90, r), sid="s3")  # readings minutes apart must not hide the gap
        v = self.view()
        self.assertEqual(v["rate"], 0.0)
        self.assertEqual(v["verdict"], "idle")
        self.assertEqual(v["severity"], "opportunity")  # 10 points left, 30 min to go

    def test_maxed(self):
        self.feed(self.t, five=(100, self.R))
        v = self.view()
        self.assertEqual(v["verdict"], "maxed")
        self.assertEqual(v["severity"], "alert")

    def test_tail_with_surplus_is_an_opportunity(self):
        r = int(self.t + 30 * M)
        for i in range(0, 21, 2):
            self.feed(self.t - (20 - i) * M, five=(40, r))
        v = self.view()
        self.assertTrue(v["in_tail"])
        self.assertEqual(v["unused_at_reset"], 60)
        self.assertEqual(v["severity"], "opportunity")

    def test_no_window(self):
        self.assertFalse(self.view()["present"])
        self.feed(self.t - 6 * H, five=(10, int(self.t - 2 * H)))
        self.assertFalse(self.view()["present"])

    def test_steady_whole_percent_steps_keep_their_rate(self):
        # Review #0: 9 %/h in whole percents is one new value every 6 min 40 s.
        # 85% with 2 h left: allowed 7.5 %/h, so every render (one every 20 s)
        # must say slow down and show the cap, also between steps.
        self.known("s1")
        t0 = self.t - 60 * M
        for k in range(0, 181):
            ts = t0 + 20 * k
            self.feed(ts, five=(int(76 + 9 * (ts - t0) / H), self.R))
            if ts >= self.t - 20 * M:
                v = pace.five_hour(self.st, ts, self.cfg)
                self.assertEqual(v["verdict"], "slow_down", ts - self.t)
                self.assertIsNotNone(v["cap_ts"], ts - self.t)

    def test_slow_steady_work_averages_out(self):
        # 4 %/h in whole percents: a step every 15 min. Renders every 20 s over
        # 2 h average within 10% of 4 %/h.
        self.known("s1")
        r = self.R  # the window opened 3 h ago
        t0 = self.t - 2 * H
        rates = []
        for k in range(0, 361):
            ts = t0 + 20 * k
            self.feed(ts, five=(int(20 + 4 * (ts - t0) / H + 0.5), r))
            if k >= 60:
                rates.append(pace.five_hour(self.st, ts, self.cfg)["rate"])
        self.assertAlmostEqual(sum(rates) / len(rates), 4.0, delta=0.4)

    def test_a_late_start_is_not_diluted_by_the_ramp_from_0(self):
        # Review #1: the window opened at 10:00, work began at 10:50 at 30 %/h.
        self.known("s1")
        start = self.t - 56 * M  # now is 10:56
        r = int(start + 5 * H)
        for i, lvl in enumerate((1, 2, 3, 4)):
            self.feed(start + (50 + 2 * i) * M, five=(lvl, r))
        v = self.view()
        self.assertAlmostEqual(v["rate"], 30.0, delta=1.0)
        self.assertEqual(v["rate_source"], "lsq")

    def test_short_rate_window_still_gives_a_rate(self):
        # Review #2: rate_window_min below rate_min_span_min used to give no rate.
        self.cfg["rate_window_min"] = 4
        self.known("s1")
        for i in range(11):
            self.feed(self.t - (10 - i) * M, five=(40 + i, self.R))
        v = self.view()
        self.assertAlmostEqual(v["rate"], 60.0, delta=1.0)

    def test_stale_first_reading_at_install_is_no_burst(self):
        # Review #3: at install every open session re-renders. An idle one shows
        # its 3-hour-old 30, a busy one 60 a minute later: 30 points did not go
        # in one minute.
        tt = self.t
        self.feed(tt - 2 * M, five=(30, self.R), sid="idle")
        self.feed(tt - 1 * M, five=(60, self.R), sid="busy")
        v = self.view()
        self.assertLess(v["rate"], 25.0)
        self.assertIsNone(v["cap_ts"])
        self.t = tt + 4 * M
        self.feed(self.t, five=(61, self.R), sid="busy")
        v = self.view()
        self.assertLess(v["rate"], 25.0)
        self.assertIsNone(v["cap_ts"])

    def test_target_below_100(self):
        self.cfg["target_5h"] = 80
        for i in range(21):
            self.feed(self.t - (20 - i) * M, five=(round(48 + 0.1 * i, 1), self.R))
        v = self.view()
        self.assertAlmostEqual(v["allowed"], 15.0)  # (80 - 50) / 2
        self.assertAlmostEqual(v["multiplier"], 2.5)


if __name__ == "__main__":
    unittest.main()
