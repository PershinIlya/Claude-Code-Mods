"""What each window shows: a bar, the percent and, only when there is
something to say, a few words. The mod draws it as one row of small bars
(`windows`); the terminal statusLine draws the same as text (`segments`).

  5h ━━━━━╍╍╍╍─ 55%  resets 16:30 · Week ━━━━━╍╍╍╍─ 48%  resets Fri

The bar: what is used, then a forecast to where the window lands at its
reset. `kind` says how the forecast is drawn:

  run     the forecast, plain
  rough   the forecast of an early estimate (drawn fading out)
  dry     it runs out before the reset: the forecast runs to the end, red
  expire  quota that will go unused: dots after the forecast
  maxed   the limit is reached: the whole bar red
  idle    nothing is moving: the used part grey, no forecast
  wait    no reading yet

Words: a window on pace says nothing at all (the bar says it), one with
room to spare says by how much in grey ("Can go 2.7x faster"). A window that
needs you says one short thing in color ("Go 1.4x slower", "Runs out 15:40
· 2x slower", "40% will go unused", "Back at 16:30"); `attention` marks it,
and the other window's percent dims.

Percents are the last reading's, whole and rounded down, as Claude Code's
own usage bar shows them. A 5-hour window that ended with no new one seen
yet reads 0%, "Not started": the next one opens with the next message.
"""

from . import clock, render
from .config import FIVE_HOUR, SEVEN_DAY
from .pace import below_target, over_target
from .render import floor_pct

NAME = {FIVE_HOUR: "5h", SEVEN_DAY: "Week"}
SHORT = {FIVE_HOUR: "5h", SEVEN_DAY: "Wk"}
LONG = {FIVE_HOUR: "5-hour", SEVEN_DAY: "Week"}
ID = {FIVE_HOUR: "h5", SEVEN_DAY: "wk"}

FAR_S = 20 * 3600  # a time further ahead than this names its weekday


def at(ts, now):
    """A time on the clock, with its weekday when it is not within a day."""
    return render.hm(ts) if ts - now < FAR_S else render.dhm(ts)


def day(ts, now):
    """A reset, as short as it can be: 16:30, or Fri."""
    return render.hm(ts) if ts - now < FAR_S else clock.strftime("%a", ts + 30)


def dur(s):
    m = int(round(max(0.0, s) / 60.0))
    if m < 60:
        return "%d min" % m
    if m < 24 * 60:
        h, m = divmod(m, 60)
        return "%d h %d min" % (h, m) if m else "%d h" % h
    d, h = divmod(int(round(m / 60.0)), 24)
    return "%d day%s %d h" % (d, "" if d == 1 else "s", h) if h else "%d day%s" % (d, "" if d == 1 else "s")


def times(m):
    return ("%.1f" % m).rstrip("0").rstrip(".")


# A run-out this close to the reset is on pace: the forecast is not that exact.
ON_PACE_MARGIN_S = {FIVE_HOUR: 10 * 60, SEVEN_DAY: 6 * 3600}
# Early in a week paced from the window alone (no profile, no recent hours to
# go by), one day's work is stretched over the rest: a rough estimate.
EARLY_WEEK_E = 0.75


def _rough(v):
    if v["window"] == FIVE_HOUR:
        return v.get("rate_source") == "interpolated"
    source = v.get("intensity_source")
    return source in ("typical", "assumed") or (source == "window" and (v.get("E") or 0) > EARLY_WEEK_E)


def _out(v):
    """When the window runs out before its reset, or None: not when that is
    within the margin of the reset."""
    out = v.get("cap_ts") if v["window"] == FIVE_HOUR else v.get("dry_ts")
    if out and v.get("reset_ts") and v["reset_ts"] - out <= ON_PACE_MARGIN_S[v["window"]]:
        return None
    return out


def _unused(v):
    left = v.get("unused_at_reset") if v["window"] == FIVE_HOUR else v.get("expire")
    return below_target(left, v.get("target", 100.0))


def bar_kind(v):
    if not v.get("present"):
        return "run" if v.get("raw") else "wait"
    if v["verdict"] == "maxed":
        return "maxed"
    if v["verdict"] == "idle":
        return "idle"
    if _out(v):
        return "dry"
    if _harvest(v):
        return "expire"
    if v.get("projected") is None:
        return "run"
    return "rough" if _rough(v) else "run"


def _harvest(v):
    """Green: quota worth using that will go unused, at least a whole point
    (with min_free 0, a leftover that rounds down to 0% says nothing)."""
    return v.get("severity") == "opportunity" and floor_pct(_unused(v) or 0) >= 1


