"""The statusline as Claude Code runs it: a subprocess fed a payload on stdin."""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from ctempo import config, render

from .helpers import MON, H, D, utc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "bin", "ctempo")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def run(payload, home, args=(), env=None, raw=None):
    e = dict(os.environ)
    e.update({"CTEMPO_HOME": home, "TZ": "UTC", "CLAUDE_CONFIG_DIR": os.path.join(home, "claude")})
    e.pop("NO_COLOR", None)
    e.update(env or {})
    data = raw if raw is not None else json.dumps(payload).encode()
    t = time.time()
    p = subprocess.run([sys.executable, "-I", "-S", ENTRY, "statusline"] + list(args), input=data,
                       capture_output=True, env=e, timeout=30)
    return p, time.time() - t


class Statusline(unittest.TestCase):
    def setUp(self):
        utc()
        self.home = tempfile.mkdtemp(prefix="ctempo-sl-")
        self.now = time.time()
        self.p = {
            "session_id": "abc",
            "rate_limits": {
                "five_hour": {"used_percentage": 42, "resets_at": int(self.now + 2 * H)},
                "seven_day": {"used_percentage": 31.5, "resets_at": int(self.now + 3 * D)},
            },
        }

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_prints_one_line_and_records(self):
        p, _ = run(self.p, self.home)
        self.assertEqual(p.returncode, 0)
        out = p.stdout.decode()
        self.assertEqual(out.count("\n"), 1)
        plain = ANSI.sub("", out.strip())
        self.assertTrue(plain.startswith("5h 42%"), plain)
        self.assertIn("wk 32%", plain)
        self.assertLessEqual(len(plain), 80)
        self.assertEqual(p.stderr, b"")
        self.assertTrue(os.path.exists(os.path.join(self.home, "tempo.db")))

    def test_no_color(self):
        p, _ = run(self.p, self.home, env={"NO_COLOR": "1"})
        self.assertNotIn(b"\x1b", p.stdout)

    def test_garbage_and_empty_input_fail_open(self):
        for raw in (b"", b"not json", b"[1,2]", b'{"rate_limits": {"five_hour": {"used_percentage": "x"}}}'):
            p, _ = run(None, self.home, raw=raw)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(p.stdout.count(b"\n"), 1)
            self.assertNotIn(b"Traceback", p.stdout + p.stderr)

    def test_unwritable_home_degrades_to_bare_line(self):
        blocker = os.path.join(self.home, "file")
        open(blocker, "w").close()
        p, _ = run(self.p, os.path.join(blocker, "sub"))
        self.assertEqual(p.returncode, 0)
        plain = ANSI.sub("", p.stdout.decode().strip())
        self.assertTrue(plain.startswith("5h 42% @"), plain)
        self.assertNotIn(b"Traceback", p.stdout + p.stderr)

    def test_bare_line_follows_the_display_settings(self):
        # Review 4: with a broken database the bare line joined with " · " and
        # was never fitted: "5h 42% @HH:MM · wk 32% @Ddd HH:MM" is 33 columns.
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"display": {"symbols": "ascii", "max_width": 24}}, f)
        with open(os.path.join(self.home, "tempo.db"), "wb") as f:
            f.write(b"not a database " * 100)
        for args in ((), ("--format", "json")):
            p, _ = run(self.p, self.home, args=args)
            out = p.stdout.decode().strip()
            line = json.loads(out)["line"] if args else ANSI.sub("", out)
            self.assertTrue(line.startswith("5h 42% @"), line)
            self.assertEqual(line[13:], " | wk 32...")  # 21 columns kept, then "..."
            self.assertLessEqual(len(line), 24)

    def test_waiting_line_follows_the_display_settings(self):
        # Review 4: "ctempo: waiting for usage data" (30 columns) ignored max_width.
        no_limits = {"session_id": "abc"}
        for symbols, want in (("unicode", "ctempo: waiting for usa…"), ("ascii", "ctempo: waiting for u...")):
            with open(os.path.join(self.home, "config.json"), "w") as f:
                json.dump({"display": {"symbols": symbols, "max_width": 24}}, f)
            p, _ = run(no_limits, self.home, env={"NO_COLOR": "1"})
            self.assertEqual(p.stdout.decode("utf-8"), want + "\n")

    def test_bad_config_types_fall_back_to_defaults(self):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"verdict": 5, "display": {"max_width": "wide", "symbols": "ascii"}, "reset": None,
                       "target_5h": "lots", "min_free": True, "future_key": 1}, f)
        cfg = config.load(os.path.join(self.home, "config.json"))
        self.assertEqual(cfg["verdict"], config.DEFAULTS["verdict"])
        self.assertEqual(cfg["display"]["max_width"], 80)
        self.assertEqual(cfg["display"]["symbols"], "ascii")
        self.assertEqual(cfg["target_5h"], 100.0)
        self.assertEqual(cfg["min_free"], 10.0)
        self.assertEqual(cfg["future_key"], 1)
        p, _ = run(self.p, self.home)
        self.assertTrue(ANSI.sub("", p.stdout.decode()).startswith("5h 42%"), p.stdout)

    def test_non_utf8_stdout_encoding_still_prints(self):
        e = dict(os.environ, CTEMPO_HOME=self.home, TZ="UTC", PYTHONIOENCODING="latin-1")
        e.pop("NO_COLOR", None)
        p = subprocess.run([sys.executable, "-S", ENTRY, "statusline"], input=json.dumps(self.p).encode(),
                           capture_output=True, env=e, timeout=30)
        out = p.stdout.decode("utf-8")
        self.assertIn("×", out)
        self.assertTrue(ANSI.sub("", out).startswith("5h 42%"))

    def test_bad_columns_keeps_the_composed_line(self):
        for cols in ("abc", "80.0", "-5", ""):
            p, _ = run(self.p, self.home, args=["--compose", "printf theirs"], env={"COLUMNS": cols})
            self.assertTrue(ANSI.sub("", p.stdout.decode()).startswith("theirs"), (cols, p.stdout))

    def test_invalid_config_values_are_dropped_or_clamped(self):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"profile": {"override": {"work_days": ["Mon", "Tue"], "wake_hours": [8, 23]}, "half_life_days": 0},
                       "compose": {"command": True, "timeout_ms": 1e9}, "display": {"max_width": 3, "symbols": "emoji"},
                       "target_5h": float("inf") if False else 100}, f)
        cfg = config.load(os.path.join(self.home, "config.json"))
        self.assertEqual(cfg["profile"]["override"]["work_days"], [0, 1, 2, 3, 4])  # bad field: default
        self.assertEqual(cfg["profile"]["override"]["wake_hours"], [8, 23])
        self.assertEqual(cfg["profile"]["half_life_days"], 0.5)
        self.assertIsNone(cfg["compose"]["command"])
        self.assertEqual(cfg["compose"]["timeout_ms"], 10000)
        self.assertEqual(cfg["display"]["max_width"], 20)
        self.assertEqual(cfg["display"]["symbols"], "unicode")
        p, _ = run(self.p, self.home)
        line = ANSI.sub("", p.stdout.decode()).strip()
        self.assertTrue(line.startswith("5h 42%"), line)  # full math, not the bare fallback
        self.assertLessEqual(len(line), 20)
        self.assertNotIn("error", line)

    def test_override_fields_are_checked_one_by_one_and_empty_surfaces_kept(self):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"profile": {"override": {"work_days": [0, 1, 2.0, 9, "x"], "wake_hours": [7.5, 23],
                                                "off_day_factor": 3, "mode": "REPLACE"}},
                       "mod": {"surfaces": []}}, f)
        cfg = config.load(os.path.join(self.home, "config.json"))
        self.assertEqual(cfg["profile"]["override"],
                         {"work_days": [0, 1, 2], "wake_hours": [7, 23], "off_day_factor": 1.0, "mode": "replace"})
        self.assertEqual(cfg["mod"]["surfaces"], [])
        p, _ = run(self.p, self.home, args=["--source", "mod", "--format", "json"])
        self.assertEqual(json.loads(p.stdout)["surfaces"], [])

    def test_pathological_config_falls_back_to_defaults(self):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            f.write("[" * 200000 + "]" * 200000)
        self.assertEqual(config.load(os.path.join(self.home, "config.json")), config.DEFAULTS)
        p, _ = run(self.p, self.home)
        self.assertTrue(ANSI.sub("", p.stdout.decode()).startswith("5h 42%"))

    def test_error_json_still_tells_the_mod_where_to_draw(self):
        from contextlib import redirect_stdout
        from unittest import mock

        from ctempo import statusline

        class Out:
            def __init__(self):
                self.buffer = io.BytesIO()

        o = Out()
        with mock.patch.dict(os.environ, {"CTEMPO_HOME": self.home}), mock.patch.object(render, "plain", side_effect=RuntimeError("x")), \
                mock.patch.object(sys, "stdout", o), mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(self.p).encode()))):
            statusline.main(["--format", "json"])
        d = json.loads(o.buffer.getvalue())
        self.assertFalse(d["ok"])
        self.assertEqual(d["surfaces"], ["desktop"])
        self.assertEqual(d["refresh_s"], 60)

    def test_epoch_leak_payload_is_not_shown(self):
        self.p["rate_limits"]["seven_day"]["used_percentage"] = 1783720704
        p, _ = run(self.p, self.home)
        plain = ANSI.sub("", p.stdout.decode())
        self.assertNotIn("1783720704", plain)
        self.assertIn("wk 0%", plain)

    def test_json_format_for_the_mod(self):
        p, _ = run(self.p, self.home, args=["--source", "mod", "--format", "json"])
        d = json.loads(p.stdout)
        self.assertTrue(d["ok"])
        self.assertTrue(d["line"].startswith("5h 42%"))
        self.assertEqual("".join(s["text"] for s in d["segments"]), d["line"])
        self.assertIn(d["segments"][0]["tone"], ("dim", "plain", "opportunity", "warn", "alert"))
        self.assertIn("desktop", d["surfaces"])

    def test_compose_appends_to_existing_command(self):
        p, _ = run(self.p, self.home, args=["--compose", "printf 'theirs'"], env={"COLUMNS": "200"})
        plain = ANSI.sub("", p.stdout.decode())
        self.assertTrue(plain.startswith("theirs  5h 42%"), plain)

    def test_compose_passes_stdin_through(self):
        cmd = "%s -c \"import sys, json; print(json.load(sys.stdin)['session_id'])\"" % sys.executable
        p, _ = run(self.p, self.home, args=["--compose", cmd], env={"COLUMNS": "200"})
        self.assertTrue(ANSI.sub("", p.stdout.decode()).startswith("abc  5h"))

    def test_compose_wraps_when_too_wide(self):
        p, _ = run(self.p, self.home, args=["--compose", "printf '%080d' 0"], env={"COLUMNS": "100"})
        lines = ANSI.sub("", p.stdout.decode()).rstrip("\n").split("\n")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("5h 42%"))

    def test_compose_failure_and_timeout_show_ours(self):
        p, _ = run(self.p, self.home, args=["--compose", "exit 3"])
        self.assertTrue(ANSI.sub("", p.stdout.decode()).startswith("5h 42%"))
        p, el = run(self.p, self.home, args=["--compose", "sleep 5; echo late"])
        self.assertTrue(ANSI.sub("", p.stdout.decode()).startswith("5h 42%"))
        self.assertLess(el, 4.0)

    def test_no_recursion_when_composed_with_itself(self):
        cmd = "%s -I -S %s statusline --compose 'printf x'" % (sys.executable, ENTRY)
        p, el = run(self.p, self.home, args=["--compose", cmd], env={"COLUMNS": "300"})
        out = ANSI.sub("", p.stdout.decode())
        self.assertEqual(out.count("5h 42%"), 2)  # inner ctempo does not compose again
        self.assertNotIn("x", out.split("5h")[0])


