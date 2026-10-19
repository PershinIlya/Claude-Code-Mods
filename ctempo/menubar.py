"""`ctempo menubar`: the band in the macOS menu bar, through SwiftBar.

SwiftBar (a free menu bar app) runs a plugin script every minute and shows
what it prints: the first line in the menu bar, the lines after `---` in the
menu that opens on a click. The script `ctempo menubar --install` writes
runs `ctempo menubar`, which reads the store and prints

  [two small bars] 22% · 18%             (5 hours on top; red, yellow or green words
  ---                                       when a window needs you)
  [bar] 5-hour   22%                       (each window's bar as a picture, as the
        77% will go unused · resets 12:00   Desktop band draws it, its words and reset)
  ...
  Updated 2 min ago

It records nothing: the numbers are the ones Claude Code sessions on this Mac
(the status line, the Desktop mod) last stored, and "Updated" says how old.
"""

import os
import shlex
import subprocess
import sys
import time

from . import band, config, render

PLUGIN = "ctempo.1m.sh"  # SwiftBar reads the refresh interval from the name
MARK = "# ctempo menu bar plugin"
STALE_S = 30 * 60
TONE = {  # SwiftBar takes "light,dark"
    "error": "#c2453b,#ec6a5e",
    "warning": "#a86b12,#e0b252",
    "success": "#3f7d47,#7cbf83",
}
DIM = "#8a867c,#a3a19a"
RANK = ("success", "warning", "error")


def lines(ws, updated, now, cfg, checked=None, note=None):
    """SwiftBar's output for the windows: the title, `---`, the menu.
    `checked`: when /usage last answered (usagecmd); `note`: why it did not.
    menubar.style "bars" (the default) draws the bars as pictures; "text"
    is the plain text of earlier versions."""
    if cfg.get("menubar", {}).get("style", "bars") == "text":
        return text_lines(ws, updated, now, cfg, checked, note)
    from . import menuart

    waiting = all(w["pct"] is None for w in ws)
    newest = max([t for t in (updated, checked) if t is not None] or [0])
    stale = not newest or now - newest > STALE_S
    parts, tones = [], []
    for w in ws:
        if w["pct"] is None:
            parts.append("-")
            continue
        part = "%d%%" % w["pct"]
        p = w.get("phrase")
        if p and p["attention"]:
            if p["tone"] != "success":  # green says it by color; its words are in the menu
                part += " " + p["narrow_pre"] + p["num"] + p["narrow"]
            tones.append(p["tone"])
        parts.append(part)
    worst = max(tones, key=RANK.index) if tones else None
    color = TONE[worst] if worst else (DIM if stale or waiting else None)
    pic = "image=%s,%s width=24 height=18" % (menuart.icon(ws, False), menuart.icon(ws, True))
    title = ("" if waiting else " · ".join(parts)) + " | " + pic + (" color=" + color if color else "")
    out = [title, "---"]
    if waiting:
        out.append("Waiting for the first reading: start a Claude Code session | size=12")
    for w in ws if not waiting else []:
        name = "5-hour" if w["id"] == "h5" else "Week"
        img = "image=%s,%s width=120 height=14" % (menuart.bar(w, 120, 14, False), menuart.bar(w, 120, 14, True))
        head = "%s   %s" % (name, "waiting" if w["pct"] is None else "%d%%" % w["pct"])
        out.append("%s | %s size=13 tooltip=%s" % (head, img, w["sentence"].replace("|", "/")))
        out.append("%s | size=11 color=%s" % (summary(w), TONE.get((w.get("phrase") or {}).get("tone"), DIM)))
    out.append("---")
    times = []
    if updated is not None:
        times.append(age(now - updated))
    if checked is not None:
        times.append("checked " + age(now - checked)[8:])
    if times:
        out.append("%s | size=11 color=%s" % (", ".join(times), DIM))
    if note:
        out.append("%s | size=11 color=%s" % (note, TONE["warning"]))
    if stale and not waiting and checked is None:
        out.append("Readings come from Claude Code sessions on this Mac | size=11 color=%s" % DIM)
    out.append("Refresh | refresh=true")
    return out


# Between a window's words and its reset in the menu: 2 em spaces, a gap
# wider than the words' own spaces (SwiftBar keeps inner spaces as they are).
GAP = "\u2003\u2003"


