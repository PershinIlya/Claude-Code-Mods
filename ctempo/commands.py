"""The on-demand commands: now, report, profile, check, doctor, config.

These fail loud (they are run by hand), unlike the statusline hot path.
"""

import json
import os
import sqlite3
import sys
import time

from . import config, pace, profile, render, weekly
from .config import FIVE_HOUR, SEVEN_DAY
from .store import Store


def _open(create=False):
    try:
        return Store(timeout_ms=2000, create=create)
    except FileNotFoundError:
        return None


def views(st, cfg, now):
    prof = profile.load(st)
    return pace.five_hour(st, now, cfg), weekly.weekly(st, now, cfg, prof=prof), prof


def dur(seconds):
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return "%dd %02dh" % (d, h)
    if h:
        return "%dh %02dm" % (h, m)
    return "%dm" % m


VERDICT_TEXT = {
    "speed_up": "speed up: there is room to go faster",
    "on_pace": "on pace",
    "slow_down": "slow down",
    "idle": "idle",
    "unknown": "not enough data yet",
    "maxed": "limit reached",
    "fresh": "no open window (the next message starts one)",
}


# -- now -------------------------------------------------------------------------------
def cmd_now(args, cfg):
    now = time.time()
    st = _open()
    if st is None:
        print("No data yet: %s does not exist. Is the statusLine installed (ctempo install)?" % config.db_path())
        return 1
    try:
        v5, vw, prof = views(st, cfg, now)
        last = st.db.execute("SELECT MAX(ts), COUNT(*) FROM samples").fetchone()
        ppw = weekly.exchange_rate(st, now)[0]
        if args.json:
            print(json.dumps({"now": now, "five_hour": v5, "seven_day": vw, "exchange_rate": ppw}, indent=2, default=str))
            return 0
        segs = render.segments(v5, vw, cfg)
        print(render.to_ansi(segs, render.use_color(cfg) and sys.stdout.isatty()))
        print()
        _print_5h(v5, cfg)
        print()
        _print_week(vw, ppw)
        print()
        age = now - last[0] if last[0] else None
        print(
            "Data: %d samples, last %s ago. Profile: %s."
            % (last[1], dur(age) if age is not None else "never", vw.get("profile_source", "n/a"))
        )
    finally:
        st.close()
    return 0


def _print_5h(v, cfg):
    print("5-hour window: %s" % VERDICT_TEXT.get(v.get("verdict"), v.get("verdict")))
    if not v.get("present"):
        return
    t = v["target"]
    print("  used           %5.1f%%   (target %.0f%%)" % (v["used"], t))
    print("  resets         %s, in %s" % (render.hm(v["reset_ts"]), dur(v["reset_ts"] - time.time())))
    if v.get("rate") is None:
        print("  burn rate      unknown (fewer than 3 readings over 5 minutes)")
    else:
        how = {"lsq": "least squares over the last %s min" % cfg["rate_window_min"],
               "since_last": "average since the previous reading (too few recent readings)",
               "window_avg": "window average (too few recent readings)"}
        print("  burn rate      %5.1f %%/h  %s" % (v["rate"], how.get(v["rate_source"], "")))
    print("  allowed rate   %5.1f %%/h  = (%.0f - %.1f) / %.2f h" % (v["allowed"], t, v["used"], v["remaining_h"]))
    if v.get("multiplier") is not None:
        m = v["multiplier"]
        print(
            "  multiplier     x%.2f     %s"
            % (m, "you can go %.1fx faster" % m if m >= 1 else "spend at %.0f%% of your current rate" % (m * 100))
        )
    if v.get("projected") is not None:
        if v.get("cap_ts"):
            print("  projection     hits 100%% at %s, %s before the reset" % (render.hm(v["cap_ts"]), dur(v["reset_ts"] - v["cap_ts"])))
        else:
            print("  projection     %5.1f%% at reset, ~%.0f%% would expire" % (v["projected"], v["unused_at_reset"]))


