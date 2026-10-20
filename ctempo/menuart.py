"""The band's bars as pictures for the macOS menu bar (SwiftBar `image=`).

The same language as the Desktop band (mods/ctempo/hooks/register.tsx,
barSvg): orange for what is used, a pale orange forecast to the reset after
a hairline gap (fading while the estimate is rough), red into a wall when it
runs out, green dots over quota that will go unused, all red when reached,
grey when idle. Drawn at twice the point size; SwiftBar is told the size in
points (width=, height=), so they are sharp on a Retina screen.
"""

import base64

from .png import Canvas, hex_color

S = 2  # pixels per point
PALETTE = {
    False: {"t": "#8a867c", "a": "#d97757", "r": "#d0564c", "g": "#5b9c64"},  # light
    True: {"t": "#b4afa4", "a": "#d97757", "r": "#e5695e", "g": "#74b97c"},  # dark
}


def _draw(c, w, x, y, width, h, dark, dots=True):
    """One bar of the window `w` (band.window) in the box (x, y, width, h) in points."""
    p = PALETTE[dark]
    r = h / 2.0
    X = lambda pct: x + max(0.0, min(100.0, pct)) / 100.0 * width  # noqa: E731
    box = lambda x0, x1, color, fade=None: c.round_rect(  # noqa: E731
        x0 * S, y * S, x1 * S, (y + h) * S, r * S, color, fade)
    box(x, x + width, hex_color(p["t"], 0.24))
    kind = w["kind"]
    if kind == "wait" or w.get("pct") is None:
        return
    if kind == "maxed":
        box(x, x + width, hex_color(p["r"]))
        return
    u = X(w["used"])
    gap = max(u, x + h) + 1.0 if w["used"] > 0 else x
    if kind == "dry":
        if x + width - gap >= h:
            fade = None
            if w.get("soft"):
                lo, hi = gap * S, (x + width) * S
                fade = lambda px: 1.0 - 0.8 * (px - lo) / max(1.0, hi - lo)  # noqa: E731
            box(gap, x + width, hex_color(p["r"], 0.45), fade)
        c.round_rect((x + width - 1.2) * S, (y - 2) * S, (x + width) * S, (y + h + 2) * S, 0.6 * S,
                     hex_color(p["r"], 0.5 if w.get("soft") else 1.0))
    elif kind != "idle" and w.get("projected") is not None:
        end = X(w["projected"])
        if end - gap >= h:
            fade = None
            if kind == "rough":
                lo, hi = gap * S, end * S
                fade = lambda px: 1.0 - 0.9 * (px - lo) / max(1.0, hi - lo)  # noqa: E731
            box(gap, end, hex_color(p["a"], 0.36), fade)
        if kind == "expire" and dots:
            step = 3.5
            cx = end + 3.0
            while cx < x + width - 1.5:
                c.dot(cx * S, (y + h / 2.0) * S, 0.9 * S, hex_color(p["g"]))
                cx += step
    if w["used"] > 0:
        box(x, max(u, x + h), hex_color(p["t"], 0.7) if kind == "idle" else hex_color(p["a"]))


def bar(w, width, height, dark, pad=0):
    """A window's bar, `width` x `height` points, as base64 PNG; `pad` points
    on the right stay clear, room before the text beside it."""
    bar_h = 5.0
    c = Canvas(width * S, height * S)
    _draw(c, w, 0.5, (height - bar_h) / 2.0, width - 1.5 - pad, bar_h, dark)
    return base64.b64encode(c.png()).decode("ascii")


def icon(ws, dark, width=24, height=18):
    """Both windows stacked, 5 hours on top, for the menu bar title."""
    c = Canvas(width * S, height * S)
    bar_h, gap = 4.0, 3.0
    top = (height - 2 * bar_h - gap) / 2.0
    for i, w in enumerate(ws[:2]):
        _draw(c, w, 0.5, top + i * (bar_h + gap), width - 1.5, bar_h, dark, dots=False)
    return base64.b64encode(c.png()).decode("ascii")
