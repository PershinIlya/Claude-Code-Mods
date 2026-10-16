"""Pace math for the 7-day window, driven by the hour-of-week profile.

  E          = expected share of weekly spend still to come (now -> reset)
  on_pace    = target * (1 - E)                     (after a drop reset, over
               the span since the drop: target * (1 - E / span))
  delta      = used - on_pace                       (+ ahead, - behind)
  intensity  = actual spend over the last N active hours
               / (target * profile share of those hours, blocked ones left out)
  projected  = used + intensity * target * E        (capped at 100)
  multiplier = max(0, target - used) / (intensity * target * E)

`dry` is where the hour-by-hour walk at that intensity crosses 100%.
"""

from . import config, profile
from .config import DURATION, FIVE_HOUR, SEVEN_DAY
from .pace import below_target, current_used, over_target, spare, verdict_for
from .store import FIRST, FRESH

MIN_EXPECTED_SHARE = 0.01  # the lookback must cover at least 1% of a week's expected spend
MIN_WINDOW_SHARE = 0.02


def _start_level(store, wid, a, w, max_gap):
    """(start, level) to count a window instance's spend from: `a`, or later.

    A FIRST reading is only a lower bound (D-06): it raises the level but
    never fixes the start, so with no FRESH reading at or before `a` the
    count starts at the first FRESH one. When `a` falls in a gap between
    readings (heartbeats say nothing about the level) longer than max_gap,
    the gap's rise is spread over it by profile share, as learning does
    (D-13): only its expected part falls after `a`. None when the instance
    has no FRESH reading yet.
    """
    first = store.db.execute("SELECT MIN(ts) FROM samples WHERE window_id=? AND flag=?", (wid, FRESH)).fetchone()[0]
    if first is None:
        return None
    if first > a:
        return first, store.max_at(wid, first)
    v0 = store.max_at(wid, a)
    last = store.db.execute(
        "SELECT MAX(ts) FROM samples WHERE window_id=? AND flag IN (?, ?) AND ts<=?", (wid, FRESH, FIRST, a)
    ).fetchone()[0]
    nxt = store.db.execute(
        "SELECT ts, used_pct FROM samples WHERE window_id=? AND flag IN (?, ?) AND ts>? ORDER BY ts LIMIT 1",
        (wid, FRESH, FIRST, a),
    ).fetchone()
    if nxt is not None and nxt[0] - last > max_gap and nxt[1] > v0:
        whole = profile.share_between(w, last, nxt[0])
        if whole > 0:
            v0 += (nxt[1] - v0) * profile.share_between(w, last, a) / whole
    return a, v0


def spend_since(store, inst, t0, now, used, w, max_gap):
    """Points spent between t0 (or the earliest known point after it) and now.

    Walks back into the previous weekly window when the lookback crosses a
    reset. Returns (points, effective_start, cut, capped), cut when the count
    had to start at a first FRESH reading after the lookback start, capped
    the span (lo, hi) of the previous window after it reached 100%: nothing
    could be spent there, so it is no expected spend either.
    """
    a = max(t0, inst["start_ts"])
    cut, capped = False, (now, now)
    if a <= inst["start_ts"]:
        v0 = 0.0  # a weekly window starts empty
    else:
        k = _start_level(store, inst["id"], a, w, max_gap)
        if k is None:
            return 0.0, now, True, capped
        cut = k[0] > a
        a, v0 = k
    actual = max(0.0, used - v0)
    if t0 < inst["start_ts"] and a <= inst["start_ts"]:
        # Not a window that never ended ('shift'), nor one a drop reset
        # replaced ('drop', D-10): the window that replaced it ends at the
        # same reset, give or take the seconds resets_at wobbles (D-08), and
        # holds the spend since the drop. Which one was closer was chance.
        prev = store.db.execute(
            "SELECT id, start_ts, max_used FROM windows WHERE window=? AND id!=? "
            "AND ABS(reset_ts - ?) <= 3600 AND IFNULL(close_reason, '') NOT IN ('shift', 'drop') "
            "ORDER BY ABS(reset_ts - ?), id DESC LIMIT 1",
            (SEVEN_DAY, inst["id"], inst["start_ts"], inst["start_ts"]),
        ).fetchone()
        if prev is not None:
            a2 = max(t0, prev[1])
            k = (a2, 0.0) if a2 <= prev[1] else _start_level(store, prev[0], a2, w, max_gap)
            if k is not None:
                actual += max(0.0, prev[2] - k[1])
                a, cut = k[0], k[0] > a2
                if prev[2] >= 100.0:
                    # Blocked from the cap to the reset. A FIRST reading at
                    # 100 still bounds the moment it got there.
                    cap = store.db.execute(
                        "SELECT MIN(ts) FROM samples WHERE window_id=? AND flag IN (?, ?) AND used_pct>=100",
                        (prev[0], FRESH, FIRST),
                    ).fetchone()[0]
                    if cap is not None:
                        capped = (max(a, cap), inst["start_ts"])
    return actual, a, cut, capped


