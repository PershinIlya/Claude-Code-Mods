"""`ctempo menubar`: the band in the macOS menu bar, through SwiftBar.

SwiftBar (a free menu bar app) runs a plugin script every minute and shows
what it prints: the first line in the menu bar, the lines after `---` in the
menu that opens on a click. The script `ctempo menubar --install` writes
runs `ctempo menubar`, which reads the store and prints

  5h 22% · Wk 18%                      (red, yellow or green when a window needs you)
  ---
  5h ━╍╍───── 22% 77% will go unused   (each window, as the terminal line draws it)
  5-hour: 22% used, resets 12:00. ...  (the sentence Details shows)
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


def lines(ws, updated, now, cfg):
    """SwiftBar's output for the windows: the title, `---`, the menu."""
    title_parts, tones = [], []
    for w in ws:
        short = "5h" if w["id"] == "h5" else "Wk"
        if w["pct"] is None:
            title_parts.append(short + " -")
            continue
        part = "%s %s%d%%" % (short, "~" if w["approx"] else "", w["pct"])
        p = w.get("phrase")
        if p and p["attention"]:
            part += " " + p["narrow_pre"] + p["num"] + p["narrow"]
            tones.append(p["tone"])
        title_parts.append(part)
    waiting = all(w["pct"] is None for w in ws)
    stale = updated is None or now - updated > STALE_S
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
            out.append("%s | font=Menlo size=12 trim=false" % render.plain(band.segments([w], cfg, 60)))
            out.append("%s | size=11 color=%s" % (w["sentence"], DIM))
    out.append("---")
    if updated is not None:
        out.append("%s | size=11 color=%s" % (age(now - updated), DIM))
    if stale and not waiting:
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

    now = time.time()
    try:
        st = _open()
        if st is None:
            ws, updated = band.windows({}, {}, now), None
        else:
            try:
                v5, vw, _ = views(st, cfg, now)
            finally:
                st.close()
            ws, updated = band.windows(v5, vw, now), band.updated(v5, vw)
        out = lines(ws, updated, now, cfg)
    except Exception as e:  # the menu bar shows why instead of nothing
        out = ["ctempo ! | color=%s" % TONE["warning"], "---", "ctempo: %s | size=12" % str(e)[:200],
               "Refresh | refresh=true"]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


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
    if args.install is not None:
        return install(args.install or None)
    if args.uninstall is not None:
        return uninstall(args.uninstall or None)
    return show(cfg)
