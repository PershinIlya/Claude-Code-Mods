"""The band: bars, words and the terminal line (ctempo/band.py)."""

import json
import os
import unittest

from ctempo import band, config, render, statusline

from .helpers import MON, H, D, TempHome, payload, utc

NOW = MON + 14 * H + 40 * 60  # Monday 14:40 UTC
CELLS = set("━╍─┈")


def v5(**kw):
    v = dict(window=config.FIVE_HOUR, present=True, used=55.3, reset_ts=int(MON + 16 * H + 30 * 60),
             verdict="on_pace", multiplier=1.05, projected=95.0, cap_ts=None, allowed=18.0, rate=17.0,
             rate_source="lsq", severity="dim", target=100.0, unused_at_reset=5.0, known_ts=NOW - 60,
             last_seen=NOW - 60)
    v.update(kw)
    return v


def vw(**kw):
    v = dict(window=config.SEVEN_DAY, present=True, used=48.0, reset_ts=int(MON + 4 * D + 4 * H),
             verdict="on_pace", multiplier=1.05, projected=96.0, dry_ts=None, delta=1.0, severity="dim",
             target=100.0, expire=4.0, intensity_source="recent", last_seen=NOW - 300)
    v.update(kw)
    return v


STATES = {
    "on pace": (v5(), vw()),
    "early": (v5(used=8, rate_source="interpolated", verdict="speed_up", multiplier=1.8, projected=58),
              vw(used=2, verdict="speed_up", multiplier=2.7, projected=39)),
    "runs out": (v5(used=70, verdict="slow_down", multiplier=0.5, cap_ts=MON + 15 * H + 40 * 60, projected=100,
                    severity="alert"), vw()),
    "maxed": (v5(used=100, verdict="maxed", severity="alert", projected=100), vw(used=52)),
    "idle": (v5(used=64, verdict="idle", rate=0.0, projected=64), vw(used=35)),
    "unused": (v5(used=12, verdict="speed_up", multiplier=1.7, projected=40),
               vw(used=40, projected=60, severity="opportunity", verdict="speed_up", multiplier=2.5, expire=40.0)),
    "week dry": (v5(), vw(used=80, verdict="slow_down", multiplier=0.4, dry_ts=MON + 2 * D + 11 * H,
                          projected=100, severity="alert")),
    "half": (v5(), {"present": False}),
}


def words(p):
    return None if p is None else p["pre"] + p["num"] + p["post"]


