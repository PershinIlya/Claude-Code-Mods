"""`ctempo install` / `ctempo uninstall`.

Dry run by default; --apply writes. Edits exactly one key of settings.json
(statusLine), keeps every other key and hook, keeps the file's indentation
and trailing newline, backs it up with a timestamp before writing, and
records what it did in ~/.claude-tempo/install.json (one record per settings
file) so uninstall can put the original back. The record is saved before the
settings file is written. When the file has not changed since install apart
from what install and the plugin CLI did, uninstall restores it byte for
byte (line endings and mode included).
"""

import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
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
    """Absolute path (a relative one would mean another file from another directory)."""
    return os.path.abspath(os.path.expanduser(explicit or os.path.join(config.claude_dir(), "settings.json")))


def state_path():
    return os.path.join(config.home(), "install.json")


# -- JSON with the file's own formatting ------------------------------------------
class Fmt:
    def __init__(self, indent=2, newline=False, eol="\n"):
        self.indent = indent
        self.newline = newline
        self.eol = eol


def detect_format(text):
    indent = 2
    m = re.search(r"\n([ \t]+)\S", text)
    if m:
        ws = m.group(1)
        indent = "\t" if ws.startswith("\t") else len(ws)
    eol = "\r\n" if "\r\n" in text else "\n"
    return Fmt(indent, text.endswith("\n"), eol)


def dumps(obj, fmt):
    # JSON strings cannot hold raw newlines, so only structural ones change.
    s = json.dumps(obj, indent=fmt.indent, ensure_ascii=False).replace("\n", fmt.eol)
    return s + (fmt.eol if fmt.newline else "")


def read_settings(path):
    """(object, raw text or None, Fmt). Raises ValueError on a file we must not touch.

    Read with newline="" so CRLF files keep their bytes."""
    real = os.path.realpath(path)
    if os.path.islink(path) and not os.path.exists(real):
        raise ValueError("%s is a dangling symlink; refusing to touch it" % path)
    if not os.path.exists(real):
        return {}, None, Fmt(2, True)
    try:
        with open(real, encoding="utf-8", newline="") as f:
            raw = f.read()
    except UnicodeDecodeError:
        raise ValueError("%s is not UTF-8; refusing to touch it" % path)
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
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _umask():
    m = os.umask(0)
    os.umask(m)
    return m


