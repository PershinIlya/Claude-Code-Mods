import unittest

from ctempo import commands, ingest, pace
from ctempo.config import FIVE_HOUR, SEVEN_DAY
from ctempo.store import ANOMALY, FIRST, FRESH, HEARTBEAT

from .helpers import MON, H, D, TempHome


class Sanitize(unittest.TestCase):
    now = MON + 10 * H

    def s(self, w, used, reset):
        return ingest.sanitize(w, used, reset, self.now)

    def test_epoch_leak_in_used_percentage_is_rejected(self):
        # ccpool #52326: used_percentage sometimes carries the resets_at epoch.
        self.assertEqual(self.s(SEVEN_DAY, 1783720704, self.now + D), (None, "pct_out_of_range"))
        self.assertEqual(self.s(SEVEN_DAY, "1783720704", self.now + D), (None, "pct_out_of_range"))

    def test_small_overshoot_is_clamped(self):
        self.assertEqual(self.s(FIVE_HOUR, 100.4, self.now + H)[0][0], 100.0)

    def test_negative_bool_nan_rejected(self):
        self.assertEqual(self.s(FIVE_HOUR, -1, self.now + H)[1], "pct_out_of_range")
        self.assertEqual(self.s(FIVE_HOUR, True, self.now + H)[1], "pct_missing")
        self.assertEqual(self.s(FIVE_HOUR, float("nan"), self.now + H)[1], "pct_missing")
        self.assertEqual(self.s(FIVE_HOUR, None, self.now + H)[1], "pct_missing")

    def test_reset_units_and_formats(self):
        r = int(self.now + 2 * H)
        self.assertEqual(self.s(FIVE_HOUR, 10, r * 1000)[0], (10.0, r))  # milliseconds
        self.assertEqual(self.s(FIVE_HOUR, 10, str(r))[0], (10.0, r))
        self.assertEqual(self.s(FIVE_HOUR, 10, "2026-10-12T12:00:00Z")[0], (10.0, int(MON + 12 * H)))

    def test_bogus_resets(self):
        self.assertEqual(self.s(FIVE_HOUR, 10, self.now - 60)[1], "expired")
        self.assertEqual(self.s(FIVE_HOUR, 10, self.now + 6 * H)[1], "reset_too_far")
        self.assertEqual(self.s(SEVEN_DAY, 10, self.now + 8 * D)[1], "reset_too_far")
        self.assertEqual(self.s(SEVEN_DAY, 10, 9999999999)[1], "reset_too_far")
        self.assertEqual(self.s(SEVEN_DAY, 10, 0)[1], "reset_bad_epoch")
        self.assertEqual(self.s(SEVEN_DAY, 10, "soon")[1], "reset_missing")

    def test_parse_payload_missing_windows_and_kinds(self):
        r, rej, kinds = ingest.parse_payload({"rate_limits": {"seven_day": {"used_percentage": 5, "resets_at": self.now + D}, "spend_limit": {}}}, self.now)
        self.assertEqual(list(r), [SEVEN_DAY])
        self.assertIn("spend_limit", kinds)
        self.assertEqual(ingest.parse_payload({}, self.now), ({}, {}, []))
        self.assertEqual(ingest.parse_payload([1, 2], self.now), ({}, {}, []))
        self.assertEqual(ingest.parse_payload({"rate_limits": None}, self.now)[0], {})