class Words(unittest.TestCase):
    def setUp(self):
        utc()

    def test_each_state_reads_as_designed(self):
        want = {
            "on pace": (("run", None), ("run", None)),
            "early": (("rough", "Can go about 2x faster"), ("run", "Can go 2.7x faster")),
            "runs out": (("dry", "Runs out 15:40, go 2x slower"), ("run", None)),
            "maxed": (("maxed", "Back at 16:30"), ("run", None)),
            "idle": (("idle", "Idle"), ("run", None)),
            "unused": (("run", "Can go 1.7x faster"), ("expire", "40% will go unused")),
            "week dry": (("run", None), ("dry", "Runs out Wed 11:00, go 2.5x slower")),
            "half": (("run", None), ("wait", None)),
        }
        for name, (a, b) in STATES.items():
            got = tuple((w["kind"], words(w["phrase"])) for w in band.windows(a, b, NOW))
            self.assertEqual(got, want[name], name)

    def test_attention_tones_and_the_narrow_wording(self):
        h5, wk = band.windows(*STATES["unused"], NOW)
        self.assertEqual((wk["phrase"]["tone"], wk["phrase"]["attention"], wk["phrase"]["narrow"]),
                         ("success", True, " unused"))
        self.assertEqual((h5["phrase"]["tone"], h5["phrase"]["attention"], h5["phrase"]["calm"]),
                         ("inactive", False, True))
        h5, _ = band.windows(*STATES["runs out"], NOW)
        p = h5["phrase"]
        self.assertEqual((p["tone"], p["num"], p["narrow_pre"] + p["num"] + p["narrow"]), ("error", "15:40", "Out 15:40"))
        # On pace but running out (a milder overshoot), or an early estimate: may.
        soft = band.phrase(v5(verdict="on_pace", cap_ts=MON + 16 * H, severity="warn"), NOW)
        self.assertEqual((words(soft), soft["tone"]), ("May run out 16:00", "warning"))  # 1.05x: no "slower"
        p = band.phrase(v5(verdict="slow_down", severity="warn", multiplier=0.7), NOW)
        self.assertEqual((words(p), p["narrow_pre"] + p["num"] + p["narrow"], p["tone"], p["attention"]),
                         ("Go 1.4x slower", "1.4x slower", "warning", True))
        self.assertEqual(words(band.phrase(v5(verdict="slow_down", multiplier=None), NOW)), "Ease off")
        self.assertIsNone(band.phrase(v5(verdict="unknown", multiplier=None, projected=None), NOW))

    def test_multipliers(self):
        cases = [(1.3, "lsq", "Can go 1.3x faster", "1.3x faster"), (2.0, "lsq", "Can go 2x faster", "2x faster"),
                 (12, "lsq", "Plenty of room", "Plenty of room"),
                 (1.2, "interpolated", "Can go a bit faster", "A bit faster"),
                 (1.3, "interpolated", "Can go about 1.5x faster", "~1.5x faster"),
                 (2.3, "interpolated", "Can go about 2.5x faster", "~2.5x faster")]
        for m, source, want, narrow in cases:
            v = v5(verdict="speed_up", multiplier=m, rate_source=source)
            p = band.phrase(v, NOW)
            self.assertEqual((words(p), p["narrow_pre"] + p["num"] + p["narrow"]), (want, narrow), (m, source))
            self.assertIn(want.split(" faster")[0], band.sentence(v, NOW))

    def test_slow_down_multipliers(self):
        # How much slower: 1 / the multiplier; an early estimate in halves.
        cases = [(0.5, "lsq", "Go 2x slower", "2x slower"), (0.75, "lsq", "Go 1.3x slower", "1.3x slower"),
                 (0.05, "lsq", "Pause until the reset", "Pause"),
                 (0.6, "interpolated", "Go about 1.5x slower", "~1.5x slower"),
                 (0.8, "interpolated", "Ease off", "Ease off")]
        for m, source, want, narrow in cases:
            v = v5(verdict="slow_down", multiplier=m, rate_source=source, projected=110)
            p = band.phrase(v, NOW)
            self.assertEqual((words(p), p["narrow_pre"] + p["num"] + p["narrow"]), (want, narrow), (m, source))
            self.assertTrue(band.sentence(v, NOW).endswith(want + "."), band.sentence(v, NOW))

    def test_week_back_and_resets_name_the_day(self):
        v = vw(used=100, verdict="maxed", severity="alert")
        self.assertEqual(words(band.phrase(v, NOW)), "Back Fri 04:00")
        self.assertEqual(band.sentence(v, NOW), "Week: limit reached. Back Fri 04:00.")
        self.assertEqual(band.windows(v5(), vw(), NOW)[1]["reset"], "Fri")
        self.assertEqual(band.windows(v5(), vw(reset_ts=int(NOW + 5 * H)), NOW)[1]["reset"], "19:40")

    def test_sentences(self):
        h5, wk = band.windows(*STATES["runs out"], NOW)
        self.assertEqual(h5["sentence"], "5-hour: 70% used, resets 16:30. Runs out at 15:40, 50 min before the reset. "
                                         "Go 2x slower.")
        self.assertEqual(wk["sentence"], "Week: 48% used, resets Fri 04:00. On pace, about 96% by then.")
        _, wk = band.windows(*STATES["unused"], NOW)
        self.assertEqual(wk["sentence"], "Week: 40% used, resets Fri 04:00. About 60% by then: 40% will go unused. "
                                         "Use it.")
        h5, _ = band.windows(*STATES["idle"], NOW)
        self.assertEqual(h5["sentence"], "5-hour: 64% used, resets 16:30. Idle: up to 18% an hour is safe.")
        h5, wk = band.windows({"present": False}, {"present": False}, NOW)
        self.assertEqual((h5["sentence"], wk["pct"]), ("5-hour: waiting for the first reading.", None))
        h5, _ = band.windows({"present": False, "raw": (12.0, MON + 17 * H)}, {"present": False}, NOW)
        self.assertEqual((h5["pct"], h5["kind"], h5["sentence"]), (12, "run", "5-hour: 12% used, resets 17:00."))
        _, wk = band.windows(*STATES["week dry"], NOW)
        self.assertEqual(wk["sentence"], "Week: 80% used, resets Fri 04:00. Runs out Wed 11:00, 1 day 17 h before "
                                         "the reset. Go 2.5x slower.")

    def test_over_a_target_below_100_is_a_warning_even_when_idle(self):
        # Review 18: the classic line was yellow at or above a target below
        # 100 whatever the verdict; the band said a calm grey "Idle".
        for verdict in ("idle", "unknown"):
            v = v5(verdict=verdict, used=85, target=80.0, severity="warn", rate=0.0, projected=85, multiplier=None)
            p = band.phrase(v, NOW)
            self.assertEqual((words(p), p["tone"], p["attention"]), ("Over your 80% target", "warning", True), verdict)
            self.assertIn("Over your 80% target.", band.sentence(v, NOW))

    def test_nothing_green_for_less_than_a_point(self):
        # Review 18: with min_free 0 a leftover of 0.6 said "0% will go unused".
        v = vw(used=99.4, projected=99.4, expire=0.6, severity="opportunity", verdict="on_pace")
        self.assertEqual(band.bar_kind(v), "run")
        self.assertIsNone(band.phrase(v, NOW))
        self.assertNotIn("unused", band.sentence(v, NOW))

    def test_a_maxed_week_blocks_the_5_hours(self):
        # Review 18: the week at its limit, and the 5 hours still said "Can go 2.2x faster".
        h5, wk = band.windows(v5(verdict="speed_up", multiplier=2.2, used=30, projected=70, known_ts=NOW - 100,
                                 rate=40),
                              vw(used=100, verdict="maxed", severity="alert", projected=100), NOW)
        self.assertEqual((h5["kind"], h5["projected"], h5["phrase"], h5["pct"]), ("idle", None, None, 30))
        self.assertEqual(h5["sentence"], "5-hour: 30% used, resets 16:30. Blocked by the weekly limit until Fri 04:00.")
        self.assertEqual(words(wk["phrase"]), "Back Fri 04:00")

    def test_a_run_out_close_to_the_reset_is_on_pace(self):
        # The person's first day: week 17%, the window average stretched over
        # the week ran out 50 min before Friday's reset, and the band said "Out".
        reset = int(MON + 4 * D + 4 * H)
        v = vw(used=17, verdict="on_pace", multiplier=0.98, projected=100, dry_ts=reset - 50 * 60,
               severity="warn", intensity_source="window", E=0.83)
        self.assertIsNone(band.phrase(v, NOW))
        self.assertEqual(band.bar_kind(v), "rough")
        self.assertIn("On pace", band.sentence(v, NOW))
        h5 = v5(used=80, verdict="on_pace", cap_ts=int(MON + 16 * H + 25 * 60), severity="warn")  # 5 min early
        self.assertIsNone(band.phrase(h5, NOW))
        h5 = v5(used=80, verdict="on_pace", cap_ts=int(MON + 16 * H), severity="warn")  # 30 min early
        self.assertEqual(words(band.phrase(h5, NOW)), "May run out 16:00")

    def test_early_in_the_week_the_window_average_is_rough(self):
        early = vw(used=17, verdict="slow_down", multiplier=0.7, projected=100, dry_ts=MON + 2 * D,
                   severity="alert", intensity_source="window", E=0.85)
        p = band.phrase(early, NOW)
        self.assertEqual((words(p), p["tone"]), ("May run out Wed 00:00, go ~1.5x slower", "warning"))
        later = dict(early, E=0.5)
        self.assertEqual(band.phrase(later, NOW)["tone"], "error")

    def test_a_milder_run_out_is_soft(self):
        h5, _ = band.windows(v5(verdict="on_pace", cap_ts=MON + 16 * H, severity="warn"), vw(), NOW)
        self.assertEqual((h5["kind"], h5["soft"], h5["phrase"]["narrow_pre"]), ("dry", True, "Out "))
        h5, _ = band.windows(*STATES["runs out"], NOW)
        self.assertEqual((h5["kind"], h5["soft"]), ("dry", False))

    def test_percents_round_down_as_claude_code_shows_them(self):
        self.assertEqual([render.pct(x) for x in (36.7, 36.0, 99.99, 100.0, 0.4)], ["36%", "36%", "99%", "100%", "0%"])
        self.assertEqual(band.windows(v5(used=36.96, known_ts=None), vw(used=31.5), NOW)[0]["pct"], 36)
        self.assertEqual(band.windows(v5(), vw(used=31.5), NOW)[1]["pct"], 31)