def atomic_write(path, data):
    """Write str or bytes via a private tmp file and rename; keeps the mode."""
    real = os.path.realpath(path)
    os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    mode = stat.S_IMODE(os.stat(real).st_mode) if os.path.exists(real) else (0o666 & ~_umask())
    tmp = "%s.ctempo.%d.tmp" % (real, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def make_backup(path):
    """Copy the file's bytes to a new, never-overwritten timestamped backup."""
    real = os.path.realpath(path)
    base = "%s.ctempo-backup-%s" % (real, time.strftime("%Y%m%d-%H%M%S"))
    with open(real, "rb") as f:
        data = f.read()
    for i in range(1000):
        name = base if i == 0 else "%s-%d" % (base, i)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        shutil.copymode(real, name)
        return name
    raise OSError("could not create a unique backup next to %s" % real)


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
        had = compose_of(current["command"])
        compose = None if replace else had
        want = dict(current)
        want["command"] = our_command(python, entry, compose)
        want.setdefault("refreshInterval", refresh)
        if want == current:
            return None, "statusLine already runs ctempo"
        if replace and had:
            return want, "REPLACE: drop the composed statusLine command (kept in the backup and in install.json)"
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
    try:
        r = subprocess.run([exe] + args, capture_output=True, text=True, env=env, timeout=120)
    except subprocess.TimeoutExpired:
        return 124, "", "claude %s timed out after 120 s" % " ".join(args[:3])
    except OSError as e:
        return 127, "", "could not run claude: %s" % e
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


# -- install records ---------------------------------------------------------------
STATE_VERSION = 2


def load_state():
    """{"version": 2, "records": {abs settings path: record}, ...globals}."""
    try:
        with open(state_path(), encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = None
    if not isinstance(state, dict):
        state = {}
    if "records" not in state:  # version 1: one record at the top level
        rec = {k: state[k] for k in ("settings_path", "had_file", "backup_path", "original_sha256", "had_statusLine",
                                     "original_statusLine", "had_keys", "installed", "written_statusLine") if k in state}
        path = rec.get("settings_path")
        state = {k: v for k, v in state.items() if k not in rec}
        state["records"] = {path: rec} if isinstance(path, str) and os.path.isabs(path) else {}
    state["version"] = STATE_VERSION
    return state


def save_state(state):
    os.makedirs(config.home(), exist_ok=True)
    tmp = state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, state_path())


def installed_records(state):
    return {p: r for p, r in state.get("records", {}).items() if r.get("installed")}


def _original_from_backups(spath):
    """The statusLine in the newest backup that predates ctempo, if any."""
    real = os.path.realpath(spath)
    folder = os.path.dirname(real) or "."
    prefix = os.path.basename(real) + ".ctempo-backup-"
    try:
        names = sorted((n for n in os.listdir(folder) if n.startswith(prefix)), reverse=True)
    except OSError:
        return None
    for n in names:
        try:
            obj = json.loads(read_text(os.path.join(folder, n)))
        except (OSError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        sl = obj.get("statusLine")
        if not (isinstance(sl, dict) and is_ours(sl.get("command"))):
            return ("statusLine" in obj, sl)
    return None


def _statusline_part(obj, spath):
    """What install should remember as the original statusLine."""
    current = obj.get("statusLine")
    if isinstance(current, dict) and is_ours(current.get("command")):
        # Already ctempo's (install record lost, or repinned): never record
        # ctempo itself as the original. Prefer a pre-ctempo backup, else
        # what --compose says.
        found = _original_from_backups(spath)
        if found is not None:
            return {"had_statusLine": found[0], "original_statusLine": found[1]}
        comp = compose_of(current["command"])
        if comp:
            return {"had_statusLine": True, "original_statusLine": dict(current, command=comp)}
        return {"had_statusLine": None, "original_statusLine": None}
    return {"had_statusLine": "statusLine" in obj, "original_statusLine": current}


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
    bin_dir = os.path.abspath(os.path.expanduser(args.bin_dir)) if args.bin_dir else None
    if bin_dir:
        out("Launcher: %s/ctempo -> %s -I -S %s" % (bin_dir, python, entry))
    out("Runtime info for the mod: %s" % config.runtime_path())
    if not args.no_bootstrap:
        out("Profile: bootstrap the prior from ~/.claude/projects transcripts (timestamps and token counts only)")
    if not args.apply:
        out("")
        out("DRY RUN: nothing was written. Re-run with --apply to install.")
        return 0

    # Apply. The record goes to disk before the settings file changes, so a
    # crash at any later point still leaves uninstall what it needs.
    state = load_state()
    records = state.setdefault("records", {})
    rec = records.get(spath)
    cur_ours = isinstance(current, dict) and is_ours(current.get("command"))
    if want is not None:
        backup = make_backup(spath) if raw is not None else None
        if rec is None or not rec.get("installed"):
            rec = {"settings_path": spath, "had_file": raw is not None, "had_keys": {k: (k in obj) for k in PLUGIN_KEYS}}
            rec.update(_statusline_part(obj, spath))
            rec.update(backup_path=backup, original_sha256=sha(raw) if raw is not None else None)
        elif not cur_ours:
            # The statusLine changed since the last install: that is now the
            # original, and the backup just taken is the file to come back to.
            rec.update(_statusline_part(obj, spath))
            rec.update(backup_path=backup, original_sha256=sha(raw) if raw is not None else None)
        rec.update(installed=True, written_statusLine=want)
        records[spath] = rec
        state.update(python=python, entry=entry)
        save_state(state)
        atomic_write(spath, new_raw)
        out("Wrote %s%s" % (spath, (" (backup: %s)" % backup) if backup else ""))
    elif rec is None and cur_ours:
        rec = {"settings_path": spath, "had_file": True, "had_keys": {}, "installed": True}
        rec.update(_statusline_part(obj, spath))
        records[spath] = rec
    rc = 0
    try:
        state.update(python=python, entry=entry)
        os.makedirs(config.home(), exist_ok=True)
        with open(config.runtime_path(), "w", encoding="utf-8") as f:
            json.dump(runtime_info(python, entry), f, indent=2)
        config.write_defaults()
        if bin_dir:
            lp = os.path.join(bin_dir, "ctempo")
            try:
                os.makedirs(bin_dir, exist_ok=True)
                if not os.path.exists(lp) or LAUNCHER_MARK in read_text(lp):
                    with open(lp, "w", encoding="utf-8") as f:
                        f.write(launcher_text(python, entry))
                    os.chmod(lp, 0o755)
                    state["launcher"] = lp
                    out("Wrote launcher %s" % lp)
                else:
                    out("Skipped launcher: %s exists and is not ours" % lp)
            except OSError as e:
                out("Skipped launcher: %s" % e)
                rc = 1
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
    finally:
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
                out("Profile prior from transcripts: %d messages over %s days, trusted %.0f%% (no content kept)." % (
                    stats["messages"], meta["days"], 100 * meta["trust"]))
            else:
                out("No transcripts in the last %s days: the profile starts uniform." % config.load()["profile"]["bootstrap_days"])
        except Exception as e:  # bootstrap is a bonus; never fail the install on it
            out("Bootstrap skipped: %s (run `ctempo bootstrap` later)" % e)
    out("Done. Restart Claude Code or run /reload-plugins to load the mod; the statusLine applies at once.")
    return rc


def uninstall(args, out=print):
    state = load_state()
    records = state.setdefault("records", {})
    if args.settings:
        spath = settings_path(args.settings)
    else:
        live = installed_records(state)
        if len(live) > 1:
            out("error: ctempo is installed in several settings files; pass --settings one of:")
            for p in sorted(live):
                out("  " + p)
            return 1
        spath = next(iter(live)) if live else settings_path(None)
    rec = records.get(spath) or {}
    if not os.path.exists(os.path.realpath(spath)):
        out("Settings: %s does not exist; nothing to restore there." % spath)
        obj, raw, fmt = None, None, None
    else:
        try:
            obj, raw, fmt = read_settings(spath)
        except ValueError as e:
            out("error: %s" % e)
            return 1
        out("Settings: %s" % spath)
    others = [p for p in installed_records(state) if p != spath]
    cmds = unmod_commands() if (state.get("mod") and not args.no_mod and not others) else []
    for c in cmds:
        out("  claude " + " ".join(c))
    cur = obj.get("statusLine") if obj else None
    ours = isinstance(cur, dict) and is_ours(cur.get("command"))
    if obj is not None and not ours:
        out("  statusLine is not ctempo's; it is left alone")
    if not args.apply:
        if obj is not None and ours:
            new_obj, new_raw = restore(obj, raw, fmt, rec)
            d = diff(raw, new_raw, spath) if new_raw is not None else "  (file would be removed: it did not exist before install)\n"
            out(d if d else "  (no change)")
        if cmds:
            out("  (the plugin commands also remove enabledPlugins / extraKnownMarketplaces entries)")
        out("DRY RUN: nothing was written. Re-run with --apply to uninstall.")
        return 0
    for c in cmds:
        code, so, se = run_claude(c)
        msg = (so.strip() or se.strip()).splitlines()
        out("  claude %s -> exit %d %s" % (" ".join(c[:3]), code, msg[-1] if msg else ""))
    if obj is not None:
        obj, raw, fmt = read_settings(spath)  # the plugin CLI may have rewritten it
        cur = obj.get("statusLine")
        ours = isinstance(cur, dict) and is_ours(cur.get("command"))
        if ours or cmds:
            new_obj, new_raw = restore(obj, raw, fmt, rec)
            if new_raw is None:
                os.remove(os.path.realpath(spath))
                out("Removed %s (it did not exist before install)" % spath)
            elif new_raw != raw:
                atomic_write(spath, new_raw)
                out("Restored %s" % spath)
    records.pop(spath, None)
    if not installed_records(state):
        lp = state.pop("launcher", None)
        if lp and os.path.exists(lp) and LAUNCHER_MARK in read_text(lp):
            os.remove(lp)
            out("Removed launcher %s" % lp)
        if os.path.exists(config.runtime_path()):
            os.remove(config.runtime_path())
        if cmds:
            state.pop("mod", None)
    save_state(state)
    out("Uninstalled. Your data stays in %s (delete it yourself if you want)." % config.home())
    return 0


def restore(obj, raw, fmt, rec):
    """Settings with ctempo's statusLine undone: (obj, text or bytes), or (obj, None) to delete the file."""
    new = dict(obj)
    cur = obj.get("statusLine")
    if isinstance(cur, dict) and is_ours(cur.get("command")):
        orig = rec.get("original_statusLine")
        orig_ours = isinstance(orig, dict) and is_ours(orig.get("command"))
        if rec.get("had_statusLine") is True and not orig_ours:
            new["statusLine"] = orig
        elif rec.get("had_statusLine") is False:
            new.pop("statusLine", None)
        else:  # no usable record: undo what the command itself says
            comp = compose_of(cur["command"])
            if comp:
                new["statusLine"] = dict(cur, command=comp)
            else:
                new.pop("statusLine", None)
    for k, had in (rec.get("had_keys") or {}).items():
        if not had and k in new and new[k] in ({}, []):
            new.pop(k)
    if rec.get("had_file") is False and not new:
        return new, None
    # Byte-exact when nothing else changed: hand back the backup's bytes.
    backup = rec.get("backup_path")
    if backup and os.path.exists(backup):
        try:
            with open(backup, "rb") as f:
                orig_bytes = f.read()
            if json.loads(orig_bytes.decode("utf-8")) == new and hashlib.sha256(orig_bytes).hexdigest() == rec.get("original_sha256"):
                return new, orig_bytes
        except (OSError, ValueError):
            pass
    if new == obj and raw is not None:
        return new, raw
    return new, dumps(new, fmt)