def summary(w):
    """The second line of a window in the menu: its words, and the reset."""
    if w["pct"] is None:
        return w["sentence"]
    p = w.get("phrase")
    words = (p["pre"] + p["num"] + p["post"]) if p else ("Limit reached" if w["kind"] == "maxed" else "On pace")
    if w["kind"] == "maxed" or not w.get("reset"):
        return words
    return "%s%sresets %s" % (words, GAP, w["reset"])


def text_lines(ws, updated, now, cfg, checked=None, note=None):
    """The plain text menu (menubar.style "text")."""
    title_parts, tones = [], []
    for w in ws:
        short = "5h" if w["id"] == "h5" else "Wk"
        if w["pct"] is None:
            title_parts.append(short + " -")
            continue
        part = "%s %d%%" % (short, w["pct"])
        p = w.get("phrase")
        if p and p["attention"]:
            part += " " + p["narrow_pre"] + p["num"] + p["narrow"]
            tones.append(p["tone"])
        title_parts.append(part)
    waiting = all(w["pct"] is None for w in ws)
    newest = max([t for t in (updated, checked) if t is not None] or [0])
    stale = not newest or now - newest > STALE_S
    if waiting:
        title = "ctempo | color=%s" % DIM
    else:
        title = " · ".join(title_parts)
        worst = max(tones, key=RANK.index) if tones else None
        if worst:
            title += " | color=%s" % TONE[worst]
        elif stale:
            title += " | color=%s" % DIM
    out = [title, "---"]
    if waiting:
        out.append("Waiting for the first reading: start a Claude Code session | size=12")
    else:
        for w in ws:
            text = render.plain(band.segments([w], cfg, 60)) if w["pct"] is not None else w["name"] + " waiting"
            out.append("%s | font=Menlo size=12 trim=false" % text)
            out.append("%s | size=11 color=%s" % (w["sentence"], DIM))
    out.append("---")
    if updated is not None:
        out.append("%s | size=11 color=%s" % (age(now - updated), DIM))
    if checked is not None:
        out.append("Checked with /usage %s | size=11 color=%s" % (age(now - checked)[8:], DIM))
    if note:
        out.append("%s | size=11 color=%s" % (note, TONE["warning"]))
    if stale and not waiting and checked is None:
        out.append("Readings come from Claude Code sessions on this Mac | size=11 color=%s" % DIM)
    out.append("Refresh | refresh=true")
    return out