class StaleReadingAfterReset(unittest.TestCase):
    """An idle session's pre-reset reading must not win on display."""

    def test_shows_the_judged_level(self):
        from ctempo import ingest, statusline
        from ctempo.store import Store

        utc()
        home = tempfile.mkdtemp(prefix="ctempo-stale-")
        old = os.environ.get("CTEMPO_HOME")
        os.environ["CTEMPO_HOME"] = home
        try:
            cfg = config.load()
            st = Store()
            now = MON + 2 * H
            R = int(MON + 4 * H)
            for t, used, sid in ((now - 600, 95, "idle"), (now - 590, 95, "a"), (now - 300, 1, "a"), (now - 240, 2, "a")):
                r, rej, k = ingest.parse_payload({"rate_limits": {"five_hour": {"used_percentage": used, "resets_at": R}}}, t)
                ingest.record(st, r, t, "statusline", sid, cfg, rej, k)
            st.close()
            raw = json.dumps({"session_id": "idle", "rate_limits": {"five_hour": {"used_percentage": 95, "resets_at": R}}}).encode()
            opts = statusline.parse_args([])
            v5, vw, readings = statusline.compute(raw, cfg, opts, now)
            self.assertEqual(v5["used"], 2)
            self.assertNotEqual(v5["verdict"], "maxed")
        finally:
            if old is None:
                os.environ.pop("CTEMPO_HOME", None)
            else:
                os.environ["CTEMPO_HOME"] = old
            shutil.rmtree(home, ignore_errors=True)


