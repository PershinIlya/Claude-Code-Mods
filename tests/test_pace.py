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
        self.feed(self.t - 10 * M, five=(40, self.R))
        self.feed(self.t - 5 * M, five=(40, self.R), sid="s2")  # same value, other session: no new row
        self.st.add_sample(self.t - 5 * M, FIVE_HOUR, 40, self.R, "test", "s3", 1, 1)  # heartbeat tick
        self.feed(self.t, five=(50, self.R))
        v = self.view()
        # Points (0, 40), (5 min, 40), (10 min, 50): 60 %/h (see Slope).
        self.assertAlmostEqual(v["rate"], 60.0, places=6)
        self.assertAlmostEqual(v["multiplier"], 25.0 / 60.0)
        self.assertEqual(v["verdict"], "slow_down")
        self.assertAlmostEqual(v["projected_raw"], 170.0)
        self.assertEqual(v["projected"], 100.0)
        self.assertAlmostEqual(v["cap_ts"], self.t + 50 * M)  # (100 - 50) / 60 h
        self.assertEqual(v["severity"], "alert")

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

    def test_too_few_points_falls_back_to_window_average(self):
        r = int(self.t + 3 * H)  # window started 2 h ago
        self.feed(self.t, five=(30, r))
        v = self.view()
        self.assertEqual(v["rate_source"], "window_avg")
        self.assertAlmostEqual(v["rate"], 15.0)  # 30 points over 2 h
        self.assertAlmostEqual(v["allowed"], 70.0 / 3.0)
        self.assertAlmostEqual(v["projected"], 75.0)

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

    def test_target_below_100(self):
        self.cfg["target_5h"] = 80
        for i in range(21):
            self.feed(self.t - (20 - i) * M, five=(round(48 + 0.1 * i, 1), self.R))
        v = self.view()
        self.assertAlmostEqual(v["allowed"], 15.0)  # (80 - 50) / 2
        self.assertAlmostEqual(v["multiplier"], 2.5)


if __name__ == "__main__":
    unittest.main()