def _print_week(v, ppw):
    print("Weekly window: %s" % VERDICT_TEXT.get(v.get("verdict"), v.get("verdict")))
    if not v.get("present"):
        return
    t = v["target"]
    print("  used           %5.1f%%   (target %.0f%%)" % (v["used"], t))
    print("  resets         %s, in %s" % (render.dhm(v["reset_ts"]), dur(v["reset_ts"] - time.time())))
    print("  profile        %.0f%% of a week's expected spend is still ahead (E = %.3f, %s)" % (v["E"] * 100, v["E"], v["profile_source"]))
    print(
        "  on-pace level  %5.1f%%   you are %+.1f points %s"
        % (v["on_pace"], v["delta"], "ahead (spending faster than your profile)" if v["delta"] > 0 else "behind (surplus)")
    )
    src = {
        "recent": "last %s: spent %.1f pts, profile expected %.1f" % (dur(time.time() - v["lookback_start"]), v["lookback_actual"], v["lookback_expected"]),
        "window": "whole window so far (recent span too short)",
        "typical": "your typical week (no recent data)",
        "assumed": "assumed (no data yet)",
    }
    print("  intensity      %5.2f     %s" % (v["intensity"], src.get(v["intensity_source"], "")))
    if v.get("dry_ts"):
        print("  projection     runs dry ~%s, %s before the reset" % (render.dhm(v["dry_ts"]), dur(v["reset_ts"] - v["dry_ts"])))
    else:
        print("  projection     %5.1f%% at reset, ~%.0f%% will expire" % (v["projected"], v["expire"]))
        if ppw:
            print("                 (~%.1f full 5-hour windows' worth)" % (v["expire"] / ppw))
    if v.get("multiplier") is not None:
        print("  multiplier     x%.2f" % v["multiplier"])
    if ppw:
        print("  exchange rate  one full 5-hour window = %.1f%% of the week" % ppw)


# -- report ----------------------------------------------------------------------------
def cmd_report(args, cfg):
    now = time.time()
    st = _open()
    if st is None:
        print("No data yet.")
        return 1
    try:
        since = now - args.days * 86400
        st.begin()
        st.close_expired(now)
        st.commit()
        ppw = weekly.exchange_rate(st, now, max(28, args.days))[0]
        out = {"days": args.days, "exchange_rate": ppw}
        for w in (SEVEN_DAY, FIVE_HOUR):
            rows = [i for i in st.instances(w, since=since) if i["closed_at"] is not None]
            out[w] = rows
        if args.json:
            print(json.dumps(out, indent=2))
            return 0
        print("Waste ledger, last %d days (unused = 100 - peak observed level)" % args.days)
        print()
        wk = out[SEVEN_DAY]
        print("Weekly windows closed: %d" % len(wk))
        if wk:
            print("  %-18s %6s %7s  %s" % ("reset", "peak", "unused", "note"))
            for i in wk:
                print("  %-18s %5.0f%% %6.0f%%  %s" % (render.dhm(i["reset_ts"]), i["max_used"], i["unused_at_close"] or 0, _note(i, ppw)))
        f = out[FIVE_HOUR]
        print()
        print("5-hour windows closed: %d" % len(f))
        if f:
            peaks = [i["max_used"] for i in f]
            capped = sum(1 for p in peaks if p >= 99.5)
            print("  average peak %.0f%%, average unused %.0f%%, %d hit the limit" % (
                sum(peaks) / len(peaks), sum(100 - p for p in peaks) / len(peaks), capped))
            if args.verbose:
                for i in f:
                    print("  %-18s %5.0f%% %6.0f%%  %s" % (render.dhm(i["reset_ts"]), i["max_used"], i["unused_at_close"] or 0, i["close_reason"]))
        print()
        if wk:
            tot = sum(i["unused_at_close"] or 0 for i in wk)
            line = "Totals: %.0f weekly points unused over %d weeks (avg %.0f%% per week)" % (tot, len(wk), tot / len(wk))
            if ppw:
                line += ", about %.1f full 5-hour windows" % (tot / ppw)
            print(line)
        else:
            print("Totals: no weekly window has closed yet.")
    finally:
        st.close()
    return 0