def age(s):
    m = int(max(0, s) // 60)
    if m < 1:
        return "Updated just now"
    if m < 60:
        return "Updated %d min ago" % m
    h = int(round(m / 60.0))
    return "Updated %d h ago" % h if h < 48 else "Updated %d days ago" % int(round(h / 24.0))


def show(cfg):
    from .commands import _open, views

    from . import usagecmd

    now = time.time()
    note = None
    try:
        every = usagecmd.every(cfg)
        st = _open(create=bool(every))
        if st is not None and usagecmd.due(st, now, every):
            st.close()
            ok, msg = usagecmd.run_now(now)
            note = None if ok else "/usage: " + msg
            st = _open()
        checked = None
        if st is None:
            ws, updated = band.windows({}, {}, now), None
        else:
            try:
                v5, vw, _ = views(st, cfg, now)
                checked = st.meta_get("usage_cmd_ok_at") if every else None
                ws, updated = band.windows(v5, vw, now), band.updated(v5, vw)
                if cfg.get("menubar", {}).get("notify", True):
                    notify(st, ws, max([t for t in (updated, checked) if t] or [0]), now)
            finally:
                st.close()
        out = lines(ws, updated, now, cfg, checked, note)
    except Exception as e:  # the menu bar shows why instead of nothing
        out = ["ctempo ! | color=%s" % TONE["warning"], "---", "ctempo: %s | size=12" % str(e)[:200],
               "Refresh | refresh=true"]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


# -- notifications ----------------------------------------------------------------------

def events(ws):
    """[(key, title, body)]: what is worth a notification, once per window
    and kind: a limit that runs out before its reset (not the milder "may"),
    one that is reached, and quota that will go unused."""
    out = []
    for w in ws:
        p = w.get("phrase")
        if not p or not p["attention"] or w.get("reset_ts") is None:
            continue
        name = "5-hour" if w["id"] == "h5" else "Weekly"
        if w["kind"] == "maxed":
            title = "%s limit reached, back %s" % (name, ("at " + p["num"]) if " " not in p["num"] else p["num"])
        elif w["kind"] == "dry" and not w.get("soft"):
            title = "%s limit runs out %s" % (name, ("at " + p["num"]) if " " not in p["num"] else p["num"])
        elif w["kind"] == "expire":
            title = "%s: %s will go unused" % (name, p["num"])
        else:
            continue
        out.append(("notified:%s:%s:%s" % (w["id"], w["reset_ts"], w["kind"]), title, w["sentence"]))
    return out


def notify(st, ws, newest, now, send=None):
    """Post each new event as a macOS notification, once. Old readings post
    nothing: a stale alarm is worse than none."""
    if not newest or now - newest > STALE_S:
        return []
    sent = []
    for key, title, body in events(ws):
        if st.meta_get(key):
            continue
        try:
            st.meta_set(key, now)
        except Exception:
            continue
        (send or post)(title, body)
        sent.append(title)
    return sent


def post(title, body):
    if sys.platform != "darwin":
        return
    script = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title \"ctempo\" "
              "subtitle (item 1 of argv)", "-e", "end run"]
    try:
        subprocess.Popen(["osascript"] + script + [title, body], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        pass


# -- the SwiftBar plugin ---------------------------------------------------------------

def plugin_dir():
    """SwiftBar's plugin folder, as it saved it on first launch, or None."""
    try:
        p = subprocess.run(["defaults", "read", "com.ameba.SwiftBar", "PluginDirectory"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    path = p.stdout.strip()
    return os.path.expanduser(path) if p.returncode == 0 and path else None


def script(python, args, entry, home):
    q = shlex.quote
    return "\n".join([
        "#!/bin/sh",
        MARK + " (written by `ctempo menubar --install`; `ctempo menubar --uninstall` removes it)",
        "# <swiftbar.title>ctempo</swiftbar.title>",
        "# <swiftbar.desc>Claude usage pace: the 5-hour and weekly windows</swiftbar.desc>",
        "# <swiftbar.hideAbout>true</swiftbar.hideAbout>",
        "# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>",
        "# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>",
        "export CTEMPO_HOME=%s" % q(home),
        "exec %s menubar" % " ".join(q(a) for a in [python] + list(args) + [entry]),
    ]) + "\n"


def runtime():
    """(python, args, entry): the install's pinned interpreter, else this one."""
    import json

    try:
        with open(config.runtime_path(), encoding="utf-8-sig") as f:
            rt = json.load(f)
        if isinstance(rt.get("python"), str) and isinstance(rt.get("entry"), str):
            args = rt.get("args") if isinstance(rt.get("args"), list) else ["-I", "-S"]
            return rt["python"], args, rt["entry"]
    except (OSError, ValueError):
        pass
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "ctempo")
    return sys.executable, ["-I", "-S"], entry


def ours(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return MARK in f.read(4096)
    except OSError:
        return False


def install(folder):
    folder = folder or plugin_dir()
    if not folder:
        print("SwiftBar's plugin folder is not set. Install SwiftBar (brew install --cask swiftbar, or "
              "https://swiftbar.app), open it once and choose a plugin folder, then run this again; "
              "or name the folder: ctempo menubar --install ~/SwiftBarPlugins")
        return 1
    folder = os.path.abspath(os.path.expanduser(folder))
    path = os.path.join(folder, PLUGIN)
    if os.path.exists(path) and not ours(path):
        print("%s exists and is not ctempo's; not touched" % path)
        return 1
    python, args, entry = runtime()
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(script(python, args, entry, config.home()))
    os.chmod(path, 0o755)
    from . import install as inst

    state = inst.load_state()
    state["menubar"] = path
    inst.save_state(state)
    print("Wrote %s: SwiftBar shows it within a minute (or choose Refresh All in its menu)." % path)
    return 0


def uninstall(folder=None):
    from . import install as inst

    state = inst.load_state()
    path = os.path.join(os.path.abspath(os.path.expanduser(folder)), PLUGIN) if folder else state.get("menubar")
    removed = remove(path)
    state.pop("menubar", None)
    inst.save_state(state)
    print("Removed %s" % path if removed else "No ctempo menu bar plugin to remove")
    return 0


def remove(path):
    if path and os.path.exists(path) and ours(path):
        os.remove(path)
        return True
    return False


def main(args, cfg):
    if args.fetch is not None:
        from . import usagecmd

        usagecmd.say_every(args.fetch, usagecmd.set_every(args.fetch))
        if args.install is None and args.uninstall is None:
            return 0
    if args.install is not None:
        return install(args.install or None)
    if args.uninstall is not None:
        return uninstall(args.uninstall or None)
    return show(cfg)