class Level(unittest.TestCase):
    """The percent is the last reading's, never lifted by an estimate of the
    work since: a band ahead of Claude Code's own number reads as wrong."""

    def setUp(self):
        utc()

    def test_the_last_reading_while_work_goes_on(self):
        h5, _ = band.windows(v5(used=36.6, rate=60, known_ts=NOW - 120), vw(), NOW)
        self.assertEqual((h5["pct"], h5["used"]), (36, 36.6))
        self.assertNotIn("approx", h5)
        self.assertTrue(h5["sentence"].startswith("5-hour: 36% used, resets 16:30."), h5["sentence"])

    def test_an_ended_window_reads_not_started(self):
        # The morning after: the last 5 hours ended at night, the next one
        # opens with the next message; the band said "waiting" until then.
        h5, wk = band.windows({"present": False, "ended_ts": MON + 3 * H}, vw(), NOW)
        self.assertEqual((h5["pct"], h5["kind"], words(h5["phrase"]), h5["phrase"]["attention"], h5["reset"]),
                         (0, "idle", "Not started", False, None))
        self.assertEqual(h5["sentence"], "5-hour: the last window ended at 03:00; a new one starts with your next "
                                         "message.")
        h5, _ = band.windows({"present": False, "ended_ts": MON - 2 * D}, vw(), NOW)
        self.assertTrue(h5["sentence"].startswith("5-hour: the last window ended Sat 00:00;"), h5["sentence"])
        self.assertEqual(render.plain(band.segments([h5, wk], config.load(os.devnull))),
                         "5h ──────── 0% Not started · Week ━━━━╍╍╍╍ 48%  resets Fri")