class Render(unittest.TestCase):
    def setUp(self):
        utc()
        self.cfg = config.load(os.devnull)

    def v5(self, **kw):
        v = dict(present=True, used=62.0, reset_ts=MON + 13 * H + 20 * 60, verdict="speed_up", multiplier=1.4,
                 projected=88.0, cap_ts=None, allowed=9.1, rate_source="lsq", severity="dim")
        v.update(kw)
        return v

    def vw(self, **kw):
        # Slow down without a dry time: a target_week below 100 (yellow).
        v = dict(present=True, used=48.0, reset_ts=MON + 3 * D + 9 * H, verdict="slow_down", multiplier=0.8,
                 projected=96.0, dry_ts=None, delta=3.2, severity="warn")
        v.update(kw)
        return v

    def line(self, v5, vw, cfg=None):
        return render.plain(render.segments(v5, vw, cfg or self.cfg))

    def test_reference_line(self):
        # Review 4: the README line, in a state the default targets can reach
        # (x0.8 at target 100 runs dry; x1.1 projects 48 + 52 / 1.1 = 95).
        vw = self.vw(verdict="on_pace", multiplier=1.1, projected=95.0, severity="dim")
        self.assertEqual(self.line(self.v5(), vw), "5h 62% ×1.4▲ →88% @13:20 · wk 48% +3 ×1.1 →95% @Thu 09:00")

    def test_dry_idle_and_fallbacks(self):
        s = self.line(self.v5(verdict="slow_down", multiplier=0.6, cap_ts=MON + 12 * H + 40 * 60),
                      self.vw(dry_ts=MON + 2 * D + 14 * H, delta=12.4))
        self.assertEqual(s, "5h 62% ×0.6▼ dry 12:40 @13:20 · wk 48% +12 ×0.8▼ dry~Wed 14:00 @Thu 09:00")
        self.assertTrue(self.line(self.v5(verdict="idle"), self.vw()).startswith("5h 62% idle ≤9%/h @13:20"))
        self.assertTrue(self.line(self.v5(rate_source="window_avg", verdict="on_pace", multiplier=1.0), self.vw()).startswith("5h 62% ~×1.0 →88%"))
        self.assertEqual(self.line({"present": False}, {"present": False}), "5h 0% · wk 0%")

    def test_width_never_exceeds_limit(self):
        long5 = self.v5(verdict="slow_down", multiplier=0.06, cap_ts=MON + 12 * H, used=99.7)
        longw = self.vw(dry_ts=MON + 2 * D + 14 * H, delta=-88.0, multiplier=12.0)
        for width in (80, 60, 50, 40):
            cfg = config.load(os.devnull)
            cfg["display"]["max_width"] = width
            self.assertLessEqual(len(self.line(long5, longw, cfg)), width)

    def test_ascii_symbols(self):
        cfg = config.load(os.devnull)
        cfg["display"]["symbols"] = "ascii"
        s = self.line(self.v5(), self.vw())
        s2 = self.line(self.v5(), self.vw(), cfg)
        self.assertNotEqual(s, s2)
        self.assertTrue(all(ord(c) < 128 for c in s2), s2)

    def test_last_resort_cut_keeps_each_tone(self):
        # Review 4: when the required parts alone overflow, the cut made the
        # whole line one dim segment, so both red windows lost their color.
        # Required: 22 + 3 + 26 = 51 columns; 40 leaves 39 (unicode) or 37
        # (ascii) before the ellipsis.
        cfg = config.load(os.devnull)
        cfg["display"]["max_width"] = 40
        v5 = self.v5(verdict="slow_down", multiplier=0.6, cap_ts=MON + 12 * H + 40 * 60, severity="alert")
        vw = self.vw(dry_ts=MON + 2 * D + 14 * H, severity="alert")
        self.assertEqual(render.segments(v5, vw, cfg),
                         [("5h 62% ×0.6▼ dry 12:40", "alert"), (" · ", "dim"), ("wk 48% ×0.8▼ d…", "alert")])
        cfg["display"]["symbols"] = "ascii"
        self.assertEqual(render.segments(v5, vw, cfg),
                         [("5h 62% x0.6v dry 12:40", "alert"), (" | ", "dim"), ("wk 48% x0.8v...", "alert")])

    def test_ascii_cut_stays_ascii_and_within_width(self):
        # Review 4: the cut appended "…" in ascii mode too.
        long5 = self.v5(verdict="slow_down", multiplier=0.06, cap_ts=MON + 12 * H, used=99.7, severity="alert")
        longw = self.vw(dry_ts=MON + 2 * D + 14 * H, delta=-88.0, multiplier=12.0)
        for symbols in ("unicode", "ascii"):
            for width in range(20, 81):
                cfg = config.load(os.devnull)
                cfg["display"].update(symbols=symbols, max_width=width)
                segs = render.segments(long5, longw, cfg)
                line = render.plain(segs)
                self.assertLessEqual(len(line), width, (symbols, line))
                self.assertEqual(segs[0][1], "alert", (symbols, segs))
                if symbols == "ascii":
                    self.assertTrue(all(ord(c) < 128 for c in line), line)

    def test_bare_line_uses_the_symbols_and_fits(self):
        # Review 4: 13 + 3 + 17 = 33 columns; 32 keeps 29, then "...".
        from ctempo import statusline

        cfg = config.load(os.devnull)
        cfg["display"].update(symbols="ascii", max_width=32)
        readings = {config.FIVE_HOUR: (40, MON + 16 * H + 600), config.SEVEN_DAY: (30, MON + 5 * D + 14 * H + 600)}
        self.assertEqual(statusline.bare_line(readings, MON, cfg), "5h 40% @16:10 | wk 30% @Sat 1...")
        self.assertEqual(statusline.bare_line(readings, MON, self.cfg), "5h 40% @16:10 · wk 30% @Sat 14:10")

    def test_formatting_helpers(self):
        self.assertEqual(render.pct(99.6), "99%")
        self.assertEqual(render.pct(100.0), "100%")
        self.assertEqual(render.mult(12), "9+")
        self.assertEqual(render.rate(0.42), "0.4")

    def test_colors(self):
        segs = render.segments(self.v5(severity="alert"), self.vw(), self.cfg)
        out = render.to_ansi(segs, True)
        self.assertIn("\033[1;31m5h", out)
        self.assertIn("\033[33mwk", out)
        self.assertFalse(render.use_color(self.cfg, {"NO_COLOR": "1"}))
        self.assertTrue(render.use_color(self.cfg, {}))