def _soft(v):
    """It may run out: the estimate is rough, or the overshoot is mild."""
    return _rough(v) or v.get("severity") == "warn"


def phrase(v, now):
    """The words after the percent, or None: {pre, num, post, narrow_pre,
    narrow, tone, attention, calm}. At most one number, drawn bold;
    `narrow_pre` and `narrow` replace `pre` and `post` in a narrow band;
    `calm` words show only when there is room."""
    if not v.get("present"):
        return None
    verdict = v["verdict"]
    if verdict == "maxed":
        when = at(v["reset_ts"], now)
        return _p("Back " if " " in when else "Back at ", when, "", "error", True, narrow_pre="Back ")
    if verdict in ("idle", "unknown") and over_target(v):
        return _p("Over your ", "%d%%" % int(round(v["target"])), " target", "warning", True, narrow_pre="Over ")
    if verdict == "idle":
        return _p("Idle", "", "", "inactive", False)
    out = _out(v)
    if out:
        soft = _soft(v)
        pace = slower(v)
        return _p("May run out " if soft else "Runs out ", at(out, now), (", " + pace[3]) if pace else "",
                  "warning" if soft else "error", True, narrow="", narrow_pre="Out ")
    if verdict == "slow_down":
        pace = slower(v)
        if not pace:
            return _p("Ease off", "", "", "warning", True)
        return _p(pace[0], pace[1], pace[2], "warning", True, narrow_pre=pace[4], narrow=pace[2])
    if _harvest(v):
        return _p("", "%d%%" % floor_pct(_unused(v) or 0), " will go unused", "success", True, narrow=" unused")
    if verdict == "speed_up" and v.get("multiplier") is not None:
        pre, num, post, _, narrow_pre = faster(v)
        return _p(pre, num, post, "inactive", False, calm=True, narrow_pre=narrow_pre)
    return None


def faster(v):
    """How much faster the window can go: (pre, num, post, short, narrow
    pre) of "Can go 2.7x faster", with "about" and halves for an early
    estimate, or words alone for a bit or plenty of room."""
    m = v["multiplier"]
    rough = _rough(v)
    if rough:
        m = round(m * 2) / 2
        if m < 1.5:
            return "Can go a bit faster", "", "", "a bit faster", "A bit faster"
    if m >= 10:
        return "Plenty of room", "", "", "plenty of room", "Plenty of room"
    num = times(m) + "x"
    return ("Can go about " if rough else "Can go "), num, " faster", ("~" if rough else "") + num + " faster", \
        ("~" if rough else "")


def slower(v):
    """How much slower the window must go to land on its target at the
    reset, as faster(): "Go 1.4x slower" (1 / the multiplier), "Pause" from
    10x; None without a multiplier, or when it is close to 1 (under 1.1x, or
    1.5x for an early estimate)."""
    m = v.get("multiplier")
    if m is None:
        return None
    rough = _rough(v)
    if m <= 0.1:
        return "Pause until the reset", "", "", "pause", "Pause"
    f = 1.0 / m
    if rough:
        f = round(f * 2) / 2
    if f < (1.5 if rough else 1.1):
        return None
    num = times(f) + "x"
    return ("Go about " if rough else "Go "), num, " slower", "go " + ("~" if rough else "") + num + " slower", \
        ("~" if rough else "")


def _p(pre, num, post, tone, attention, narrow=None, calm=False, narrow_pre=None):
    return {"pre": pre, "num": num, "post": post, "narrow_pre": pre if narrow_pre is None else narrow_pre,
            "narrow": post if narrow is None else narrow, "tone": tone, "attention": attention, "calm": calm}


def sentence(v, now):
    """The window in words, for the bar's tooltip and Details."""
    name = LONG[v["window"]]
    if not v.get("present"):
        if v.get("raw"):
            used, reset = v["raw"]
            return "%s: %d%% used, resets %s." % (name, floor_pct(used), at(reset, now))
        if v.get("ended_ts"):
            ended = v["ended_ts"]
            when = ("at " + render.hm(ended)) if now - ended < FAR_S else render.dhm(ended)
            return "%s: the last window ended %s; a new one starts with your next message." % (name, when)
        return "%s: waiting for the first reading." % name
    if v["verdict"] == "maxed":
        return "%s: limit reached. Back %s." % (name, _at_words(v["reset_ts"], now))
    reset = render.dhm(v["reset_ts"]) if v["window"] == SEVEN_DAY else at(v["reset_ts"], now)
    head = "%s: %d%% used, resets %s." % (name, floor_pct(v["used"]), reset)
    return head + " " + _verdict_words(v, now)


