"""Pace math for the 5-hour window, plus helpers shared with the weekly view.

All rates are in percentage points per hour. Times are epoch seconds.
"""

from . import config
from .config import FIVE_HOUR
from .store import FRESH

IDLE_RATE = 0.05  # %/h; below this a fitted slope counts as idle


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


def level_path(fresh, start_ts, used, now):
    """[(ts, level, synthetic)]: running max of fresh readings, starting at
    0 at the window start, ending at the current level."""
    path = []
    run = None
    for ts, u in fresh:
        run = u if run is None else max(run, u)
        path.append((ts, run, False))
    if not path or path[0][0] > start_ts + 1:
        path.insert(0, (start_ts, 0.0, True))
    if path[-1][1] < used:  # the level is known now (no fresh row yet, or the write failed)
        path.append((now, used, True))
    return path


def level_at(path, t):
    """Piecewise-linear level at t; flat after the last point and before the first."""
    if t <= path[0][0]:
        return path[0][1]
    for (t0, y0, _), (t1, y1, _) in zip(path, path[1:]):
        if t <= t1:
            return y0 if t1 <= t0 else y0 + (y1 - y0) * (t - t0) / (t1 - t0)
    return path[-1][1]


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

    # The level path: fresh readings only (a heartbeat repeats a session's
    # last API response and says nothing about the account's level), as a
    # running max, through 0 at the window start, linear between readings
    # (spend in an unobserved gap is spread over it, not squeezed into the
    # moment we looked) and flat after the last one (no news = no spend,
    # so a finished burst fades out). The rate is the least-squares slope of
    # that path over the trailing window.
    win_s = float(cfg["rate_window_min"]) * 60.0
    min_span = float(cfg["rate_min_span_min"]) * 60
    path = level_path(store.fresh_levels(inst["id"]), inst["start_ts"], used, now)
    since = max(now - win_s, inst["start_ts"])
    rate, source, inside = None, None, []
    if now - since >= min_span:
        inside = [p for p in path if since < p[0] < now]
        points = [(since, level_at(path, since))] + [(t, y) for t, y, _ in inside] + [(now, level_at(path, now))]
        s = lsq_slope(points)
        if s is not None:
            # A fit carried by a few readings and interpolation is marked ~.
            real = [p for p in path if since <= p[0] <= now and not p[2]]
            enough = len(real) >= int(cfg["rate_min_samples"]) and now - real[0][0] >= min_span if real else False
            rate, source = max(0.0, s), "lsq" if enough else "interpolated"
    v["rate"] = rate
    v["rate_source"] = source
    v["rate_points"] = len(inside) + 2

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
