"""Pace math for the 5-hour window, plus helpers shared with the weekly view.

All rates are in percentage points per hour. Times are epoch seconds.
"""

from . import config
from .config import FIVE_HOUR
from .store import FRESH

IDLE_RATE = 0.05  # %/h; below this a fitted slope counts as idle
ANCHOR_S = 300.0  # a reading this close before the fit window can anchor it


def lsq_slope(points):
    """Ordinary least squares slope of (t, y) points, per hour. None if degenerate."""
    n = len(points)
    if n < 2:
        return None
    t0 = points[0][0]
    xs = [(t - t0) / 3600.0 for t, _ in points]
    ys = [y for _, y in points]
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def running_series(rows, start_value=None):
    """(ts, used, flag) rows -> (ts, running max of FRESH values).

    Heartbeats contribute their timestamp only: they prove time passed, and
    their value may be an idle session's stale reading.
    """
    out = []
    cur = start_value
    for ts, used, flag in rows:
        if flag == FRESH:
            cur = used if cur is None else max(cur, used)
        elif cur is None:
            cur = used
        out.append((ts, cur))
    return out


def verdict_for(mult, cfg):
    th = cfg["verdict"]
    if mult is None:
        return "unknown"
    if mult >= float(th["speed_up"]):
        return "speed_up"
    if mult < float(th["slow_down"]):
        return "slow_down"
    return "on_pace"


def current_used(store, inst, reading):
    """The window's level: the stored running max, or this payload's reading
    if the write was skipped and it is newer and higher."""
    used = inst["max_used"]
    if reading and abs(reading[1] - inst["reset_ts"]) <= 300:
        used = max(used, reading[0])
    return used


def five_hour(store, now, cfg, reading=None):
    """Everything known about the current 5-hour window, as a dict."""
    v = {"window": FIVE_HOUR, "present": False}
    inst = store.open_instance(FIVE_HOUR)
    if inst is None or inst["reset_ts"] <= now:
        v["verdict"] = "fresh"
        v["severity"] = "dim"
        return v
    tgt = config.target(cfg, FIVE_HOUR)
    used = current_used(store, inst, reading)
    reset = inst["reset_ts"]
    rem_h = max(0.0, (reset - now) / 3600.0)
    v.update(present=True, used=used, reset_ts=reset, start_ts=inst["start_ts"], remaining_h=rem_h,
             target=tgt, window_id=inst["id"])

    # Trailing least-squares rate over the running max. The window's value
    # just before the fit window anchors its left edge, but only when it was
    # read within ANCHOR_S of that edge (an older value would pour usage from
    # an unobserved gap into the last 20 minutes); "now" anchors the right
    # edge, so a burst that stopped long ago fades out as time passes.
    win_s = float(cfg["rate_window_min"]) * 60.0
    since = now - win_s
    rows = store.instance_series(inst["id"], since=since)
    before, before_ts = store.state_at(inst["id"], since)
    if before_ts is not None and since - before_ts > ANCHOR_S:
        before = None
    series = running_series(rows, before)
    points = []
    if before is not None and (not series or series[0][0] > since + 1):
        points.append((since, before))
    points.extend(series)
    real = len(series)
    if not series or now - series[-1][0] > 1.0:  # the reading may already be stored at `now`
        points.append((now, used))
        real += 1
    span = (now - series[0][0]) if series else 0.0
    min_span = float(cfg["rate_min_span_min"]) * 60
    rate, source = None, None
    if real >= int(cfg["rate_min_samples"]) and span >= min_span:
        s = lsq_slope(points)
        if s is not None:
            rate, source = max(0.0, s), "lsq"
    if rate is None:
        # Not enough recent readings: the average since the last reading
        # before now (what changed since we last looked), else since the
        # window opened (it opened at 0).
        # The latest reading at least min_span old: a reading taken a minute
        # ago must not hide the flat one from before an idle gap.
        prev, prev_ts = store.state_at(inst["id"], now - min_span)
        if prev_ts is not None:
            rate, source = max(0.0, used - prev) / ((now - prev_ts) / 3600.0), "since_last"
    if rate is None:
        elapsed = now - max(inst["start_ts"], reset - config.DURATION[FIVE_HOUR])
        if elapsed >= min_span:
            rate, source = used / (elapsed / 3600.0), "window_avg"
    v["rate"] = rate
    v["rate_source"] = source
    v["rate_points"] = len(points)

    allowed = max(0.0, tgt - used) / rem_h if rem_h > 0 else 0.0
    v["allowed"] = allowed
    v["idle"] = rate is not None and rate < IDLE_RATE

    if rate is None:
        v.update(multiplier=None, projected=None, projected_raw=None, cap_ts=None)
        v["verdict"] = "unknown"
    elif v["idle"]:
        v.update(multiplier=None, projected=used, projected_raw=used, cap_ts=None)
        v["verdict"] = "idle"
    else:
        raw = used + rate * rem_h
        cap_ts = None
        if raw >= 100.0 and used < 100.0:
            cap_ts = now + (100.0 - used) / rate * 3600.0
        mult = allowed / rate if rate > 0 else None
        v.update(multiplier=mult, projected=min(100.0, raw), projected_raw=raw, cap_ts=cap_ts)
        v["verdict"] = verdict_for(mult, cfg)
    if used >= 100.0:
        v["verdict"] = "maxed"
    proj = v["projected"] if v["projected"] is not None else used
    v["unused_at_reset"] = max(0.0, 100.0 - proj)
    v["in_tail"] = (reset - now) <= float(cfg["tail"]["five_hour_min"]) * 60
    v["severity"] = severity(v, cfg)
    return v


def severity(v, cfg):
    """dim (fine), opportunity (spare quota about to expire), warn, alert."""
    if not v.get("present"):
        return "dim"
    if v["verdict"] == "maxed" or v.get("cap_ts") is not None:
        return "alert"
    if v["verdict"] == "slow_down":
        return "warn"
    if v.get("in_tail") and v.get("unused_at_reset", 0) >= float(cfg["min_free"]):
        return "opportunity"
    return "dim"
