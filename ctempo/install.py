"""`ctempo install` / `ctempo uninstall`.

Dry run by default; --apply writes. Edits exactly one key of settings.json
(statusLine), keeps every other key and hook, keeps the file's indentation
and trailing newline, backs it up with a timestamp before writing, and
records what it did in ~/.claude-tempo/install.json so uninstall can put the
original back. When the file has not changed since install apart from what
install and the plugin CLI did, uninstall restores it byte for byte.
"""

import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from . import __version__, config

MARKETPLACE = "claude-tempo"
PLUGIN = "ctempo"
PLUGIN_ID = PLUGIN + "@" + MARKETPLACE
PLUGIN_KEYS = ("enabledPlugins", "extraKnownMarketplaces")
LAUNCHER_MARK = "# ctempo launcher"


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def entry_path():
    return os.path.join(repo_root(), "bin", "ctempo")


def settings_path(explicit=None):
    return explicit or os.path.join(config.claude_dir(), "settings.json")


def state_path():
    return os.path.join(config.home(), "install.json")


# -- JSON with the file's own formatting ------------------------------------------
class Fmt:
    def __init__(self, indent=2, newline=False):
        self.indent = indent
        self.newline = newline


def detect_format(text):
    indent = 2
    m = re.search(r"\n([ \t]+)\S", text)
    if m:
        ws = m.group(1)
        indent = "\t" if ws.startswith("\t") else len(ws)
    return Fmt(indent, text.endswith("\n"))


def dumps(obj, fmt):
    s = json.dumps(obj, indent=fmt.indent, ensure_ascii=False)
    return s + ("\n" if fmt.newline else "")


