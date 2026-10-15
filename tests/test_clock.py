"""Local time goes through ctempo.clock, so the tests pin the zone on every
OS: time.tzset does not exist on Windows, and the process may run in any
zone. helpers.utc() pins UTC, helpers.zone() an IANA zone."""

import os
import unittest

from ctempo import clock, profile, render, transcripts

from .helpers import MON, H, D, TZ_UTC, utc, zone


class Clock(unittest.TestCase):
    def setUp(self):
        utc()

    def test_the_suite_runs_in_utc_whatever_the_machines_zone(self):
        # MON is Monday 2026-10-12 00:00 UTC.
        self.assertEqual(render.hm(MON), "00:00")
        self.assertEqual(render.dhm(MON + 20 * H), "Mon 20:00")
        self.assertEqual(profile.bin_of(MON + 20 * H), 20)
        self.assertEqual(clock.strftime("%Y-%m-%d", MON), "2026-10-12")
        self.assertEqual(os.environ.get("TZ"), TZ_UTC)  # what child processes start in

    def test_a_zone_moves_the_reset_clock_the_bins_and_the_days(self):
        events = [(MON + 18 * H, 1.0), (MON + 19 * H, 1.0)]  # 23:30 and 00:30 in India
        self.assertEqual(transcripts.build_prior(events, MON - D, MON + 2 * D, 7)[1]["active_days"], 1)
        with zone("Asia/Kolkata"):  # UTC+05:30
            self.assertEqual(render.hm(MON), "05:30")
            self.assertEqual(render.dhm(MON + 20 * H), "Tue 01:30")
            self.assertEqual(profile.bin_of(MON + 20 * H), 24 + 1)
            self.assertEqual(clock.strftime("%Y-%m-%d", MON + 20 * H), "2026-10-13")
            self.assertEqual(transcripts.build_prior(events, MON - D, MON + 2 * D, 7)[1]["active_days"], 2)
        self.assertEqual(render.hm(MON), "00:00")  # and back in UTC after the block


if __name__ == "__main__":
    unittest.main()