def exchange_rate(store, now, days=28):
    """Weekly points per full 5-hour window, from paired readings.

    thevibeworks' `pct_per_window`: accumulate the rise of both windows over
    consecutive paired readings within the same instances; publish once the
    5h side has risen at least 50 points and the weekly side 3.
    """
    rows = store.db.execute(
        "SELECT ts, window, used_pct, window_id FROM samples WHERE flag IN (?, ?) AND ts>? ORDER BY ts, id",
        (FRESH, FIRST, now - days * 86400),
    ).fetchall()
    by_ts = {}
    for ts, w, used, wid in rows:
        by_ts.setdefault(round(ts, 3), {})[w] = (used, wid)
    rf = rs = 0.0
    prev = None
    for ts in sorted(by_ts):
        pair = by_ts[ts]
        if FIVE_HOUR not in pair or SEVEN_DAY not in pair:
            continue
        if prev is not None:
            (f0, fw0), (s0, sw0) = prev[FIVE_HOUR], prev[SEVEN_DAY]
            (f1, fw1), (s1, sw1) = pair[FIVE_HOUR], pair[SEVEN_DAY]
            if fw0 == fw1 and sw0 == sw1 and f1 > f0 and s1 >= s0:
                rf += f1 - f0
                rs += s1 - s0
        prev = pair
    if rf >= 50 and rs >= 3:
        return max(1.0, min(50.0, rs / rf * 100.0)), rf, rs
    return None, rf, rs


def weekly(store, now, cfg, reading=None, prof=None):
    v = {"window": SEVEN_DAY, "present": False}
    inst = store.open_instance(SEVEN_DAY)
    if inst is None or inst["reset_ts"] <= now:
        v["verdict"] = "fresh"
        v["severity"] = "dim"
        if reading:  # the write failed before it opened this window: show its level, no math
            v["raw"] = reading
        return v
    tgt = config.target(cfg, SEVEN_DAY)
    used = current_used(store, inst, reading)
    reset = inst["reset_ts"]
    week_start = reset - DURATION[SEVEN_DAY]
    start = max(week_start, inst["start_ts"])  # a drop reset opens the window late (D-10)
    prof = prof or profile.load(store)
    w, awake, info = profile.current_weights(store, prof, now, cfg)

    elapsed = profile.share_between(w, week_start, now)
    left = profile.share_between(w, now, reset)
    total = elapsed + left or 1.0
    E = left / total  # DST weeks have 167 or 169 local hours
    elapsed = profile.share_between(w, start, now) / total  # since this instance opened
    span = elapsed + E  # the instance's own share of the week: 1 unless a drop opened it
    on_pace = tgt * (1.0 - E / span) if start > week_start and span > 0 else tgt * (1.0 - E)
    v.update(present=True, used=used, reset_ts=reset, start_ts=start, remaining_h=(reset - now) / 3600.0,
             target=tgt, window_id=inst["id"], E=E, on_pace=on_pace, delta=used - on_pace,
             profile_source=info["source"], awake_hours=info["awake_hours"], weekly_typical=info["weekly_total"],
             last_seen=inst["last_seen"])

    # Intensity over the last N awake hours (the lookback may cross a reset).
    hours = float(cfg["profile"]["active_hours_lookback"])
    t0 = profile.active_lookback(awake, now, hours, 7 * 86400)
    max_gap = float(cfg["profile"]["max_gap_hours"]) * profile.HOUR
    actual, t_eff, cut, capped = spend_since(store, inst, t0, now, used, w, max_gap)
    share = (profile.share_between(w, t_eff, now) - profile.share_between(w, *capped)) / total
    intensity, source = None, None
    # Readings are whole percents: a lookback cut short at the first reading
    # (install, first sighting late in the window) that has not seen a step
    # yet knows nothing, so its zero is not "idle"; nor is the whole window's
    # while it is still at 0 (the typical week stands in).
    if share >= MIN_EXPECTED_SHARE and t_eff < now and not (cut and actual <= 0):
        intensity, source = actual / (tgt * share), "recent"
    elif elapsed >= MIN_WINDOW_SHARE and not (cut and used <= 0):
        intensity, source = used / (tgt * elapsed), "window"
    elif info["weekly_total"]:
        intensity, source = info["weekly_total"] / tgt, "typical"
    else:
        intensity, source = 1.0, "assumed"
    v.update(intensity=intensity, intensity_source=source, lookback_start=t_eff, lookback_cut=cut,
             lookback_actual=actual, lookback_expected=tgt * share)

    remaining_spend = intensity * tgt * E
    raw = used + remaining_spend
    dry = None
    if raw >= 100.0 and used < 100.0:
        dry = profile.walk_to(w, now, reset, intensity * tgt / total, 100.0 - used)
    # Past a target below 100 there is nothing left to allow: 0, as for 5h.
    mult = max(0.0, tgt - used) / remaining_spend if remaining_spend > 1e-9 else None
    v.update(projected=min(100.0, raw), projected_raw=raw, dry_ts=dry, multiplier=mult,
             expire=max(0.0, 100.0 - min(100.0, raw)), unused_at_reset=max(0.0, 100.0 - min(100.0, raw)))
    if used >= 100.0:
        v["verdict"] = "maxed"
    elif source == "recent" and actual <= 0:
        v["verdict"] = "idle"
    else:
        v["verdict"] = verdict_for(mult, cfg)
    v["in_tail"] = (reset - now) <= float(cfg["tail"]["seven_day_hours"]) * 3600
    v["severity"] = _severity(v, cfg)
    return v


def _severity(v, cfg):
    # The 5h tiers (pace.severity): red needs a dry time and a slow down
    # verdict; at or above a target below 100 is yellow, idle included; green
    # counts only what expires below the target, as `check` does.
    dry = v.get("dry_ts") is not None
    if v["verdict"] == "maxed" or (dry and v["verdict"] == "slow_down"):
        return "alert"
    if dry or v["verdict"] == "slow_down" or over_target(v):
        return "warn"
    if v.get("in_tail") and spare(below_target(v.get("expire"), v.get("target", 100.0)), cfg["min_free"]):
        return "opportunity"
    return "dim"