def read_settings(path):
    """(object, raw text or None, Fmt). Raises ValueError on a file we must not touch."""
    real = os.path.realpath(path)
    if os.path.islink(path) and not os.path.exists(real):
        raise ValueError("%s is a dangling symlink; refusing to touch it" % path)
    if not os.path.exists(real):
        return {}, None, Fmt(2, True)
    with open(real, encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return {}, raw, Fmt(2, True)
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise ValueError("%s is not valid JSON (%s); refusing to touch it" % (path, e))
    if not isinstance(obj, dict):
        raise ValueError("%s is not a JSON object; refusing to touch it" % path)
    return obj, raw, detect_format(raw)


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def atomic_write(path, text):
    real = os.path.realpath(path)
    os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
    tmp = "%s.ctempo.%d.tmp" % (real, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if os.path.exists(real):
        shutil.copymode(real, tmp)
    os.replace(tmp, real)


def sha(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def diff(old, new, path):
    return "".join(
        difflib.unified_diff(
            (old or "").splitlines(True), new.splitlines(True), fromfile=path + " (now)", tofile=path + " (after)"
        )
    )


# -- interpreter ---------------------------------------------------------------------
PROBE = "import sys, sqlite3, json; print('%d.%d %s' % (sys.version_info[0], sys.version_info[1], sqlite3.sqlite_version))"


def probe(py):
    """(ok, version, sqlite, startup_ms, note)."""
    if not py or not os.path.isabs(py) or not os.access(py, os.X_OK):
        return False, None, None, None, "not an executable absolute path"
    if sys.platform == "darwin" and py == "/usr/bin/python3":
        # Without the Command Line Tools this shim opens an install dialog.
        if subprocess.run(["xcode-select", "-p"], capture_output=True).returncode != 0:
            return False, None, None, None, "Command Line Tools not installed"
    best = None
    out = ""
    for _ in range(3):
        t = time.perf_counter()
        try:
            r = subprocess.run([py, "-I", "-S", "-c", PROBE], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, None, None, None, str(e)
        ms = (time.perf_counter() - t) * 1000
        if r.returncode != 0:
            return False, None, None, None, (r.stderr.strip().splitlines() or ["failed"])[-1]
        out = r.stdout.strip()
        best = ms if best is None else min(best, ms)
    ver, sq = out.split(" ", 1)
    major, minor = (int(x) for x in ver.split("."))
    if (major, minor) < (3, 8):
        return False, ver, sq, best, "Python 3.8+ required"
    return True, ver, sq, best, ""


def candidates():
    seen, out = set(), []
    for p in [
        "/usr/bin/python3",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        sys.executable,
        shutil.which("python3"),
    ]:
        if p and p not in seen and os.path.exists(p):
            seen.add(p)
            out.append(p)
    return out


def choose_python(explicit=None):
    """Pick the interpreter: explicit, else the first stable one that passes.

    Order prefers system and Homebrew paths over the one running install
    (often a conda env that may be activated, updated or removed).
    """
    report = []
    if explicit:
        ok, ver, sq, ms, note = probe(explicit)
        report.append((explicit, ok, ver, sq, ms, note))
        return (explicit if ok else None), report
    chosen = None
    for p in candidates():
        ok, ver, sq, ms, note = probe(p)
        report.append((p, ok, ver, sq, ms, note))
        if ok and chosen is None:
            chosen = p
    return chosen, report


# -- the statusLine command --------------------------------------------------------
def is_ours(cmd):
    return isinstance(cmd, str) and "ctempo" in cmd and " statusline" in cmd


def our_command(python, entry, compose=None):
    parts = [shlex.quote(python), "-I", "-S", shlex.quote(entry), "statusline"]
    if compose:
        parts += ["--compose", shlex.quote(compose)]
    return " ".join(parts)


def compose_of(cmd):
    """The composed command embedded in one of our statusLine commands."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None
    if "--compose" in argv:
        i = argv.index("--compose")
        return argv[i + 1] if i + 1 < len(argv) else None
    return None


def plan_statusline(current, python, entry, replace=False, refresh=60):
    """New statusLine value and a note, or (None, note) when nothing changes."""
    if isinstance(current, dict) and is_ours(current.get("command")):
        compose = compose_of(current["command"])
        want = dict(current)
        want["command"] = our_command(python, entry, compose)
        want.setdefault("refreshInterval", refresh)
        if want == current:
            return None, "statusLine already runs ctempo"
        return want, "update ctempo's statusLine command (interpreter or path changed)"
    if isinstance(current, dict) and isinstance(current.get("command"), str) and current["command"].strip():
        if replace:
            return {"type": "command", "command": our_command(python, entry), "refreshInterval": refresh}, (
                "REPLACE existing statusLine (kept in the backup and in install.json)"
            )
        want = dict(current)
        want["type"] = "command"
        want["command"] = our_command(python, entry, current["command"])
        theirs = current.get("refreshInterval")
        want["refreshInterval"] = min(refresh, theirs) if isinstance(theirs, (int, float)) and theirs >= 1 else refresh
        return want, "compose: run your existing statusLine command and append ctempo's segment"
    return {"type": "command", "command": our_command(python, entry), "refreshInterval": refresh}, "add statusLine"


# -- plugin (mod) via the claude CLI -----------------------------------------------
def claude_bin():
    return shutil.which("claude")


def run_claude(args, env=None):
    exe = claude_bin()
    if not exe:
        return 127, "", "claude not found on PATH"
    r = subprocess.run([exe] + args, capture_output=True, text=True, env=env, timeout=120)
    return r.returncode, r.stdout, r.stderr


def mod_commands(root):
    return [
        ["plugin", "marketplace", "add", root],
        ["plugin", "install", PLUGIN_ID, "--scope", "user"],
    ]


def unmod_commands():
    return [
        ["plugin", "uninstall", PLUGIN_ID, "--scope", "user"],
        ["plugin", "marketplace", "remove", MARKETPLACE],
    ]


# -- launcher ------------------------------------------------------------------------
def launcher_text(python, entry):
    return '#!/bin/sh\n%s\nexec %s -I -S %s "$@"\n' % (LAUNCHER_MARK, shlex.quote(python), shlex.quote(entry))


def runtime_info(python, entry):
    return {"python": python, "entry": entry, "repo": repo_root(), "version": __version__, "written_at": time.time()}


# -- install / uninstall -------------------------------------------------------------
def install(args, out=print):
    spath = settings_path(args.settings)
    try:
        obj, raw, fmt = read_settings(spath)
    except ValueError as e:
        out("error: %s" % e)
        return 1
    python, report = choose_python(args.python)
    out("Interpreter candidates (python -I -S startup, best of 3):")
    for p, ok, ver, sq, ms, note in report:
        out(
            "  %s %-40s %s%s%s"
            % (
                "*" if p == python else ("+" if ok else "-"),
                p,
                ("Python %s, sqlite %s" % (ver, sq)) if ver else "",
                (", %.0f ms" % ms) if ms else "",
                (" (" + note + ")") if note else "",
            )
        )
    if not python:
        out("error: no usable Python 3.8+ interpreter found; pass --python /abs/path")
        return 1
    entry = entry_path()
    current = obj.get("statusLine")
    want, note = plan_statusline(current, python, entry, replace=args.replace)
    new_obj = dict(obj)
    if want is not None:
        new_obj["statusLine"] = want
    new_raw = dumps(new_obj, fmt) if want is not None else raw
    out("")
    out("Settings: %s" % spath)
    out("  statusLine: %s" % note)
    if want is not None:
        d = diff(raw, new_raw, spath)
        out(d if d else "  (no textual change)")
    root = repo_root()
    use_mod = not args.no_mod
    if use_mod:
        out("Mod (Desktop Code tab band), via the local marketplace in %s:" % root)
        for c in mod_commands(root):
            out("  claude " + " ".join(shlex.quote(x) for x in c))
        if not claude_bin():
            out("  (claude is not on PATH: run these yourself later)")
    bin_dir = os.path.expanduser(args.bin_dir) if args.bin_dir else None
    if bin_dir:
        out("Launcher: %s/ctempo -> %s -I -S %s" % (bin_dir, python, entry))
    out("Runtime info for the mod: %s" % config.runtime_path())
    if not args.no_bootstrap:
        out("Profile: bootstrap the prior from ~/.claude/projects transcripts (timestamps and token counts only)")
    if not args.apply:
        out("")
        out("DRY RUN: nothing was written. Re-run with --apply to install.")
        return 0

    # Apply.
    state = load_state() or {}
    if want is not None:
        backup = None
        if raw is not None:
            backup = "%s.ctempo-backup-%s" % (os.path.realpath(spath), time.strftime("%Y%m%d-%H%M%S"))
            shutil.copy2(os.path.realpath(spath), backup)
        if not state.get("installed") or state.get("settings_path") != spath:
            state = {
                "installed": True,
                "settings_path": spath,
                "had_file": raw is not None,
                "backup_path": backup,
                "original_sha256": sha(raw) if raw is not None else None,
                "had_statusLine": "statusLine" in obj,
                "original_statusLine": obj.get("statusLine"),
                "had_keys": {k: (k in obj) for k in PLUGIN_KEYS},
            }
        atomic_write(spath, new_raw)
        out("Wrote %s%s" % (spath, (" (backup: %s)" % backup) if backup else ""))
    state.update(
        {"installed": True, "settings_path": spath, "python": python, "entry": entry, "written_statusLine": new_obj.get("statusLine")}
    )
    os.makedirs(config.home(), exist_ok=True)
    with open(config.runtime_path(), "w", encoding="utf-8") as f:
        json.dump(runtime_info(python, entry), f, indent=2)
    config.write_defaults()
    if bin_dir:
        os.makedirs(bin_dir, exist_ok=True)
        lp = os.path.join(bin_dir, "ctempo")
        if not os.path.exists(lp) or LAUNCHER_MARK in read_text(lp):
            with open(lp, "w", encoding="utf-8") as f:
                f.write(launcher_text(python, entry))
            os.chmod(lp, 0o755)
            state["launcher"] = lp
            out("Wrote launcher %s" % lp)
        else:
            out("Skipped launcher: %s exists and is not ours" % lp)
    rc = 0
    if use_mod:
        if claude_bin():
            for c in mod_commands(root):
                code, so, se = run_claude(c)
                msg = (so.strip() or se.strip()).splitlines()
                out("  claude %s -> exit %d %s" % (" ".join(c[:3]), code, msg[-1] if msg else ""))
                if code != 0 and "already" not in (so + se).lower():
                    rc = 1
            state["mod"] = {"marketplace": MARKETPLACE, "plugin": PLUGIN_ID, "root": root}
        else:
            out("  claude not on PATH: mod not installed")
    save_state(state)
    if not args.no_bootstrap:
        try:
            from . import transcripts
            from .store import Store

            st = Store()
            try:
                prior, meta, stats = transcripts.bootstrap(st, config.load())
            finally:
                st.close()
            if prior is not None:
                out("Profile prior from transcripts: %d messages over %s days (no content kept)." % (stats["messages"], meta["days"]))
            else:
                out("No transcripts in the last %s days: the profile starts uniform." % config.load()["profile"]["bootstrap_days"])
        except Exception as e:  # bootstrap is a bonus; never fail the install on it
            out("Bootstrap skipped: %s (run `ctempo bootstrap` later)" % e)
    out("Done. Restart Claude Code or run /reload-plugins to load the mod; the statusLine applies at once.")
    return rc


def uninstall(args, out=print):
    state = load_state()
    spath = settings_path(args.settings or (state or {}).get("settings_path"))
    try:
        obj, raw, fmt = read_settings(spath)
    except ValueError as e:
        out("error: %s" % e)
        return 1
    if not state:
        state = {}
    mod = state.get("mod")
    cmds = unmod_commands() if (mod and not args.no_mod) else []
    out("Settings: %s" % spath)
    for c in cmds:
        out("  claude " + " ".join(c))
    if not args.apply:
        new_obj, new_raw = restore(obj, raw, fmt, state)
        cur = obj.get("statusLine")
        if not is_ours((cur or {}).get("command") if isinstance(cur, dict) else None):
            out("  statusLine is not ctempo's; it will be left alone")
        d = diff(raw, new_raw, spath) if new_raw is not None else "  (file would be removed: it did not exist before install)\n"
        out(d if d else "  (no change to statusLine)")
        if cmds:
            out("  (the plugin commands also remove enabledPlugins / extraKnownMarketplaces entries)")
        out("DRY RUN: nothing was written. Re-run with --apply to uninstall.")
        return 0
    for c in cmds:
        code, so, se = run_claude(c)
        msg = (so.strip() or se.strip()).splitlines()
        out("  claude %s -> exit %d %s" % (" ".join(c[:3]), code, msg[-1] if msg else ""))
    obj, raw, fmt = read_settings(spath)  # the plugin CLI may have rewritten it
    new_obj, new_raw = restore(obj, raw, fmt, state)
    if new_raw is None:
        if raw is not None and os.path.exists(spath):
            os.remove(os.path.realpath(spath))
            out("Removed %s (it did not exist before install)" % spath)
    elif new_raw != raw:
        atomic_write(spath, new_raw)
        out("Restored statusLine in %s" % spath)
    lp = state.get("launcher")
    if lp and os.path.exists(lp) and LAUNCHER_MARK in read_text(lp):
        os.remove(lp)
        out("Removed launcher %s" % lp)
    if os.path.exists(config.runtime_path()):
        os.remove(config.runtime_path())
    state["installed"] = False
    save_state(state)
    out("Uninstalled. Your data stays in %s (delete it yourself if you want)." % config.home())
    return 0


def restore(obj, raw, fmt, state):
    """Settings with ctempo's statusLine undone. (obj, text) or (obj, None) to delete the file."""
    new = dict(obj)
    cur = obj.get("statusLine")
    if isinstance(cur, dict) and is_ours(cur.get("command")):
        if state.get("had_statusLine") is True:
            new["statusLine"] = state.get("original_statusLine")
        elif state.get("had_statusLine") is False:
            new.pop("statusLine", None)
        else:  # no install record: undo what the command itself says
            orig = compose_of(cur["command"])
            if orig:
                restored = dict(cur)
                restored["command"] = orig
                new["statusLine"] = restored
            else:
                new.pop("statusLine", None)
    for k, had in (state.get("had_keys") or {}).items():
        if not had and k in new and new[k] in ({}, []):
            new.pop(k)
    if state.get("had_file") is False and not new:
        return new, None
    # Byte-exact when nothing else changed: hand back the original bytes.
    backup = state.get("backup_path")
    if backup and os.path.exists(backup):
        orig_raw = read_text(backup)
        try:
            if json.loads(orig_raw) == new and sha(orig_raw) == state.get("original_sha256"):
                return new, orig_raw
        except ValueError:
            pass
    if new == obj and raw is not None:
        return new, raw
    return new, dumps(new, fmt)


def load_state():
    try:
        with open(state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(state):
    os.makedirs(config.home(), exist_ok=True)
    with open(state_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