def _note(i, ppw):
    notes = []
    if i["close_reason"] in ("drop", "shift"):
        notes.append("reset detected by %s" % i["close_reason"])
    if i["last_seen"] < i["reset_ts"] - 3600 and i["close_reason"] != "drop":
        notes.append("last seen %s before reset" % dur(i["reset_ts"] - i["last_seen"]))
    if ppw and i["unused_at_close"]:
        notes.append("~%.1f 5h windows" % (i["unused_at_close"] / ppw))
    return "; ".join(notes)


# -- profile ---------------------------------------------------------------------------
SHADES = " ░▒▓█"


def cmd_profile(args, cfg):
    now = time.time()
    st = _open(create=True)
    try:
        prof = profile.load(st)
        if args.rebuild:
            prof.S, prof.W, prof.T = [0.0] * 168, [0.0] * 168, [0.0] * 168
            prof.processed_until, prof.learned_hours = None, 0
            st.begin()
            n = profile.learn(st, prof, now, cfg, prof.weights(now, cfg)[0])
            profile.save(st, prof)
            st.commit()
            print("Rebuilt from stored samples: %d hours learned." % n)
        w, awake, info = prof.weights(now, cfg)
        if args.json:
            print(json.dumps({"weights": w, "awake": awake, "info": info, "prior_meta": prof.prior_meta}, indent=2))
            return 0
        rows = profile.heatmap_rows(w)
        top = max(max(r) for r in rows) or 1.0
        low = min(min(r) for r in rows)
        flat = top - low < 0.25  # within 25% of uniform everywhere: nothing to show
        print("Hour-of-week profile (%s); darker = larger share of a week's spend" % info["source"])
        print("      " + "".join("%-3d" % h if h % 3 == 0 else "" for h in range(24)))
        for d, r in enumerate(rows):
            cells = "".join("·" if flat else SHADES[min(4, int((x - low) / (top - low) * 4.999))] for x in r)
            print("  %s %s  %4.1f%%" % (profile.DAYS[d], cells, sum(r) / 168 * 100))
        print("  (each cell = 1 hour; day totals at the right; uniform would be 14.3%% a day%s)" % (
            "; flat so far: no hour stands out yet" if flat else ""))
        print("Awake hours per week: %d of 168" % info["awake_hours"])
        if prof.prior_meta:
            m = prof.prior_meta
            print("Prior: transcripts, %s days, %s messages, %s active hours, trusted %.0f%% (full at 14 days)" % (
                m.get("days"), m.get("messages"), m.get("active_hours"), 100 * (m.get("trust") or 0)))
        else:
            print("Prior: none (run `ctempo bootstrap`)")
        print("Learned from usage %%: %d hours folded in, typical week %s" % (
            prof.learned_hours, ("%.0f%% of the limit" % info["weekly_total"]) if info["weekly_total"] else "unknown yet"))
        _surplus(st, cfg, w, now)
    finally:
        st.close()
    return 0


def _surplus(st, cfg, w, now):
    """Where surplus usually sits: the lightest awake hours, and how far
    behind pace past weeks were on each day before their reset."""
    blocks = []
    for d in range(7):
        for h in range(0, 24, 3):
            s = sum(w[d * 24 + h + k] for k in range(3))
            blocks.append((s, d, h))
    awake_blocks = sorted(b for b in blocks if b[0] > 1.5 / 168)[:3]
    if awake_blocks:
        print("Lightest awake blocks: " + ", ".join("%s %02d-%02d" % (profile.DAYS[d], h, h + 3) for _, d, h in awake_blocks))
    closed = [i for i in st.instances(SEVEN_DAY, since=now - 56 * 86400) if i["closed_at"] is not None]
    if not closed:
        print("Surplus by day before reset: needs at least one closed weekly window.")
        return
    tgt = config.target(cfg, SEVEN_DAY)
    sums = [[] for _ in range(7)]
    for i in closed:
        start = i["reset_ts"] - 7 * 86400
        for k in range(7):
            t = i["reset_ts"] - k * 86400
            used = st.max_at(i["id"], t)
            if used is None:
                continue
            on = tgt * profile.share_between(w, start, t)
            sums[k].append(used - on)
    parts = ["%s %+.0f" % ("reset" if k == 0 else "-%dd" % k, sum(v) / len(v)) for k, v in enumerate(sums) if v]
    print("Points vs pace, averaged over %d past weeks: %s" % (len(closed), ", ".join(reversed(parts))))
    print("  (negative = surplus building up; the reset column is what expired)")


