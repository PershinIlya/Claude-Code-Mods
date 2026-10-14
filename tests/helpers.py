import json
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

    def feed(self, now, five=None, week=None, sid="s1", source="test"):
        readings, rejects, kinds = ingest.parse_payload(payload(five, week, sid), now)
        return ingest.record(self.st, readings, now, source, sid, self.cfg, rejects, kinds)

    def known(self, *sids):
        """Mark sessions as already heard from, so their next reading is timed (not FIRST)."""
        for sid in sids:
            for w in (config.FIVE_HOUR, config.SEVEN_DAY):
                self.st.reader_set(sid, w, -1.0, 0, 0)

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