def _at_words(ts, now):
    when = at(ts, now)
    return ("at " + when) if " " not in when else when


def _verdict_words(v, now):
    verdict = v["verdict"]
    proj = v.get("projected")
    by = "about %d%% by then" % int(round(proj)) if proj is not None else ""
    early = ", early estimate" if _rough(v) else ""
    over = ("Over your %d%% target. " % int(round(v["target"]))) if over_target(v) else ""
    if verdict == "idle":
        if v["window"] == FIVE_HOUR:
            return over + "Idle: up to %s%% an hour is safe." % render.rate(v.get("allowed"))
        return over + "Idle lately, %s." % by
    out = _out(v)
    if out:
        lead = "May run out" if _soft(v) else "Runs out"
        return "%s %s, %s before the reset. %s." % (lead, _at_words(out, now), dur(v["reset_ts"] - out), _ease(v))
    if verdict == "unknown":
        if v["window"] == FIVE_HOUR:
            return over + "No pace yet: up to %s%% an hour is safe." % render.rate(v.get("allowed"))
        return over + "No pace yet."
    if verdict == "slow_down":
        return "Heading for %s, above your %d%% target. %s." % (by, int(round(v.get("target", 100.0))), _ease(v))
    if _harvest(v):
        return "%s: %d%% will go unused. Use it." % (by[0].upper() + by[1:], floor_pct(_unused(v) or 0))
    if verdict == "speed_up" and v.get("multiplier") is not None:
        pre, num, post = faster(v)[:3]
        return "%s%s%s: %s%s." % (pre, num, post, by, early)
    return "On pace, %s%s." % (by, early)


def _ease(v):
    pace = slower(v)
    return (pace[0] + pace[1] + pace[2]) if pace else "Ease off"


def window(v, now):
    """One window, as the mod draws it."""
    w = v.get("window")
    out = {"id": ID[w], "name": NAME[w], "short": SHORT[w], "kind": bar_kind(v)}
    if v.get("present"):
        used = v["used"]
        proj = v.get("projected")
        out.update(
            pct=floor_pct(used), used=round(used, 2),
            projected=None if proj is None else round(max(proj, used), 2),
            reset=day(v["reset_ts"], now), reset_ts=v["reset_ts"],
            phrase=phrase(v, now), sentence=sentence(v, now),
            # A milder run-out: the bar's red forecast fades, as a rough estimate does.
            soft=out["kind"] == "dry" and _soft(v),
        )
    elif v.get("raw"):
        used, reset = v["raw"]
        out.update(pct=floor_pct(used), used=round(used, 2), projected=None, soft=False,
                   reset=day(reset, now), reset_ts=reset, phrase=None, sentence=sentence(v, now))
    elif v.get("ended_ts"):
        # The window ended and no new one is seen yet: it opens with the next message.
        out.update(kind="idle", pct=0, used=0.0, projected=None, reset=None, reset_ts=None, soft=False,
                   phrase=_p("Not started", "", "", "inactive", False, calm=True), sentence=sentence(v, now))
    else:
        out.update(pct=None, used=0.0, projected=None, reset=None, reset_ts=None, soft=False,
                   phrase=None, sentence=sentence(v, now))
    return out


def windows(v5, vw, now):
    a = window(dict(v5, window=FIVE_HOUR), now)
    b = window(dict(vw, window=SEVEN_DAY), now)
    if vw.get("present") and vw["verdict"] == "maxed" and v5.get("present") and v5["verdict"] != "maxed":
        # The week's limit blocks the 5 hours too: no forecast, no "faster".
        a.update(kind="idle", pct=floor_pct(v5["used"]), used=round(v5["used"], 2), projected=None,
                 phrase=None, soft=False,
                 sentence="5-hour: %d%% used, resets %s. Blocked by the weekly limit until %s." % (
                     floor_pct(v5["used"]), at(v5["reset_ts"], now), render.dhm(vw["reset_ts"])))
    return [a, b]


def updated(v5, vw):
    """When a reading last changed: the newest fresh reading of either window."""
    seen = [v.get("last_seen") for v in (v5, vw) if v.get("present") and v.get("last_seen")]
    return max(seen) if seen else None


# The terminal line.