class TerminalLine(unittest.TestCase):
    def setUp(self):
        utc()
        self.cfg = config.load(os.devnull)

    def line(self, state, width=None, cfg=None):
        return render.plain(band.segments(band.windows(*STATES[state], NOW), cfg or self.cfg, width))

    def test_reference_lines(self):
        self.assertEqual(self.line("on pace"), "5h ━━━━╍╍╍╍ 55%  resets 16:30 · Week ━━━━╍╍╍╍ 48%  resets Fri")
        self.assertEqual(self.line("runs out", 100), "5h ━━━━━━╍╍ 70% Runs out 15:40, go 2x slower  resets 16:30 · "
                                                     "Week ━━━━╍╍╍╍ 48%  resets Fri")
        self.assertEqual(self.line("runs out"), "5h ━━━━━━╍╍ 70% Runs out 15:40, go 2x slower · Week ━━━━╍╍╍╍ 48%")
        self.assertEqual(self.line("early", 60), "5h ━╍╍╍╍─── 8% ~2x faster · Week ━╍╍───── 2% 2.7x faster")
        self.assertEqual(self.line("unused", 60), "5h ━╍╍───── 12% 1.7x faster · Week ━━━╍╍┈┈┈ 40% 40% unused")
        self.assertEqual(self.line("unused", 50), "5h ━╍╍───── 12% · Week ━━━╍╍┈┈┈ 40% 40% unused")
        self.assertEqual(self.line("maxed", 40), "5h ━━━━━ 100% Back 16:30 · Wk ━━━╍╍ 52%")
        self.assertEqual(self.line("runs out", 40), "5h ━━━━╍ 70% Out 15:40 · Week ━━╍╍╍ 48%")
        self.assertEqual(self.line("half"), "5h ━━━━╍╍╍╍ 55%  resets 16:30 · Week waiting")

    def test_tones(self):
        segs = band.segments(band.windows(*STATES["runs out"], NOW), self.cfg)
        tones = dict((t, tone) for t, tone in segs)
        self.assertEqual(tones[" Runs out 15:40, go 2x slower"], "error")
        self.assertEqual(tones["48%"], "inactive")  # the calm window dims beside one that needs you
        self.assertEqual(tones["70%"], "text")
        self.assertIn("\033[1;31m Runs out 15:40", render.to_ansi(segs, True))

    def test_every_width_fits_and_keeps_what_needs_you(self):
        for symbols in ("unicode", "ascii"):
            cfg = config.load(os.devnull)
            cfg["display"]["symbols"] = symbols
            for state in STATES:
                for width in range(20, 141):
                    line = self.line(state, width, cfg)
                    self.assertLessEqual(len(line), width, (symbols, state, width, line))
                    if symbols == "ascii":
                        self.assertTrue(all(ord(c) < 128 for c in line), line)
                    if width >= 40 and state in ("runs out", "maxed", "week dry"):
                        h5, wk = band.windows(*STATES[state], NOW)
                        p = (h5 if h5["phrase"] else wk)["phrase"]
                        self.assertTrue(p["pre"] + p["num"] in line or p["narrow_pre"] + p["num"] in line,
                                        (state, width, line))

    def test_classic_style_is_kept(self):
        self.cfg["display"]["style"] = "classic"
        line = render.plain(band.line(*STATES["on pace"], self.cfg, NOW))
        self.assertTrue(line.startswith("5h 55% ×1.1 →95% @16:30 · wk 48% +1"), line)
        self.cfg["display"]["style"] = "fancy"
        self.assertEqual(config.normalize(self.cfg)["display"]["style"], "bars")


