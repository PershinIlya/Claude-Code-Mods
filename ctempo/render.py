"""The compact line, as ANSI text or as tone-tagged segments for the mod.

  5h 62% ×1.4▲ →88% @13:20 · wk 48% +3 ×1.1 →95% @Thu 09:00

Tones: dim (fine), opportunity (green: spare quota about to expire),
warn (yellow: runs dry before the reset but on pace, or slow down against a
target below 100), alert (red: runs dry with a slow down verdict, or maxed).
"""

import os
import time

SYMBOLS = {
    "unicode": {"x": "×", "up": "▲", "down": "▼", "to": "→", "le": "≤", "sep": " · ", "pm": "±", "cut": "…"},
    "ascii": {"x": "x", "up": "^", "down": "v", "to": "->", "le": "<=", "sep": " | ", "pm": "+", "cut": "..."},
}
ANSI = {
    "dim": "\033[2m",
    "plain": "",
    "opportunity": "\033[32m",
    "warn": "\033[33m",
    "alert": "\033[1;31m",
}
RESET = "\033[0m"


def symbols(cfg):
    return SYMBOLS.get(cfg["display"].get("symbols"), SYMBOLS["unicode"])


def use_color(cfg, env=None):
    env = os.environ if env is None else env
    mode = cfg["display"].get("color", "auto")
    if mode == "never":
        return False
    if mode == "always":
        return True
    return not env.get("NO_COLOR") and env.get("TERM") != "dumb"


def pct(x):
    n = int(round(x))
    if n >= 100 and x < 100:
        n = 99
    return "%d%%" % n


def mult(m):
    if m is None:
        return "?"
    if m >= 9.95:
        return "9+"
    return "%.1f" % m


def rate(r):
    if r is None:
        return "?"
    if r >= 99.5:
        return "99+"
    if r < 9.95:
        return "%.1f" % r if r < 1 else "%.0f" % r
    return "%.0f" % r


def hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def dhm(ts):
    return time.strftime("%a %H:%M", time.localtime(ts))


def _glyph(verdict, s):
    return {"speed_up": s["up"], "slow_down": s["down"]}.get(verdict, "")


def five_hour_parts(v, s):
    """List of (text, droppable) for the 5h segment."""
    if not v.get("present"):
        return [("5h 0%", False)]
    parts = [("5h " + pct(v["used"]), False)]
    verdict = v["verdict"]
    if verdict == "maxed":
        parts.append(("maxed", False))
    elif verdict == "idle":
        parts.append(("idle " + s["le"] + rate(v["allowed"]) + "%/h", False))
    elif verdict == "unknown":
        parts.append((s["le"] + rate(v["allowed"]) + "%/h", False))
    else:
        approx = "~" if v.get("rate_source") != "lsq" else ""
        parts.append((approx + s["x"] + mult(v["multiplier"]) + _glyph(verdict, s), False))
        if v.get("cap_ts"):
            parts.append(("dry " + hm(v["cap_ts"]), False))
        else:
            parts.append((s["to"] + pct(v["projected"]), True))
    parts.append(("@" + hm(v["reset_ts"]), True))
    return parts


def weekly_parts(v, s):
    if not v.get("present"):
        return [("wk 0%", False)]
    d = v["delta"]
    sign = s["pm"] + "0" if abs(d) < 0.5 else ("%+d" % int(round(d)))
    parts = [("wk " + pct(v["used"]), False), (sign, True)]
    verdict = v["verdict"]
    if verdict == "maxed":
        parts.append(("maxed", False))
    elif verdict == "idle":
        parts.append(("idle", False))
        parts.append((s["to"] + pct(v["projected"]), True))
    else:
        parts.append((s["x"] + mult(v["multiplier"]) + _glyph(verdict, s), False))
        if v.get("dry_ts"):
            parts.append(("dry~" + dhm(v["dry_ts"]), False))
        else:
            parts.append((s["to"] + pct(v["projected"]), True))
    parts.append(("@" + dhm(v["reset_ts"]), True))
    return parts


def segments(v5, vw, cfg, width=None):
    """[(text, tone)] fitted to the width; separators are tagged 'dim'."""
    s = symbols(cfg)
    width = int(width or cfg["display"].get("max_width", 80))
    p5, pw = five_hour_parts(v5, s), weekly_parts(vw, s)
    # Drop optional parts, least useful first, until the line fits.
    order = [(p5, len(p5) - 1), (pw, 1), (pw, len(pw) - 1), (p5, len(p5) - 2), (pw, len(pw) - 2)]
    dropped = set()

    def build():
        a = " ".join(t for i, (t, _) in enumerate(p5) if (0, i) not in dropped)
        b = " ".join(t for i, (t, _) in enumerate(pw) if (1, i) not in dropped)
        return a, b

    a, b = build()
    for parts, idx in order:
        if len(a) + len(s["sep"]) + len(b) <= width:
            break
        if 0 <= idx < len(parts) and parts[idx][1]:
            dropped.add((0 if parts is p5 else 1, idx))
            a, b = build()
    segs = [(a, v5.get("severity") or "dim"), (s["sep"], "dim"), (b, vw.get("severity") or "dim")]
    return fit(segs, cfg, width)


def fit(segs, cfg, width=None):
    """Cut [(text, tone)] to the width as a last resort: the segment that
    overflows ends in an ellipsis and keeps its tone, the rest is dropped."""
    width = int(width or cfg["display"].get("max_width", 80))
    if sum(len(t) for t, _ in segs) <= width:
        return segs
    cut = symbols(cfg)["cut"]
    out, room = [], max(0, width - len(cut))
    for text, tone in segs:
        if len(text) > room:
            out.append((text[:room] + cut, tone))
            break
        out.append((text, tone))
        room -= len(text)
    return out


def to_ansi(segs, color):
    if not color:
        return "".join(t for t, _ in segs)
    out = []
    for text, tone in segs:
        code = ANSI.get(tone, "")
        out.append(code + text + RESET if code else text)
    return "".join(out)


def plain(segs):
    return "".join(t for t, _ in segs)


def message(text, cfg, color):
    return to_ansi([(text, "dim")], color)