def line(v5, vw, cfg, now, width=None):
    """[(text, tone)] of the line in the configured style (display.style)."""
    if not any(v.get("present") or v.get("raw") for v in (v5, vw)):
        return render.fit([("ctempo: waiting for usage data", "dim")], cfg, width)
    if cfg["display"].get("style") == "classic":
        return render.segments(v5, vw, cfg, width)
    return segments(windows(v5, vw, now), cfg, width)


BAR = {
    "unicode": {"used": "━", "forecast": "╍", "track": "─", "spare": "┈"},
    "ascii": {"used": "#", "forecast": "=", "track": "-", "spare": "."},
}
CELLS = (8, 5)


def bar_cells(win, cells, sym):
    """[(text, tone)] of a text bar `cells` wide."""
    c = BAR["ascii" if sym["x"] == "x" else "unicode"]
    kind = win["kind"]
    if kind == "wait":
        return [(c["track"] * cells, "track")]
    if kind == "maxed":
        return [(c["used"] * cells, "over")]
    u = min(cells, int(round(win["used"] / 100.0 * cells)))
    if win["used"] > 0:
        u = max(u, 1)
    if kind == "idle":
        return [(c["used"] * u, "idle"), (c["track"] * (cells - u), "track")]
    if kind == "dry":
        return [(c["used"] * u, "used"), (c["forecast"] * (cells - u), "over")]
    p = u if win["projected"] is None else max(u, min(cells, int(round(win["projected"] / 100.0 * cells))))
    rest = cells - p
    tail = (c["spare"] * rest, "spare") if kind == "expire" else (c["track"] * rest, "track")
    return [(c["used"] * u, "used"), (c["forecast"] * (p - u), "forecast"), tail]


def segments(ws, cfg, width=None):
    """[(text, tone)] of the terminal line, fitted to the width.

    Optional parts go first: the resets, then the words get short ("2.7x
    faster", "Out 15:40"), then the grey words go, then the bars shrink,
    then Week becomes Wk, then a calm window beside one that needs you
    keeps only its name and percent; a last resort cuts the line."""
    s = render.symbols(cfg)
    width = int(width or cfg["display"].get("max_width", 80))
    if not any(w["pct"] is not None for w in ws):
        return render.fit([("ctempo: waiting for usage data", "dim")], cfg, width)
    focus = any(w.get("phrase") and w["phrase"]["attention"] for w in ws)
    steps = [
        dict(resets=True, wide=True, calm=True, cells=CELLS[0], short=False, bare=False),
        dict(resets=False, wide=True, calm=True, cells=CELLS[0], short=False, bare=False),
        dict(resets=False, wide=False, calm=True, cells=CELLS[0], short=False, bare=False),
        dict(resets=False, wide=False, calm=False, cells=CELLS[0], short=False, bare=False),
        dict(resets=False, wide=False, calm=False, cells=CELLS[1], short=False, bare=False),
        dict(resets=False, wide=False, calm=False, cells=CELLS[1], short=True, bare=False),
        dict(resets=False, wide=False, calm=False, cells=CELLS[1], short=True, bare=True),
    ]
    for step in steps:
        segs = []
        for i, w in enumerate(ws):
            if i:
                segs.append((s["sep"], "dim"))
            segs.extend(_window_segs(w, step, focus, s))
        segs = _merge(segs)
        if sum(len(t) for t, _ in segs) <= width:
            return segs
    return render.fit(segs, cfg, width)


def _window_segs(w, step, focus, s):
    name = w["short"] if step["short"] else w["name"]
    out = [(name + " ", "name")]
    if w["pct"] is None:
        out.append(("waiting", "inactive"))
        return out
    p = w.get("phrase")
    attention = bool(p and p["attention"])
    if not (step["bare"] and focus and not attention):
        out.extend(bar_cells(w, step["cells"], s))
        out.append((" ", "plain"))
    out.append(("%d%%" % w["pct"], "inactive" if focus and not attention else "text"))
    if p and (step["calm"] or not p["calm"]):
        pre, post = (p["pre"], p["post"]) if step["wide"] else (p["narrow_pre"], p["narrow"])
        out.append((" " + pre + p["num"] + post, p["tone"]))
    if step["resets"] and w["reset"] and w["kind"] != "maxed":
        out.append(("  resets " + w["reset"], "inactive"))
    return out


def _merge(segs):
    out = []
    for text, tone in segs:
        if not text:
            continue
        if out and out[-1][1] == tone:
            out[-1] = (out[-1][0] + text, tone)
        else:
            out.append((text, tone))
    return out
