"""The statusline as Claude Code runs it: a subprocess fed a payload on stdin."""

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
    e.update({"CTEMPO_HOME": home, "TZ": "UTC"})
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
        v = dict(present=True, used=48.0, reset_ts=MON + 3 * D + 9 * H, verdict="slow_down", multiplier=0.8,
                 projected=96.0, dry_ts=None, delta=3.2, severity="warn")
        v.update(kw)
        return v

    def line(self, v5, vw, cfg=None):
        return render.plain(render.segments(v5, vw, cfg or self.cfg))

    def test_reference_line(self):
        self.assertEqual(self.line(self.v5(), self.vw()), "5h 62% ×1.4▲ →88% @13:20 · wk 48% +3 ×0.8▼ →96% @Thu 09:00")

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


if __name__ == "__main__":
    unittest.main()
