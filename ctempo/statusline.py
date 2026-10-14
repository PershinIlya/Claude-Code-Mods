"""`ctempo statusline`: read the payload, record a sample, print the line.

Hot path. Fail open: any error prints a minimal line and exits 0, the
traceback goes to ~/.claude-tempo/errors.log (capped), never to stdout.
"""

import json
import os
import re
import sys
import time

from . import config

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)")
LOG_MAX = 64 * 1024


def log_error(exc, where="statusline"):
    try:
        import traceback

        path = config.log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), where, exc))
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:]))
    except Exception:
        pass


def parse_args(argv):
    opts = {"source": "statusline", "format": "ansi", "record": True, "compose": True, "now": None, "compose_cmd": None}
    it = iter(argv)
    for a in it:
        if a == "--source":
            opts["source"] = next(it, "statusline")
        elif a == "--format":
            opts["format"] = next(it, "ansi")
        elif a == "--no-record":
            opts["record"] = False
        elif a == "--no-compose":
            opts["compose"] = False
        elif a == "--compose":
            opts["compose_cmd"] = next(it, None)
        elif a == "--now":
            opts["now"] = float(next(it, "0"))
    return opts


def compute(raw, cfg, opts, now, line_of=None):
    """Record the payload and compute both views. Returns (v5, vw, readings).

    `line_of(v5, vw)` renders the plain line so it can be kept for `doctor`
    while the database is open."""
    import sqlite3

    from . import ingest, pace, profile, weekly
    from .store import Store

    try:
        payload = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        payload = {}
    readings, rejects, kinds = ingest.parse_payload(payload, now)
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    sid = sid if isinstance(sid, str) and sid else None
    st = Store(timeout_ms=cfg.get("db_timeout_ms", 250))
    try:
        recorded = False
        if opts["record"] and (readings or rejects or kinds):
            try:
                ingest.record(st, readings, now, opts["source"], sid, cfg, rejects, kinds)
                recorded = True
            except sqlite3.OperationalError as e:  # locked: skip this write, still render
                log_error(e, "record")
        # Once stored, the database has the judged level; the raw reading may
        # be an idle session's stale value. Only an unstored reading is used.
        shown = {} if recorded else readings
        prof = profile.load(st)
        if opts["record"]:
            prof = maybe_learn(st, prof, now, cfg)
        v5 = pace.five_hour(st, now, cfg, shown.get(config.FIVE_HOUR))
        vw = weekly.weekly(st, now, cfg, shown.get(config.SEVEN_DAY), prof)
        if opts["record"] and line_of is not None:
            rl = payload.get("rate_limits") if isinstance(payload, dict) else None
            note_render(st, now, opts["source"], line_of(v5, vw), rl)
    finally:
        st.close()
    return v5, vw, readings


def note_render(st, now, source, line, rate_limits):
    """Keep the last line and the raw rate_limits per source (numbers and
    window names only) for `ctempo doctor`; at most once a minute unless the
    line changed."""
    import sqlite3

    key = "last_render:" + source
    prev = st.meta_get(key) or {}
    if prev.get("line") == line and now - (prev.get("ts") or 0) < 60:
        return
    keep = None
    if isinstance(rate_limits, dict):
        keep = {
            str(k)[:32]: {f: v for f, v in w.items() if isinstance(v, (int, float, str)) and len(str(v)) < 40}
            for k, w in list(rate_limits.items())[:8]
            if isinstance(w, dict)
        }
    try:
        st.meta_set(key, {"ts": now, "line": line, "rate_limits": keep})
    except sqlite3.OperationalError:
        pass


def maybe_learn(st, prof, now, cfg):
    """Fold newly completed hours into the profile, at most every 10 minutes.

    The profile is re-read inside the write transaction, so a bootstrap or a
    rebuild committed meanwhile is not overwritten. Returns the profile."""
    import sqlite3

    from . import profile

    checked = st.meta_get("profile_checked_at", 0) or 0
    if now - checked < 600:
        return prof
    try:
        st.begin()
        prof = profile.load(st)
        w, _, _ = prof.weights(now, cfg)
        profile.learn(st, prof, now, cfg, w)
        profile.save(st, prof)
        st.meta_set("profile_checked_at", now)
        st.commit()
    except sqlite3.OperationalError as e:
        st.rollback()
        log_error(e, "learn")
    return prof


def bare_line(readings, now, cfg=None):
    """Fallback without the database: levels and reset times only."""
    from .render import dhm, fit, hm, pct, plain, symbols

    cfg = cfg or config.DEFAULTS
    parts = []
    if config.FIVE_HOUR in readings:
        u, r = readings[config.FIVE_HOUR]
        parts.append("5h %s @%s" % (pct(u), hm(r)))
    if config.SEVEN_DAY in readings:
        u, r = readings[config.SEVEN_DAY]
        parts.append("wk %s @%s" % (pct(u), dhm(r)))
    return plain(fit([(symbols(cfg)["sep"].join(parts) or "ctempo: no data", "dim")], cfg))