# -- check -----------------------------------------------------------------------------
def parse_duration(s):
    s = s.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s and s[-1] in units:
        return float(s[:-1]) * units[s[-1]]
    return float(s)


def parse_tail(spec, cfg):
    tail = {FIVE_HOUR: float(cfg["tail"]["five_hour_min"]) * 60, SEVEN_DAY: float(cfg["tail"]["seven_day_hours"]) * 3600}
    if spec:
        for part in spec.split(","):
            if "=" not in part:
                raise ValueError("bad --tail part %r (want 5h=60m,week=2d)" % part)
            k, v = part.split("=", 1)
            k = k.strip().lower()
            key = FIVE_HOUR if k in ("5h", "five_hour", "session") else SEVEN_DAY if k in ("week", "wk", "7d", "seven_day") else None
            if key is None:
                raise ValueError("unknown window %r in --tail" % k)
            tail[key] = parse_duration(v)
    return tail


def check_result(st, cfg, now, min_free, tail, max_age):
    v5, vw, _ = views(st, cfg, now)
    ppw = weekly.exchange_rate(st, now)[0]
    last = st.db.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
    age = (now - last) if last else None
    reasons = []
    f = {"present": v5.get("present", False)}
    if f["present"]:
        f.update(
            used=v5["used"], resets_at=v5["reset_ts"], seconds_to_reset=v5["reset_ts"] - now,
            free_now=100.0 - v5["used"], rate=v5.get("rate"), projected=v5.get("projected"),
            projected_unused=v5["unused_at_reset"], verdict=v5["verdict"],
            in_tail=(v5["reset_ts"] - now) <= tail[FIVE_HOUR],
        )
    else:
        f.update(free_now=100.0, in_tail=False, projected_unused=None, verdict="fresh")
    k = {"present": vw.get("present", False)}
    if k["present"]:
        k.update(
            used=vw["used"], resets_at=vw["reset_ts"], seconds_to_reset=vw["reset_ts"] - now,
            free_now=100.0 - vw["used"], delta=vw["delta"], intensity=vw["intensity"],
            projected=vw["projected"], projected_unused=vw["expire"], dry_at=vw.get("dry_ts"),
            verdict=vw["verdict"], in_tail=(vw["reset_ts"] - now) <= tail[SEVEN_DAY],
        )
    else:
        k.update(free_now=100.0, in_tail=False, projected_unused=None, verdict="fresh")

    stale = age is None or age > max_age
    if stale:
        reasons.append("no sample in the last %s" % dur(max_age) if age is not None else "no samples at all")
    week_cost = None
    if f["present"] and ppw:
        week_cost = (f["projected_unused"] or 0) * ppw / 100.0
    week_ok = (not k["present"]) or (
        k.get("dry_at") is None and (k["projected"] + (week_cost or 0)) <= config.target(cfg, SEVEN_DAY)
    )
    f["harvestable"] = bool(
        f["present"] and f["in_tail"] and (f["projected_unused"] or 0) >= min_free and f["free_now"] >= min_free and week_ok
    )
    if f["present"] and f["in_tail"] and not week_ok:
        reasons.append("5h leftover would eat into a weekly budget that is already tight")
    # Harvesting weekly surplus must not starve your own 5h window: it needs
    # room now and at the current burn rate.
    five_room = f["free_now"] >= min_free and (not f["present"] or (f["projected_unused"] or 0) >= min_free)
    k["harvestable"] = bool(k["present"] and k["in_tail"] and (k["projected_unused"] or 0) >= min_free and five_room)
    if k["present"] and k["in_tail"] and (k["projected_unused"] or 0) >= min_free and not five_room:
        reasons.append("weekly surplus is there, but the 5h window has no room at the current burn")
    if f["harvestable"]:
        reasons.append("5h window ends in %s with ~%.0f%% projected unused" % (dur(f["seconds_to_reset"]), f["projected_unused"]))
    if k["harvestable"]:
        reasons.append("weekly window ends in %s with ~%.0f%% projected to expire" % (dur(k["seconds_to_reset"]), k["projected_unused"]))
    if not (f["harvestable"] or k["harvestable"]) and not reasons:
        reasons.append("no window is in its tail with at least %.0f%% to spare" % min_free)
    ok = (f["harvestable"] or k["harvestable"]) and not stale
    return {
        "ok": ok,
        "now": now,
        "data_age_s": age,
        "stale": stale,
        "reasons": reasons,
        "five_hour": f,
        "seven_day": k,
        "exchange_rate": ppw,
        "weekly_cost_of_5h_leftover": week_cost,
        "params": {"min_free": min_free, "tail_5h_s": tail[FIVE_HOUR], "tail_week_s": tail[SEVEN_DAY], "max_age_s": max_age},
    }


