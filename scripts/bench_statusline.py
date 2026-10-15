#!/usr/bin/env python3
"""Time `ctempo statusline` the way Claude Code runs it: a fresh process per
render, payload on stdin, against a database with weeks of history.

    python3 scripts/bench_statusline.py [--runs 50] [--python /usr/bin/python3]
                                        [--home DIR]   # default: a seeded temp home

Prints p50 / p95 / max wall time in milliseconds.
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)

from ctempo import clock, config, ingest, profile, winshell  # noqa: E402
from ctempo.store import Store  # noqa: E402


def seed(home, now, days=45):
    """Synthetic history: weekday work hours, a reading a minute while active."""
    os.environ["CTEMPO_HOME"] = home
    cfg = config.load()
    st = Store(os.path.join(home, "tempo.db"))
    rnd = random.Random(7)
    t = now - days * 86400
    week_reset = t + 3.5 * 86400  # not a whole number of weeks before now
    five_reset = None
    week_used = five_used = 0.0
    n = 0
    while t < now - 60:
        lt = clock.localtime(t)
        if t >= week_reset:
            week_reset += 7 * 86400
            week_used = 0.0
        active = lt.tm_wday < 5 and 9 <= lt.tm_hour < 19
        if active:
            if five_reset is None or t >= five_reset:
                five_reset, five_used = t + 5 * 3600, 0.0
            five_used = min(100.0, five_used + rnd.uniform(0, 0.6))
            week_used = min(100.0, week_used + rnd.uniform(0, 0.05))
            payload = {"session_id": "s%d" % (int(t) // 7200), "rate_limits": {
                "five_hour": {"used_percentage": round(five_used, 1), "resets_at": int(five_reset)},
                "seven_day": {"used_percentage": round(week_used, 1), "resets_at": int(week_reset)}}}
            r, rej, kinds = ingest.parse_payload(payload, t)
            ingest.record(st, r, t, "statusline", payload["session_id"], cfg, rej, kinds)
            n += 1
        t += 60
    prof = profile.load(st)
    st.begin()
    profile.learn(st, prof, now, cfg)
    profile.save(st, prof)
    st.meta_set("profile_checked_at", now)
    st.commit()
    rows = st.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    st.close()
    return n, rows, five_reset, five_used, week_reset, week_used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--home", default=None)
    a = ap.parse_args()
    now = time.time()
    tmp = None
    if a.home:
        home = a.home
        payload = {"session_id": "bench", "rate_limits": {
            "five_hour": {"used_percentage": 40, "resets_at": int(now + 7200)},
            "seven_day": {"used_percentage": 30, "resets_at": int(now + 3 * 86400)}}}
    else:
        tmp = home = tempfile.mkdtemp(prefix="ctempo-bench-")
        t = time.time()
        n, rows, fr, fu, wr, wu = seed(home, now)
        print("seeded %d readings -> %d sample rows in %.1fs" % (n, rows, time.time() - t))
        if fr is None or fr <= now:
            fr, fu = now + 7200, 40.0
        payload = {"session_id": "bench", "rate_limits": {
            "five_hour": {"used_percentage": round(fu, 1), "resets_at": int(fr)},
            "seven_day": {"used_percentage": round(wu, 1), "resets_at": int(wr)}}}
    env = dict(os.environ, CTEMPO_HOME=home)
    argv = [a.python] + winshell.python_flags() + [os.path.join(ROOT, "bin", "ctempo"), "statusline"]  # as installed
    times, out = [], b""
    for i in range(a.runs):
        payload["rate_limits"]["five_hour"]["used_percentage"] += 0.1  # a fresh reading each run
        data = json.dumps(payload).encode()
        t = time.perf_counter()
        p = subprocess.run(argv, input=data, capture_output=True, env=env)
        times.append((time.perf_counter() - t) * 1000)
        out = p.stdout
        if p.returncode != 0:
            print("run %d exited %d" % (i, p.returncode))
    times.sort()

    def pct(q):
        return times[min(len(times) - 1, int(round(q * (len(times) - 1))))]

    print("python: %s" % a.python)
    print("runs: %d  p50 %.1f ms  p95 %.1f ms  max %.1f ms" % (len(times), pct(0.5), pct(0.95), times[-1]))
    print("last line: %s" % out.decode("utf-8", "replace").strip())
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