class Record(TempHome):
    def setUp(self):
        super().setUp()
        self.R5 = int(MON + 5 * H)
        self.R7 = int(MON + 7 * D)

    def test_first_reading_opens_window(self):
        self.feed(MON + H, five=(10, self.R5))
        w = self.windows(FIVE_HOUR)
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["start_ts"], self.R5 - 5 * H)
        self.assertEqual(self.rows(FIVE_HOUR)[0][3], FIRST)  # a first-seen session: a level, untimed

    def test_dedupe_and_heartbeat_throttle(self):
        t = MON + H
        self.feed(t, five=(10, self.R5))
        self.feed(t + 10, five=(10, self.R5))  # same session, same reading: heartbeat, throttled
        self.assertEqual(len(self.rows(FIVE_HOUR)), 1)
        self.feed(t + 70, five=(10, self.R5))  # 60 s later: one heartbeat row
        rows = self.rows(FIVE_HOUR)
        self.assertEqual([r[3] for r in rows], [FIRST, HEARTBEAT])

    def test_two_sessions_same_value_stored_once(self):
        t = MON + H
        self.feed(t, five=(10, self.R5), sid="a")
        self.feed(t + 5, five=(10, self.R5), sid="b")  # fresh for b, but identical to the last row
        self.assertEqual(len(self.rows(FIVE_HOUR)), 1)

    def test_parallel_sessions_stale_reader_never_moves_the_series(self):
        t = MON + H
        self.feed(t, five=(20, self.R5), sid="idle")
        for i in range(1, 11):
            self.feed(t + i * 60, five=(20 + i * 3, self.R5), sid="busy")
            # The idle session keeps re-reporting its old 20%: 13+ points below.
            self.feed(t + i * 60 + 30, five=(20, self.R5), sid="idle")
        w = self.windows(FIVE_HOUR)
        self.assertEqual(len(w), 1, "a stale session must not look like a reset")
        self.assertEqual(w[0]["max_used"], 50)
        self.assertFalse([r for r in self.rows(FIVE_HOUR) if r[3] == ANOMALY])

    def test_resets_at_jitter_is_one_window(self):
        t = MON + H
        self.feed(t, five=(10, self.R5))
        self.feed(t + 60, five=(11, self.R5 + 3))
        self.feed(t + 120, five=(12, self.R5 - 2))
        self.assertEqual(len(self.windows(FIVE_HOUR)), 1)

    def test_next_window_after_expiry_closes_the_old_one(self):
        self.feed(MON + H, five=(30, self.R5))
        self.feed(MON + 4 * H, five=(70, self.R5))
        self.feed(MON + 6 * H, five=(2, int(MON + 11 * H)))
        w = self.windows(FIVE_HOUR)
        self.assertEqual(len(w), 2)
        self.assertEqual(w[0]["close_reason"], "expired")
        self.assertEqual(w[0]["unused_at_close"], 30)
        self.assertEqual(w[0]["closed_at"], self.R5)
        self.assertIsNone(w[1]["closed_at"])

    def test_resets_at_advancing_before_expiry_rolls_over(self):
        self.feed(MON + H, five=(30, self.R5))
        self.feed(MON + 2 * H, five=(1, int(MON + 7 * H)))  # an early reset
        w = self.windows(FIVE_HOUR)
        self.assertEqual([i["close_reason"] for i in w], ["rollover", None])
        self.assertEqual(w[0]["closed_at"], MON + 2 * H)
        self.assertEqual(w[0]["unused_at_close"], 70)

    def test_expired_window_closes_by_itself(self):
        self.feed(MON + H, five=(30, self.R5), week=(5, self.R7))
        self.feed(MON + 6 * H, week=(6, self.R7))  # 5h window gone from the payload
        w = self.windows(FIVE_HOUR)
        self.assertEqual(w[0]["close_reason"], "expired")
        self.assertEqual(w[0]["unused_at_close"], 70)

    def test_sustained_drop_is_a_reset_single_dip_is_not(self):
        t = MON + D
        self.feed(t, week=(60, self.R7), sid="a")
        self.feed(t + 60, week=(40, self.R7), sid="a")  # one dip of 20: pending
        self.assertEqual(len(self.windows(SEVEN_DAY)), 1)
        self.feed(t + 120, week=(61, self.R7), sid="a")  # back up: the dip was noise
        self.assertEqual(len(self.windows(SEVEN_DAY)), 1)
        self.assertEqual(self.windows(SEVEN_DAY)[0]["max_used"], 61)
        self.feed(t + 180, week=(1, self.R7), sid="a")
        self.feed(t + 240, week=(2, self.R7), sid="a")  # second consecutive low: reset
        w = self.windows(SEVEN_DAY)
        self.assertEqual(len(w), 2)
        self.assertEqual(w[0]["close_reason"], "drop")
        self.assertEqual(w[0]["max_used"], 61)
        self.assertEqual(w[1]["max_used"], 2)
        self.assertEqual(w[1]["start_ts"], t + 180)

    def test_small_drop_is_not_anomalous(self):
        t = MON + D
        self.feed(t, week=(60, self.R7), sid="a")
        self.feed(t + 60, week=(50, self.R7), sid="b")
        self.feed(t + 120, week=(49, self.R7), sid="b")
        self.assertEqual(len(self.windows(SEVEN_DAY)), 1)
        self.assertEqual(self.windows(SEVEN_DAY)[0]["max_used"], 60)

    def test_heartbeat_after_drop_reset_cannot_raise_new_window(self):
        t = MON + D
        self.feed(t, week=(80, self.R7), sid="idle")
        self.feed(t + 10, week=(80, self.R7), sid="a")
        self.feed(t + 60, week=(1, self.R7), sid="a")
        self.feed(t + 120, week=(2, self.R7), sid="a")  # reset by drop
        self.feed(t + 200, week=(80, self.R7), sid="idle")  # stale, pre-reset value
        w = self.windows(SEVEN_DAY)
        self.assertEqual(w[-1]["max_used"], 2)

    def test_bogus_later_reset_then_real_one_reopens(self):
        t = MON + H
        self.feed(t, five=(10, self.R5), sid="a")
        bogus = int(t + 5 * H)  # plausible range, but a different window
        self.feed(t + 60, five=(12, bogus), sid="a")
        self.assertEqual(len(self.windows(FIVE_HOUR)), 2)
        self.feed(t + 120, five=(13, self.R5), sid="a")
        self.feed(t + 180, five=(14, self.R5), sid="a")
        w = self.windows(FIVE_HOUR)
        opened = [i for i in w if i["closed_at"] is None]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["reset_ts"], self.R5)
        self.assertEqual(opened[0]["max_used"], 14)

    def test_heartbeat_with_later_reset_does_not_redo_a_rollover(self):
        t = MON + H
        self.feed(t, five=(10, self.R5), sid="a")
        bogus = int(t + 5 * H)
        self.feed(t + 60, five=(12, bogus), sid="b")  # b's bogus later reset rolls over
        self.feed(t + 120, five=(13, self.R5), sid="a")
        self.feed(t + 180, five=(14, self.R5), sid="a")  # sustained: the real window is reopened
        for k in range(5):  # b keeps re-rendering its old (bogus) reading
            self.assertEqual(self.feed(t + 240 + k * 61, five=(12, bogus), sid="b")[FIVE_HOUR], "stale")
        opened = [i for i in self.windows(FIVE_HOUR) if i["closed_at"] is None]
        self.assertEqual([i["reset_ts"] for i in opened], [self.R5])

    def test_bogus_later_reset_is_undone_by_one_reading_from_its_session(self):
        # Review 7 (integration#1): one bogus later resets_at rolled the window
        # over at once, but undoing it took two fresh readings with the real
        # one. A maxed (or idle) session repeats its real reading only as
        # heartbeats, so the second never came: the line kept the bogus reset
        # time past the real reset, and `report` counted the window twice.
        for i in range(10):  # session a works the 00:00-05:00 window up to 100%
            self.feed(MON + i * 600, five=(10 * (i + 1), self.R5), sid="a")
        t = MON + 2 * H
        bogus = self.R5 + int(H)
        self.assertEqual(self.feed(t, five=(100, bogus), sid="a")[FIVE_HOUR], "rollover")
        self.assertEqual(self.feed(t + 60, five=(100, self.R5), sid="a")[FIVE_HOUR], "reset_reopen")
        for k in range(5):  # maxed: the same reading again and again
            self.feed(t + 120 + k * 61, five=(100, self.R5), sid="a")
        opened = [i for i in self.windows(FIVE_HOUR) if i["closed_at"] is None]
        self.assertEqual([(i["reset_ts"], i["max_used"]) for i in opened], [(self.R5, 100)])
        self.assertEqual(pace.five_hour(self.st, t + 600, self.cfg)["reset_ts"], self.R5)
        self.feed(self.R5 + 600, five=(3, self.R5 + int(5 * H)), sid="a")  # the next window
        ended = [i for i in self.windows(FIVE_HOUR) if i["close_reason"] in ("expired", "rollover")]
        self.assertEqual([(i["reset_ts"], i["close_reason"]) for i in ended], [(self.R5, "expired")])
        self.assertEqual([i["close_reason"] for i in self.windows(FIVE_HOUR) if i["reset_ts"] == bogus], ["shift"])
        # Undoing a later resets_at that another session reported still needs
        # drop_samples: this one may lag a real early reset.
        r = self.R5 + int(5 * H)
        self.assertEqual(self.feed(self.R5 + 700, five=(5, r + 600), sid="b")[FIVE_HOUR], "rollover")
        self.assertEqual(self.feed(self.R5 + 800, five=(6, r), sid="a")[FIVE_HOUR], "anomaly")
        self.assertEqual(self.feed(self.R5 + 900, five=(7, r), sid="a")[FIVE_HOUR], "reset_reopen")

    def test_a_maxed_session_undoes_a_bogus_reset_that_begins_after_its_last_reading(self):
        # Review 10: a maxed session reads no fresh value after its 100, so a
        # bogus later resets_at can begin after its last fresh reading. That
        # closed the real window as a rollover, which the one-reading undo no
        # longer matched: the line kept the bogus reset time again.
        for i in range(10):  # session a maxes the 00:00-05:00 window by 00:45
            self.feed(MON + i * 300, five=(10 * (i + 1), self.R5), sid="a")
        t = MON + 2 * H
        self.assertEqual(self.feed(t, five=(100, self.R5 + int(H)), sid="a")[FIVE_HOUR], "rollover")
        self.assertEqual(self.feed(t + 60, five=(100, self.R5), sid="a")[FIVE_HOUR], "reset_reopen")
        self.assertEqual(pace.five_hour(self.st, t + 120, self.cfg)["reset_ts"], self.R5)

    def test_a_bogus_reset_a_whole_window_from_now_is_undone_by_one_reading(self):
        # Review 10: the shape of bogus reset this suite uses elsewhere (a
        # plausible window, starting now) always begins after the last reading.
        for i in range(10):
            self.feed(MON + i * 600, five=(10 * (i + 1), self.R5), sid="a")
        t = MON + 2 * H
        self.assertEqual(self.feed(t, five=(100, int(t + 5 * H)), sid="a")[FIVE_HOUR], "rollover")
        self.assertEqual(self.feed(t + 60, five=(100, self.R5), sid="a")[FIVE_HOUR], "reset_reopen")

    def test_a_shared_session_id_cannot_undo_a_real_early_reset(self):
        # Review 8 (delta-core#0): two terminals on one conversation (`claude
        # -c` without --fork-session) share a session id. One sees a real
        # early reset; the other, idle, re-renders the old window's 90% every
        # minute. That counted as the opener undoing a bogus later reset: the
        # line flipped back to the dead window on every render pair.
        t = MON + H
        self.feed(t, five=(80, self.R5), sid="a")
        self.feed(t + 60, five=(90, self.R5), sid="a")
        r2 = int(t + 120 + 5 * H)
        self.assertEqual(self.feed(t + 120, five=(1, r2), sid="a")[FIVE_HOUR], "rollover")
        for k in range(4):
            self.assertEqual(self.feed(t + 150 + k * 60, five=(90, self.R5), sid="a")[FIVE_HOUR], "anomaly")
            self.assertEqual(self.feed(t + 180 + k * 60, five=(2 + k, r2), sid="a")[FIVE_HOUR], "fresh")
        w = self.windows(FIVE_HOUR)
        self.assertEqual([(i["reset_ts"], i["close_reason"]) for i in w], [(self.R5, "rollover"), (r2, None)])
        self.assertEqual(w[1]["max_used"], 5)
        self.assertEqual(pace.five_hour(self.st, t + 400, self.cfg)["reset_ts"], r2)

    def test_undo_by_its_session_keeps_another_sessions_pending_reading(self):
        # Review 8 (delta-core#1): after a's bogus later reset, b's real 96 is
        # pending as an anomaly; a's real 94 then undoes the rollover on its
        # own, but b's 96 stayed behind in the superseded instance, and the
        # reopened window showed 94 until the next fresh reading.
        self.feed(MON + H, five=(50, self.R5), sid="a")
        self.feed(MON + H + 600, five=(80, self.R5), sid="a")
        self.feed(MON + H + 900, five=(88, self.R5), sid="b")
        t = MON + 2 * H
        self.assertEqual(self.feed(t, five=(90, self.R5 + int(H)), sid="a")[FIVE_HOUR], "rollover")
        self.assertEqual(self.feed(t + 30, five=(96, self.R5), sid="b")[FIVE_HOUR], "anomaly")
        self.assertEqual(self.feed(t + 60, five=(94, self.R5), sid="a")[FIVE_HOUR], "reset_reopen")
        opened = [i for i in self.windows(FIVE_HOUR) if i["closed_at"] is None]
        self.assertEqual([(i["reset_ts"], i["max_used"]) for i in opened], [(self.R5, 96)])
        b96 = [r for r in self.rows(FIVE_HOUR) if r[5] == "b" and r[1] == 96]
        self.assertEqual([(r[3], r[4]) for r in b96], [(FRESH, opened[0]["id"])])

    def test_a_reset_moved_later_at_the_same_level_is_a_shift(self):
        # Review 8 (long-horizon#3): the weekly resets_at moved 1 h later in
        # the middle of the window while the level kept rising. Any later
        # resets_at closed the open window as a rollover, so `report` listed
        # the running week as finished with 55 phantom unused points.
        t = MON + 2 * D
        for k in range(12):
            self.feed(t + k * 1800, week=(30 + k, self.R7))
        self.assertEqual(self.feed(t + 12 * 1800, week=(42, self.R7 + int(H)))[SEVEN_DAY], "rollover")
        for k in range(13, 20):
            self.feed(t + k * 1800, week=(30 + k, self.R7 + int(H)))
        w = self.windows(SEVEN_DAY)
        self.assertEqual([(i["reset_ts"], i["close_reason"]) for i in w], [(self.R7, "shift"), (self.R7 + H, None)])
        self.assertEqual(w[1]["max_used"], 49)
        # A real early reset (the level drops) still rolls over.
        self.feed(t + D, week=(3, self.R7 + int(2 * H)))
        self.assertEqual([i["close_reason"] for i in self.windows(SEVEN_DAY)], ["shift", "rollover", None])

    def test_a_light_weeks_early_reset_is_a_rollover_at_about_the_same_level(self):
        # Review 9 (delta-ingest-texts#1): a moved reset was judged on the
        # level alone. A light week peaked at 10% on Tuesday, was reset early
        # on Wednesday 12:00, and the new week's first reading came on
        # Thursday at 6% (spent on claude.ai first): over half the old peak,
        # so the finished week closed as a shift and `report` left it out.
        # As in learning (D-13), a window that began after the last reading
        # is a real reset, whatever its level.
        for k in range(10):  # Monday 00:00 to Tuesday 12:00
            self.feed(MON + k * 4 * H, week=(1 + k, self.R7), sid="a")
        r2 = int(MON + 2 * D + 12 * H + 7 * D)
        self.assertEqual(self.feed(MON + 3 * D + 10 * H, week=(6, r2), sid="a")[SEVEN_DAY], "rollover")
        w = self.windows(SEVEN_DAY)
        self.assertEqual([(i["reset_ts"], i["close_reason"]) for i in w], [(self.R7, "rollover"), (r2, None)])
        self.assertEqual([i["reset_ts"] for i in commands._closed(self.st, SEVEN_DAY, MON)], [self.R7])

    def test_a_shared_session_id_cannot_undo_a_real_early_reset_from_a_low_level(self):
        # Review 9 (delta-ingest-texts#0): the drop_points guard above only
        # held while the stale level sat 15 points over the new window. A
        # weekly window at 10% reset early: every stale render of the old 10%
        # reopened the dead week and the next new one rolled it over again,
        # a window row per render pair. A rollover (the level dropped) now
        # takes drop_samples to undo.
        t = MON + 2 * D
        for k in range(5):
            self.feed(t + k * 1800, week=(6 + k, self.R7), sid="a")
        t += 3 * H
        r2 = int(t + 7 * D)
        self.assertEqual(self.feed(t, week=(0, r2), sid="a")[SEVEN_DAY], "rollover")
        for k in range(10):
            self.assertEqual(self.feed(t + 60 + k * 120, week=(10, self.R7), sid="a")[SEVEN_DAY], "anomaly")
            self.assertEqual(self.feed(t + 120 + k * 120, week=(k // 3, r2), sid="a")[SEVEN_DAY], "fresh")
        w = self.windows(SEVEN_DAY)
        self.assertEqual([(i["reset_ts"], i["close_reason"]) for i in w], [(self.R7, "rollover"), (r2, None)])
        self.assertEqual(w[1]["max_used"], 3)

    def test_a_shared_session_id_cannot_undo_a_real_early_reset_as_the_new_window_climbs(self):
        # Review 9 (delta-ingest-texts#0): the setup of the shared session id
        # test above, continued. Once the new window reached 76%, the stale
        # 90 was less than drop_points above it, and the line flipped back to
        # the dead window on every render pair.
        t = MON + H
        self.feed(t, five=(80, self.R5), sid="a")
        self.feed(t + 60, five=(90, self.R5), sid="a")
        r2 = int(t + 120 + 5 * H)
        self.assertEqual(self.feed(t + 120, five=(1, r2), sid="a")[FIVE_HOUR], "rollover")
        for k in range(1, 18):
            self.assertEqual(self.feed(t + 120 + k * 60, five=(5 * k, r2), sid="a")[FIVE_HOUR], "fresh")
            self.assertEqual(self.feed(t + 150 + k * 60, five=(90, self.R5), sid="a")[FIVE_HOUR], "anomaly")
        w = self.windows(FIVE_HOUR)
        self.assertEqual([(i["reset_ts"], i["close_reason"]) for i in w], [(self.R5, "rollover"), (r2, None)])
        self.assertEqual(w[1]["max_used"], 85)
        self.assertEqual(pace.five_hour(self.st, t + 1200, self.cfg)["reset_ts"], r2)

    def test_a_shared_session_id_cannot_undo_a_reset_moved_at_the_same_level(self):
        # Review 9 (delta-ingest-texts#0): the weekly resets_at moved 1 h
        # later at 42% (a shift) while a twin terminal on the same session id
        # kept re-rendering the old reset at 41%. Each of its renders undid
        # the move and the next new one redid it; once the old reset passed,
        # `report` listed the running week as finished. A session's real
        # reading is never below its own reading of the moved reset.
        t = MON + 2 * D
        for k in range(12):
            self.feed(t + k * 1800, week=(30 + k, self.R7), sid="a")
        t += 12 * 1800
        moved = self.R7 + int(H)
        self.assertEqual(self.feed(t, week=(42, moved), sid="a")[SEVEN_DAY], "rollover")
        for k in range(10):
            self.assertEqual(self.feed(t + 30 + k * 60, week=(41, self.R7), sid="a")[SEVEN_DAY], "anomaly")
            self.assertEqual(self.feed(t + 60 + k * 60, week=(42 + k // 4, moved), sid="a")[SEVEN_DAY], "fresh")
        self.feed(self.R7 + 600, week=(50, moved), sid="a")
        w = self.windows(SEVEN_DAY)
        self.assertEqual([(i["reset_ts"], i["close_reason"]) for i in w], [(self.R7, "shift"), (moved, None)])
        self.assertEqual(commands._closed(self.st, SEVEN_DAY, MON), [])

    def test_first_words_of_unknown_sessions_cannot_fake_a_reset(self):
        # ctempo is installed while three sessions are open: every one
        # re-renders at once; two idle ones carry an old, much lower value.
        t = MON + D
        self.feed(t, week=(60, self.R7), sid="busy")
        self.assertEqual(self.feed(t + 1, week=(30, self.R7), sid="idle1")[SEVEN_DAY], "stale")
        self.assertEqual(self.feed(t + 2, week=(31, self.R7), sid="idle2")[SEVEN_DAY], "stale")
        self.assertEqual(len(self.windows(SEVEN_DAY)), 1)
        self.assertEqual(self.windows(SEVEN_DAY)[0]["max_used"], 60)

    def test_drop_of_exactly_drop_points_is_an_anomaly(self):
        t = MON + D
        self.feed(t, week=(60, self.R7), sid="a")
        self.assertEqual(self.feed(t + 60, week=(45, self.R7), sid="a")[SEVEN_DAY], "anomaly")

    def test_no_grace_at_expiry_no_duplicate_window(self):
        self.feed(MON + H, five=(30, self.R5))
        acts = self.feed(self.R5 + 2, five=(30, self.R5))  # 2 s after its own reset
        self.assertEqual(acts[FIVE_HOUR], "rejected:expired")
        self.assertEqual(len(self.windows(FIVE_HOUR)), 1)

    def test_wobbled_reset_of_an_expired_window_is_not_a_new_window(self):
        self.feed(MON + H, five=(70, self.R5), sid="a")
        self.feed(MON + 2 * H, five=(40, self.R5 + 38), sid="b")  # b's reading wobbled 38 s later
        acts = self.feed(self.R5 + 10, five=(40, self.R5 + 38), sid="b")  # b re-renders after the reset
        self.assertEqual(acts[FIVE_HOUR], "stale")
        acts = self.feed(self.R5 + 20, five=(41, self.R5 + 38), sid="c")  # a fresh one too
        self.assertEqual(acts[FIVE_HOUR], "expired")
        self.assertEqual(len(self.windows(FIVE_HOUR)), 1)
        self.assertEqual(self.windows(FIVE_HOUR)[0]["max_used"], 70)

    def test_heartbeats_do_not_move_last_seen(self):
        t = MON + H
        self.feed(t, five=(10, self.R5))
        self.feed(t + 3600, five=(10, self.R5))
        self.assertEqual(self.windows(FIVE_HOUR)[0]["last_seen"], t)

    def test_missing_windows_and_rejects_are_counted(self):
        self.feed(MON + H, week=(5, self.R7))
        self.assertEqual(self.rows(FIVE_HOUR), [])
        self.feed(MON + H + 60, five=(1783720704, self.R5), week=(5, self.R7))
        self.assertEqual(self.st.meta_get("rejected:five_hour:pct_out_of_range"), 1)

    def test_kinds_logged(self):
        readings, rejects, kinds = ingest.parse_payload(
            {"rate_limits": {"spend_limit": {"used_percentage": 3, "resets_at": MON + D}}, "kinds": ["five_hour", "weird_kind"]}, MON
        )
        ingest.record(self.st, readings, MON, "mod", "x", self.cfg, rejects, kinds)
        self.assertEqual(sorted(k[0] for k in self.st.kinds()), ["five_hour", "spend_limit", "weird_kind"])

    def test_retention_prunes_old_samples(self):
        self.feed(MON - 400 * D, five=(5, int(MON - 400 * D + 3 * H)))
        self.st.meta_set("last_prune", 0)
        self.feed(MON + H, five=(5, self.R5))
        self.assertEqual(len(self.rows(FIVE_HOUR)), 1)


if __name__ == "__main__":
    unittest.main()