def cmd_check(args, cfg):
    now = time.time()
    try:
        tail = parse_tail(args.tail, cfg)
        max_age = parse_duration(args.max_age)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    min_free = float(args.min_free if args.min_free is not None else cfg["min_free"])
    st = _open()
    if st is None:
        print(json.dumps({"ok": False, "reasons": ["no database yet"]}))
        return 1
    try:
        res = check_result(st, cfg, now, min_free, tail, max_age)
    finally:
        st.close()
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


# -- bootstrap -------------------------------------------------------------------------
def cmd_bootstrap(args, cfg):
    from . import transcripts

    st = _open(create=True)
    try:
        t = time.time()
        prior, meta, stats = transcripts.bootstrap(st, cfg, days=args.days, root=args.root, dry_run=args.dry_run)
        el = time.time() - t
    finally:
        st.close()
    print("Scanned %d transcript files in %.1fs (%d skipped as older than the period)." % (stats["files"], el, stats["files_skipped_old"]))
    print("Assistant messages counted: %d (%d duplicate lines merged, %d unreadable lines)." % (stats["messages"], stats["duplicates"], stats["bad_lines"]))
    print("Only timestamps, message ids and token counts were read; no content was kept.")
    if prior is None:
        print("No usable messages in the period: the profile prior was not changed.")
        return 1
    print("Period: %s days, %s active hours; prior trusted %.0f%% (full at 14 days, the rest is uniform)." % (
        meta["days"], meta["active_hours"], 100 * meta["trust"]))
    if args.dry_run:
        print("DRY RUN: profile not saved.")
    else:
        print("Saved as the profile prior. See it with `ctempo profile`.")
    return 0