class TildeHome(unittest.TestCase):
    """Review 4 (runtime-ui#0): Claude Code copies settings.json `env` values
    as written, so CTEMPO_HOME can arrive as "~/..." or as a relative path. It
    must resolve under the user's home, never under each session's cwd."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctempo-tilde-")
        self.user = os.path.join(self.dir, "home")
        self.cwd = os.path.join(self.dir, "project")
        os.makedirs(self.user)
        os.makedirs(self.cwd)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_home_expands_tilde_and_takes_relative_paths_from_home(self):
        from unittest import mock

        cases = (("~/.ctempo-moved", os.path.join(self.user, ".ctempo-moved")), ("~", self.user),
                 ("data/ctempo", os.path.join(self.user, "data", "ctempo")), ("/srv/ctempo", "/srv/ctempo"),
                 ("", os.path.join(self.user, ".claude-tempo")))
        for value, want in cases:
            with mock.patch.dict(os.environ, {"HOME": self.user, "CTEMPO_HOME": value}):
                self.assertEqual(config.home(), want, value)

    def test_statusline_with_tilde_home_writes_under_home_not_cwd(self):
        now = time.time()
        payload = {"session_id": "s", "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": int(now + 2 * H)}}}
        env = dict(os.environ, HOME=self.user, CTEMPO_HOME="~/.ctempo-moved", TZ="UTC",
                   CLAUDE_CONFIG_DIR=os.path.join(self.dir, "claude"))
        p = subprocess.run([sys.executable, "-I", "-S", ENTRY, "statusline"], input=json.dumps(payload).encode(),
                           capture_output=True, env=env, cwd=self.cwd, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.user, ".ctempo-moved", "tempo.db")))
        self.assertEqual(os.listdir(self.cwd), [])  # no directory literally named "~" in the project


if __name__ == "__main__":
    unittest.main()
