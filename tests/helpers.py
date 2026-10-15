import json
import math
import os
import shutil
import tempfile
import time
import unittest

from ctempo import config, ingest
from ctempo.store import Store

MON = 1791763200  # Monday 2026-10-12 00:00 UTC
H = 3600.0
D = 86400.0


def utc():
    os.environ["TZ"] = "UTC"
    time.tzset()


def payload(five=None, week=None, sid="s1"):
    rl = {}
    if five is not None:
        rl["five_hour"] = {"used_percentage": five[0], "resets_at": five[1]}
    if week is not None:
        rl["seven_day"] = {"used_percentage": week[0], "resets_at": week[1]}
    p = {"session_id": sid}
    if rl:
        p["rate_limits"] = rl
    return p


class TempHome(unittest.TestCase):
    """Each test gets its own CTEMPO_HOME and a fresh store."""

    def setUp(self):
        utc()
        self.dir = tempfile.mkdtemp(prefix="ctempo-test-")
        self.old_home = os.environ.get("CTEMPO_HOME")
        os.environ["CTEMPO_HOME"] = self.dir
        self.old_ccd = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.dir, "claude")  # never the real ~/.claude
        self.cfg = config.load()
        self.st = Store(os.path.join(self.dir, "tempo.db"))

    def tearDown(self):
        self.st.close()
        if self.old_home is None:
            os.environ.pop("CTEMPO_HOME", None)
        else:
            os.environ["CTEMPO_HOME"] = self.old_home
        if self.old_ccd is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.old_ccd
        shutil.rmtree(self.dir, ignore_errors=True)

    def use_home(self, name):
        """A fresh store in its own CTEMPO_HOME under the test's directory."""
        self.st.close()
        home = os.path.join(self.dir, name)
        os.makedirs(home)
        os.environ["CTEMPO_HOME"] = home
        self.st = Store(os.path.join(home, "tempo.db"))

    def feed(self, now, five=None, week=None, sid="s1", source="test"):
        readings, rejects, kinds = ingest.parse_payload(payload(five, week, sid), now)
        return ingest.record(self.st, readings, now, source, sid, self.cfg, rejects, kinds)

    def known(self, *sids):
        """Mark sessions as already heard from, so their next reading is timed (not FIRST).

        The row is dated far ahead: the first ingest prunes reader rows older
        than 7 days, which dropped a ts of 0 for every session not yet heard."""
        for sid in sids:
            for w in (config.FIVE_HOUR, config.SEVEN_DAY):
                self.st.reader_set(sid, w, -1.0, 0, 1e12)

    def late_start(self, t0, first, step, every, r5, week=None, renders=40):
        """Claude Code first reads the 5h window at `first`% at t0 (it was spent
        elsewhere before: claude.ai, another machine), then `step` points more
        every `every` seconds, with a render every 20 s. Yields each render's
        time once it is fed."""
        for k in range(renders):
            ts = t0 + 20 * k
            self.feed(ts, five=(min(100, first + step * int(20 * k // every)), r5), week=week)
            yield ts

    def work(self, t0, t1, R, rate, hours=(9, 18), days=(0, 1, 2, 3, 4), every=900, learn=False):
        """A user who spends `rate` weekly points an hour in local hours
        [hours[0], hours[1]) of `days` and nothing otherwise, from t0 (a
        window start, at 0%) to t1. A reading every `every` seconds while
        working, in whole percents, capped at 100; the weekly window resets
        at R and every 7 days after. With learn, the statusline's profile
        learning runs after each reading. Returns the next reset after t1."""
        from ctempo import profile, statusline

        prof = profile.load(self.st)
        level, t = 0.0, t0
        while t < t1:
            if t >= R:
                R += int(7 * D)
                level = 0.0
            lt = time.localtime(t)
            if lt.tm_wday in days and hours[0] <= lt.tm_hour < hours[1]:
                level = min(100.0, level + rate * every / H)
                self.feed(t + every, week=(math.floor(level), R))
                if learn:
                    prof = statusline.maybe_learn(self.st, prof, t + every, self.cfg)
            t += every
        return R

    def rows(self, window):
        return self.st.db.execute(
            "SELECT ts, used_pct, resets_at, flag, window_id, session_id FROM samples WHERE window=? ORDER BY ts, id",
            (window,),
        ).fetchall()

    def windows(self, window):
        return self.st.instances(window)


def write_json(path, obj, indent=2, newline=True):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=indent) + ("\n" if newline else ""))