class Stamp(TempHome):
    """A reading that changes what is shown touches CTEMPO_HOME/changed, so
    a band in another chat redraws within seconds; a heartbeat does not."""

    def render(self, now, five, week=(30, int(MON + 5 * D)), sid="s1"):
        raw = json.dumps(payload(five, week, sid)).encode()
        opts = statusline.parse_args(["--source", "mod", "--format", "json"])
        return statusline.compute(raw, self.cfg, opts, now)

    def stamp(self):
        try:
            with open(config.stamp_path(), encoding="utf-8") as f:
                return float(f.read())
        except OSError:
            return None

    def test_fresh_readings_touch_it_and_heartbeats_do_not(self):
        r5 = int(MON + 4 * H)
        self.assertIsNone(self.stamp())
        self.render(MON + H, (10, r5))
        self.assertEqual(self.stamp(), MON + H)
        self.render(MON + H + 20, (10, r5))  # the same reading again: a heartbeat
        self.assertEqual(self.stamp(), MON + H)
        self.render(MON + H + 40, (11, r5))
        self.assertEqual(self.stamp(), MON + H + 40)

    def test_the_view_knows_when_the_level_was_last_read(self):
        r5 = int(MON + 4 * H)
        self.known("s1", "s2")
        for k in range(8):
            self.render(MON + H + 60 * k, (10 + k, r5))
        self.render(MON + H + 485, (17, r5), sid="s2")  # another chat reads the same level later
        self.render(MON + H + 495, (17, r5))  # a heartbeat: not a new reading
        v5_, vw_, _ = self.render(MON + H + 500, (17, r5))
        self.assertEqual(v5_["known_ts"], MON + H + 485)
        self.assertEqual(band.updated(v5_, vw_), MON + H + 485)


if __name__ == "__main__":
    unittest.main()


class EndedWindow(TempHome):
    def test_the_store_says_when_the_last_window_ended(self):
        from ctempo import pace

        r5 = int(MON + 4 * H)
        self.known("s1")
        self.feed(MON + H, five=(30, r5), week=(20, int(MON + 5 * D)))
        self.assertNotIn("ended_ts", pace.five_hour(self.st, MON + 2 * H, self.cfg))
        v = pace.five_hour(self.st, MON + 6 * H, self.cfg)
        self.assertEqual((v["present"], v["ended_ts"]), (False, r5))
        self.assertEqual(band.window(v, MON + 6 * H)["phrase"]["pre"], "Not started")