class Composer:
    """Runs the user's previous statusLine command in parallel with ours.

    communicate() runs in a thread so our own work overlaps with theirs; the
    command gets its own process group so a timeout kills its children too.
    """

    def __init__(self, cmd, raw, timeout):
        import subprocess
        import threading

        env = dict(os.environ)
        env["CTEMPO_COMPOSED"] = "1"
        self.result = None
        self.p = subprocess.Popen(
            cmd,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        self.t = threading.Thread(target=self._run, args=(raw, timeout), daemon=True)
        self.t.start()

    def _run(self, raw, timeout):
        try:
            out, _ = self.p.communicate(input=raw, timeout=timeout)
            self.result = (self.p.returncode, out)
        except Exception:
            pass

    def finish(self, deadline):
        self.t.join(max(0.05, deadline - time.time()))
        if self.result is None:
            self._kill()
            return None
        rc, out = self.result
        if rc != 0:
            return None
        return out.decode("utf-8", "replace").rstrip("\n")

    def _kill(self):
        import signal

        try:
            os.killpg(self.p.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                self.p.kill()
            except OSError:
                pass


def visible_len(s):
    return len(ANSI_RE.sub("", s))


def join_composed(theirs, ours, cfg, columns):
    if not theirs:
        return ours
    lines = theirs.split("\n")
    sep = cfg["compose"].get("separator", "  ")
    if visible_len(lines[-1]) + len(sep) + visible_len(ours) <= columns:
        lines[-1] = lines[-1] + sep + ours
    else:
        lines.append(ours)
    return "\n".join(lines)


def main(argv):
    t0 = time.time()
    opts = {"format": "ansi"}
    color = False
    out = ""
    composer = None
    cfg = None
    try:
        opts = parse_args(argv)
        raw = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()
        cfg = config.load()
        from . import render

        color = render.use_color(cfg) and opts["format"] == "ansi"
        cmd = opts["compose_cmd"] or cfg["compose"].get("command")
        if (
            cmd
            and opts["compose"]
            and opts["source"] == "statusline"
            and opts["format"] == "ansi"
            and not os.environ.get("CTEMPO_COMPOSED")
        ):
            try:
                composer = Composer(cmd, raw, float(cfg["compose"].get("timeout_ms", 1500)) / 1000.0)
            except Exception as e:
                log_error(e, "compose")
        now = opts["now"] or time.time()
        readings = {}
        try:
            def segs_of(v5, vw):
                if not v5.get("present") and not vw.get("present"):
                    return render.fit([("ctempo: waiting for usage data", "dim")], cfg)
                return render.segments(v5, vw, cfg)

            v5, vw, readings = compute(raw, cfg, opts, now, lambda a, b: render.plain(segs_of(a, b)))
            segs = segs_of(v5, vw)
        except Exception as e:  # database trouble: degrade to the bare line
            log_error(e)
            if not readings:
                from . import ingest

                try:
                    readings = ingest.parse_payload(json.loads(raw or b"{}"), now)[0]
                except Exception:
                    readings = {}
            segs = [(bare_line(readings, now, cfg), "dim")]
            v5 = vw = {}
        if opts["format"] == "json":
            out = json.dumps(
                {
                    "ok": True,
                    "line": render.plain(segs),
                    "segments": [{"text": t, "tone": tone} for t, tone in segs],
                    "surfaces": cfg["mod"].get("surfaces", ["desktop"]),
                    "refresh_s": cfg["mod"].get("refresh_s", 60),
                    "five_hour": _brief(v5),
                    "seven_day": _brief(vw),
                },
                separators=(",", ":"),
            )
        else:
            out = render.to_ansi(segs, color)
    except Exception as e:
        log_error(e)
        if opts.get("format") == "json":
            mod = (cfg or config.DEFAULTS)["mod"]
            out = json.dumps(
                {"ok": False, "line": "ctempo: error", "segments": [{"text": "ctempo: error", "tone": "dim"}],
                 "surfaces": mod.get("surfaces", ["desktop"]), "refresh_s": mod.get("refresh_s", 60)},
                separators=(",", ":"),
            )
        else:
            out = "ctempo: error"
    if composer is not None:
        # Outside the main try: whatever failed above, the user's own line still shows.
        try:
            timeout = float(cfg["compose"].get("timeout_ms", 1500)) / 1000.0
            theirs = composer.finish(t0 + timeout)
            out = join_composed(theirs, out, cfg, columns_env())
        except Exception as e:
            log_error(e, "compose")
    write_out(out)
    return 0


def columns_env():
    try:
        n = int(float((os.environ.get("COLUMNS") or "").strip() or 0))
    except (ValueError, OverflowError):
        n = 0
    return n if n > 0 else 200


def write_out(out):
    """UTF-8 bytes whatever the locale: Claude Code reads stdout as UTF-8."""
    try:
        data = (out + "\n").encode("utf-8", "replace")
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write(data)
            buf.flush()
        else:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
    except Exception as e:
        log_error(e, "write")


def _brief(v):
    keys = ("present", "used", "reset_ts", "verdict", "severity", "multiplier", "projected", "delta")
    return {k: v.get(k) for k in keys if k in v} if v else {}
