"""Bootstrap the profile prior from Claude Code transcripts.

Reads ~/.claude/projects/**/*.jsonl and keeps, per assistant message, only
its timestamp, message id and token counts. Message content is parsed by
json.loads (the line is one JSON object) and dropped on the spot: it is
never stored, printed or copied.

Each message is weighted by a cost proxy using API price ratios relative
to fresh input tokens (DECISIONS.md D-14):
    input + 1.25 * cache_creation + 0.1 * cache_read + 5 * output
Only the distribution over the hours of the week matters for the prior.
"""

import datetime
import json
import os
import time

from . import config, profile

W_INPUT, W_CACHE_CREATE, W_CACHE_READ, W_OUTPUT = 1.0, 1.25, 0.1, 5.0
FULL_TRUST_DAYS = 14.0


def _ts(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _n(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0.0


def cost_weight(usage):
    if not isinstance(usage, dict):
        return 0.0
    return (
        W_INPUT * _n(usage.get("input_tokens"))
        + W_CACHE_CREATE * _n(usage.get("cache_creation_input_tokens"))
        + W_CACHE_READ * _n(usage.get("cache_read_input_tokens"))
        + W_OUTPUT * _n(usage.get("output_tokens"))
    )


def scan(root, since, now):
    """Cost weight per message, deduplicated. Returns (events, stats).

    events: list of (ts, weight). A message streamed over several lines
    shares one message id; the largest usage seen for it wins.
    """
    stats = {"files": 0, "files_skipped_old": 0, "lines": 0, "bad_lines": 0, "messages": 0, "duplicates": 0}
    best = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".jsonl") or fn.startswith("."):
                continue
            path = os.path.join(dirpath, fn)
            try:
                if os.stat(path).st_mtime < since:
                    stats["files_skipped_old"] += 1
                    continue
                f = open(path, "rb")
            except OSError:
                continue
            stats["files"] += 1
            with f:
                for line in f:
                    if b'"assistant"' not in line or b'"usage"' not in line:
                        continue
                    stats["lines"] += 1
                    try:
                        d = json.loads(line)
                    except ValueError:
                        stats["bad_lines"] += 1
                        continue
                    if not isinstance(d, dict) or d.get("type") != "assistant":
                        continue
                    msg = d.get("message")
                    if not isinstance(msg, dict):
                        continue
                    ts = _ts(d.get("timestamp"))
                    weight = cost_weight(msg.get("usage"))
                    key = msg.get("id") or d.get("requestId") or d.get("uuid")
                    del d, msg  # nothing else from the line is kept
                    if ts is None or ts < since or ts > now + 300 or weight <= 0 or not key:
                        continue
                    prev = best.get(key)
                    if prev is not None:
                        stats["duplicates"] += 1
                        if prev[1] >= weight:
                            continue
                    best[key] = (ts, weight)
    stats["messages"] = len(best)
    return list(best.values()), stats


def build_prior(events, since, now, half_life_days):
    """Decay-weighted mean cost per hour-of-week bin over every hour of the period.

    Hours with no activity count as zeros, so a bin's mean is per calendar
    hour, not per active hour (thevibeworks' per-active-day mean runs high).
    """
    if not events:
        return None, {}
    first = min(t for t, _ in events)
    start = profile.hour_start(max(since, first))
    end = profile.hour_start(now)
    acc = {}
    for t, wgt in events:
        hs = profile.hour_start(t)
        acc[hs] = acc.get(hs, 0.0) + wgt
    num = [0.0] * profile.BINS
    den = [0.0] * profile.BINS
    hl = half_life_days * 86400.0
    active = 0
    for s, e, hs, b in profile.segments(start, end):
        if e - s < 3599:  # DST edge or partial: skip fractional hours
            continue
        a = 0.5 ** ((now - hs) / hl)
        v = acc.get(hs, 0.0)
        if v > 0:
            active += 1
        num[b] += a * v
        den[b] += a
    mean = [n / d if d > 0 else 0.0 for n, d in zip(num, den)]
    if sum(mean) <= 0:
        return None, {}
    shape = profile.normalize(profile.smooth(mean))
    # A few days of transcripts do not describe a week: blend toward uniform
    # until two weeks are covered (one hour of data must not become a spike).
    days = (end - start) / 86400.0
    mix = min(1.0, days / FULL_TRUST_DAYS)
    prior = [mix * x + (1 - mix) / profile.BINS for x in shape]
    meta = {
        "built_at": now,
        "period_start": start,
        "period_end": end,
        "days": round(days, 1),
        "trust": round(mix, 3),
        "active_hours": active,
        "messages": len(events),
    }
    return prior, meta


def bootstrap(store, cfg, now=None, days=None, root=None, dry_run=False):
    now = now or time.time()
    days = float(days or cfg["profile"]["bootstrap_days"])
    root = root or os.path.join(config.claude_dir(), "projects")
    since = now - days * 86400
    events, stats = scan(root, since, now)
    prior, meta = build_prior(events, since, now, float(cfg["profile"]["half_life_days"]))
    meta.update({"files": stats["files"], "root": root})
    if prior is not None and not dry_run:
        prof = profile.load(store)
        prof.prior = prior
        prof.prior_meta = meta
        store.begin()
        try:
            profile.save(store, prof)
            store.meta_set("bootstrap", meta)
            store.commit()
        except Exception:
            store.rollback()
            raise
    return prior, meta, stats
