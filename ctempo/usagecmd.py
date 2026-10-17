"""Readings from Claude Code's own `/usage` command, for the menu bar.

The status line and the mod see the rate limits only when a Claude Code
session on this machine talks to the API. `claude -p "/usage"` asks for the
account's current levels whenever it runs, so work in chats, Cowork or cloud
sessions shows too. ctempo never sees a credential: Claude Code makes the
request. Off by default (menubar.fetch_usage_min 0); the menu bar runs it at
most every that many minutes. It prints

  Current session: 11% used · resets Oct 17 at 6:20pm (Asia/Dubai)
  Current week (all models): 20% used · resets Oct 23 at 6am (Asia/Dubai)

which becomes a statusLine-shaped payload from session "usage-cmd".
"""

import datetime
import os
import re
import shutil
import subprocess
import time

from . import config

SESSION_ID = "usage-cmd"
SOURCE = "usage"
TIMEOUT_S = 45
LINE = re.compile(r"^\s*Current (session|week \(all models\)):\s*([0-9]+(?:\.[0-9]+)?)% used"
                  r"(?:\s*\S\s*resets\s+(.+?)\s*\(([^()]+)\))?\s*$")
WHEN = re.compile(r"^(?:([A-Z][a-z]{2})\s+(\d{1,2})\s+at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)$", re.I)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
WINDOW = {"session": config.FIVE_HOUR, "week (all models)": config.SEVEN_DAY}


def reset_epoch(text, tz_name, now):
    """Epoch seconds of "Oct 17 at 6:20pm" (or "6:20pm") in the IANA zone,
    the next such time after now (within 8 days); None if it does not read."""
    m = WHEN.match(text.strip())
    if not m:
        return None
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:  # Python 3.8 has no zoneinfo; a wrong zone would move the window by hours
        return None
    mon, day, hour, minute, ampm = m.groups()
    hour = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
    minute = int(minute or 0)
    here = datetime.datetime.fromtimestamp(now, tz)
    for add in range(0, 9):
        d = here.date() + datetime.timedelta(days=add)
        if mon and (d.month != MONTHS.index(mon.lower()) + 1 or d.day != int(day)):
            continue
        at = datetime.datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz).timestamp()
        if at > now - 60:
            return at
    return None


def parse(text, now):
    """{window: (used, resets_at)} from `/usage` output."""
    out = {}
    for line in text.splitlines():
        m = LINE.match(line)
        if not m or not m.group(3):
            continue
        reset = reset_epoch(m.group(3), m.group(4), now)
        if reset is not None:
            out[WINDOW[m.group(1)]] = (float(m.group(2)), int(round(reset)))
    return out


def claude_bin():
    """claude on PATH, or where its installers put it (SwiftBar runs plugins with a bare PATH)."""
    extra = [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    path = os.pathsep.join([os.environ.get("PATH", "")] + extra)
    return shutil.which("claude", path=path)


def fetch(now=None, run=None):
    """Run `/usage` and record what it says. Returns (ok, message)."""
    now = time.time() if now is None else now
    exe = claude_bin()
    if not exe and run is None:
        return False, "claude not found"
    try:
        if run is None:
            env = {k: v for k, v in os.environ.items() if k != "CTEMPO_HOME"}
            p = subprocess.run([exe, "-p", "/usage"], capture_output=True, text=True, timeout=TIMEOUT_S,
                               cwd=config.home(), env=env)
            text, rc = p.stdout, p.returncode
        else:
            text, rc = run()
    except (OSError, subprocess.SubprocessError) as e:
        return False, "claude -p /usage failed: %s" % e
    readings = parse(text, now)
    if rc != 0 or not readings:
        return False, "could not read the usage from claude -p /usage"
    payload_rl = {w: {"used_percentage": u, "resets_at": r} for w, (u, r) in readings.items()}
    record(payload_rl, now)
    return True, ", ".join("%s %g%%" % (config.LABEL[w], u) for w, (u, _) in sorted(readings.items()))


def record(rate_limits, now):
    from . import ingest
    from .statusline import shown_changed, touch_stamp
    from .store import Store

    cfg = config.load()
    readings, rejects, kinds = ingest.parse_payload({"rate_limits": rate_limits}, now)
    st = Store(timeout_ms=2000)
    try:
        actions = ingest.record(st, readings, now, SOURCE, SESSION_ID, cfg, rejects, kinds)
        st.meta_set("usage_cmd_ok_at", now)
    finally:
        st.close()
    if any(shown_changed(a) for a in actions.values()):
        touch_stamp(now)


def due(st, now, every_min):
    """Whether a fetch is due, and marks the attempt so overlapping runs do not pile up."""
    if not every_min or every_min <= 0:
        return False
    last = st.meta_get("usage_cmd_tried_at", 0) or 0
    if now - last < every_min * 60:
        return False
    st.meta_set("usage_cmd_tried_at", now)
    return True