# -- doctor ----------------------------------------------------------------------------
def cmd_doctor(args, cfg):
    from . import install

    now = time.time()
    results = []

    def add(level, what, detail=""):
        results.append((level, what, detail))

    add("ok", "ctempo %s" % _version(), "repo %s" % install.repo_root())
    rt = None
    try:
        with open(config.runtime_path(), encoding="utf-8") as f:
            rt = json.load(f)
    except (OSError, ValueError):
        pass
    if rt:
        ok, ver, sq, ms, note = install.probe(rt.get("python"))
        add("ok" if ok else "fail", "pinned interpreter %s" % rt.get("python"),
            ("Python %s, sqlite %s, %.0f ms startup" % (ver, sq, ms)) if ok else note)
        add("ok" if os.path.exists(rt.get("entry", "")) else "fail", "entry point %s" % rt.get("entry"))
    else:
        add("warn", "no runtime.json", "run `ctempo install --apply` (the mod reads it)")

    sp = install.settings_path(args.settings)
    try:
        obj, raw, _ = install.read_settings(sp)
        sl = obj.get("statusLine")
        cmd = sl.get("command") if isinstance(sl, dict) else None
        if install.is_ours(cmd):
            add("ok", "statusLine runs ctempo", cmd)
            ri = sl.get("refreshInterval")
            add("ok" if ri else "warn", "refreshInterval %s" % ri, "" if ri else "set it to 60 so idle sessions keep updating")
            comp = install.compose_of(cmd)
            if comp:
                add("ok", "composing with your previous statusLine", comp)
        elif cmd:
            add("warn", "statusLine is not ctempo", cmd)
        else:
            add("warn", "no statusLine in %s" % sp)
        if obj.get("disableAllHooks"):
            add("warn", "disableAllHooks is set", "the statusLine and mods do not run")
    except ValueError as e:
        add("fail", "settings unreadable", str(e))

    code, so, se = install.run_claude(["plugin", "list", "--json"]) if install.claude_bin() else (127, "", "")
    if code == 0:
        try:
            plugs = json.loads(so)
        except ValueError:
            plugs = []
        mine = [p for p in plugs if isinstance(p, dict) and str(p.get("id", "")).startswith(install.PLUGIN + "@")]
        if mine:
            p = mine[0]
            add("ok" if p.get("enabled", True) else "warn", "mod %s installed" % p.get("id"),
                "enabled" if p.get("enabled", True) else "disabled")
        else:
            add("warn", "mod not installed", "ctempo install --apply, or claude plugin install %s" % install.PLUGIN_ID)
    else:
        add("warn", "could not list plugins", (se or "claude not on PATH").strip()[:120])

    st = _open()
    if st is None:
        add("warn", "no database yet", config.db_path())
    else:
        try:
            jm = st.db.execute("PRAGMA journal_mode").fetchone()[0]
            qc = st.db.execute("PRAGMA quick_check").fetchone()[0]
            add("ok" if jm == "wal" else "warn", "database %s" % config.db_path(), "journal_mode=%s, quick_check=%s" % (jm, qc))
            counts = st.sample_counts()
            last = st.last_by_source()
            if counts:
                for src, n in sorted(counts.items()):
                    age = now - last[src]
                    lvl = "ok" if age < 3600 else "warn"
                    add(lvl, "samples from %s: %d" % (src, n), "last %s ago" % dur(age))
            else:
                add("warn", "no samples recorded yet", "they appear after the first API response of a session")
            for src in ("statusline", "mod"):
                lr = st.meta_get("last_render:" + src)
                if lr:
                    add("ok", "last %s line (%s ago): %s" % (src, dur(now - lr["ts"]), lr["line"]),
                        "rate_limits received: %s" % json.dumps(lr.get("rate_limits"), separators=(",", ":")))
            kinds = st.kinds()
            if kinds:
                add("ok", "rate-limit kinds seen", ", ".join("%s (%s, %d)" % (k, s, c) for k, s, _, _, c in kinds))
            rej = [(k, v) for k, v in st.db.execute("SELECT key, value FROM meta WHERE key LIKE 'rejected:%'").fetchall()]
            if rej:
                add("ok", "readings rejected by the guards", ", ".join("%s=%s" % (k[9:], v) for k, v in rej))
            prof = profile.load(st)
            if prof.prior:
                m = prof.prior_meta
                add("ok", "profile prior from transcripts", "%s days, %s messages" % (m.get("days"), m.get("messages")))
            else:
                add("warn", "no transcript prior", "run `ctempo bootstrap`")
            add("ok", "profile hours learned from usage %%: %d" % prof.learned_hours)
        except sqlite3.Error as e:
            add("fail", "database error", str(e))
        finally:
            st.close()
    if os.path.exists(config.log_path()):
        with open(config.log_path(), encoding="utf-8", errors="replace") as f:
            n = sum(1 for line in f if line[:2] == "20")
        add("warn" if n else "ok", "errors.log has %d entries" % n, config.log_path())
    if args.json:
        print(json.dumps([{"level": a, "check": b, "detail": c} for a, b, c in results], indent=2))
    else:
        mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
        for lvl, what, detail in results:
            print("%s %s%s" % (mark[lvl], what, ("  (" + detail + ")") if detail else ""))
    return 1 if any(r[0] == "fail" for r in results) else 0


def _version():
    from . import __version__

    return __version__


# -- config ----------------------------------------------------------------------------
def cmd_config(args, cfg):
    if args.action == "path":
        print(config.config_path())
    elif args.action == "init":
        p = config.write_defaults()
        print("Config at %s" % p)
    else:
        print("# %s%s" % (config.config_path(), "" if os.path.exists(config.config_path()) else " (not created; defaults shown)"))
        print(json.dumps(cfg, indent=2))
    return 0
