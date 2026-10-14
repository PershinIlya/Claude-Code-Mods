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
OWN_ENTRY = {"enabledPlugins": PLUGIN_ID, "extraKnownMarketplaces": MARKETPLACE}
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


def _user_keys(obj):
    """Which plugin keys the settings hold of their own: one that holds only
    ctempo's entry is the plugin CLI's (its commands were run by hand)."""
    return {k: k in obj and not (isinstance(obj[k], dict) and list(obj[k]) == [OWN_ENTRY[k]]) for k in PLUGIN_KEYS}


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
        # Version 1's uninstall restored the file and left the record behind: it is spent.
        path = _v1_key(rec) if rec.get("installed") is not False else None
        state = {k: v for k, v in state.items() if k not in rec}
        if path:
            rec["settings_path"] = path
        state["records"] = {path: rec} if path else {}
    state["version"] = STATE_VERSION
    return state


BACKUP_SUFFIX = re.compile(r"\.ctempo-backup-\d{8}-\d{6}(-\d+)?$")


def _v1_key(rec):
    """Absolute path of a version-1 record. Version 1 stored --settings as
    typed (maybe relative); its backup path was built from the real path."""
    path = rec.get("settings_path")
    if isinstance(path, str) and os.path.isabs(path):
        return os.path.abspath(path)
    backup = rec.get("backup_path")
    if isinstance(backup, str) and BACKUP_SUFFIX.search(backup):
        return BACKUP_SUFFIX.sub("", backup)
    if isinstance(path, str) and path:
        return os.path.abspath(path)
    return None


def find_record(state, spath):
    """(key, record) for a settings file, matching symlink aliases too."""
    records = state.setdefault("records", {})
    if spath in records:
        return spath, records[spath]
    real = os.path.realpath(spath)
    for key, rec in records.items():
        if os.path.realpath(key) == real:
            return key, rec
    return spath, None


def save_state(state):
    os.makedirs(config.home(), exist_ok=True)
    tmp = state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, state_path())


def installed_records(state):
    """Installed records, one per real file (aliases through symlinks count once)."""
    out, seen = {}, set()
    for p, r in state.get("records", {}).items():
        real = os.path.realpath(p)
        if r.get("installed") and real not in seen:
            seen.add(real)
            out[p] = r
    return out


def _backup_order(name):
    """Sort key of a backup name: its time, then its sequence number ("-10" after "-9")."""
    m = re.search(r"-(\d{8})-(\d{6})(?:-(\d+))?$", name)
    return (m.group(1), m.group(2), int(m.group(3) or 0)) if m else ("", "", 0)


def _original_from_backups(spath):
    """The statusLine in the newest backup that predates ctempo, if any. A command
    in ctempo's --compose in a newer backup (the user put it there, and --replace
    dropped it since) replaces its command, or is the statusLine when no backup
    predates ctempo."""
    real = os.path.realpath(spath)
    folder = os.path.dirname(real) or "."
    prefix = os.path.basename(real) + ".ctempo-backup-"
    try:
        names = sorted((n for n in os.listdir(folder) if n.startswith(prefix) and BACKUP_SUFFIX.search(n)),
                       key=_backup_order, reverse=True)
    except OSError:
        return None
    composed = None
    for n in names:
        try:
            text = read_text(os.path.join(folder, n))
            obj = json.loads(text) if text.strip() else {}  # a blank file is {}, as in read_settings
        except (OSError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        sl = obj.get("statusLine")
        if not (isinstance(sl, dict) and is_ours(sl.get("command"))):
            rec = _with_compose({"had_statusLine": "statusLine" in obj, "original_statusLine": sl}, composed)
            return rec["had_statusLine"], rec["original_statusLine"]
        if composed is None and compose_of(sl["command"]):
            composed = sl
    comp = compose_of(composed["command"]) if composed else None
    if comp and not is_ours(comp):
        return True, dict(composed, command=comp)
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


def _with_compose(rec, cur):
    """The record with the command in ctempo's --compose as the original: the
    user may have changed it there, where settings.json shows what runs (D-17)."""
    comp = compose_of(cur["command"]) if isinstance(cur, dict) and isinstance(cur.get("command"), str) else None
    orig = rec.get("original_statusLine")
    if comp and not is_ours(comp) and rec.get("had_statusLine") is False:
        # No statusLine before ctempo, which then composes nothing: the user put it
        # there. Keep their keys, not the refreshInterval ctempo added.
        sl = {k: v for k, v in cur.items() if k != "refreshInterval"}
        return dict(rec, had_statusLine=True, original_statusLine=dict(sl, command=comp))
    if comp and not is_ours(comp) and rec.get("had_statusLine") is True and isinstance(orig, dict) \
            and not is_ours(orig.get("command")) and orig.get("command") != comp:
        return dict(rec, original_statusLine=dict(orig, command=comp))
    return rec


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
    key, rec = find_record(state, spath)
    if key != spath and rec is not None:  # the same file through another name: one record
        records[spath] = records.pop(key)
    cur_ours = isinstance(current, dict) and is_ours(current.get("command"))
    if want is not None:
        backup = make_backup(spath) if raw is not None else None
        user_file = os.path.realpath((state.get("mod") or {}).get("user_settings") or "") == os.path.realpath(spath)
        if (rec is None or not rec.get("installed")) and user_file and raw is not None:
            # The mod is in already, so the plugin CLI may have written here: the
            # file to come back to is this one, else (pristine) the one before the
            # mod, from this file's kept record or the snapshot taken then.
            base = rec if rec is not None else state["mod"]
            pristine = base.get("pristine") or {k: base.get(k) for k in ("had_file", "backup_path", "original_sha256")}
            rec = {"settings_path": spath, "had_file": True, "had_keys": dict(base.get("had_keys") or {}),
                   "backup_path": backup, "original_sha256": sha(raw), "pristine": pristine}
            rec.update(_statusline_part(obj, spath))
        elif rec is None or not rec.get("installed"):
            rec = {"settings_path": spath, "had_file": raw is not None, "had_keys": _user_keys(obj)}
            rec.update(_statusline_part(obj, spath))
            rec.update(backup_path=backup, original_sha256=sha(raw) if raw is not None else None)
        elif not cur_ours:
            # The statusLine changed since the last install (or the file was
            # deleted): that is now the original, and the backup just taken
            # is the file to come back to.
            rec.update(_statusline_part(obj, spath))
            rec.update(backup_path=backup, original_sha256=sha(raw) if raw is not None else None)
            rec["had_file"] = raw is not None
            if raw is None:
                rec["had_keys"] = {k: False for k in PLUGIN_KEYS}
        elif rec.get("had_statusLine") is None:  # no usable record: a backup, else --compose
            rec.update(_statusline_part(obj, spath))
        if cur_ours:
            rec = _with_compose(rec, current)  # a repin or --replace must not lose an edited --compose
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
            m = state.get("mod") or {}
            # No snapshot yet (or an early mod entry without one), or a manual mod
            # (the plugin CLI never wrote for it): the user file may have changed since.
            if not m.get("user_settings") or (m.get("manual") and claude_bin() and not _cli_wrote(m)):
                # The plugin CLI edits the user settings file: remember
                # which plugin keys it had, so uninstall can tidy up there.
                # The mod is manual until ctempo sees the plugin CLI write there.
                us = settings_path(None)
                try:
                    uobj, uraw, _ = read_settings(us)
                    had_keys = _user_keys(uobj)
                except ValueError:  # not valid JSON: it exists, and its keys are unknown, so never removed
                    uobj, uraw, had_keys = {}, None, {k: True for k in PLUGIN_KEYS}
                state["mod"] = {"user_settings": us, "had_file": uraw is not None or os.path.exists(os.path.realpath(us)),
                                "had_keys": had_keys, "manual": True}
                if uraw is not None and os.path.realpath(us) != os.path.realpath(spath):
                    try:
                        state["mod"].update(backup_path=make_backup(us), original_sha256=sha(uraw))
                    except OSError:
                        pass  # uninstall then tidies by keys instead of bytes
                _, urec = find_record(state, us)
                if urec is not None and not urec.get("installed"):
                    # Kept from before this snapshot, and the plugin CLI has not
                    # written there since: the snapshot is the newer baseline.
                    _drop_records(state, os.path.realpath(us))
            _forget_keys(state["mod"])
            us = state["mod"]["user_settings"]
            before = _peek(us)
            if claude_bin():
                for c in mod_commands(root):
                    code, so, se = run_claude(c)
                    msg = (so.strip() or se.strip()).splitlines()
                    out("  claude %s -> exit %d %s" % (" ".join(c[:3]), code, msg[-1] if msg else ""))
                    if code != 0 and "already" not in (so + se).lower():
                        rc = 1
            else:
                # The user may run the commands printed above: record the mod
                # anyway, so uninstall undoes them.
                out("  claude not on PATH: mod not installed")
            if _peek(us) != before or _mod_listed(us):
                state["mod"].pop("manual", None)  # the plugin CLI has written there
            state["mod"].update(marketplace=MARKETPLACE, plugin=PLUGIN_ID, root=root)
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


def _done(code, so, se):
    """A plugin command succeeded, or there was nothing left to remove."""
    if code == 0:
        return True
    if code in (124, 127):  # claude timed out or could not run: nothing happened
        return False
    text = (so + se).lower()
    return "not found in installed plugins" in text or ("marketplace" in text and "not found" in text) \
        or "not installed" in text


def _peek(path):
    """The file's bytes, or None when there is no such file."""
    try:
        with open(os.path.realpath(path), "rb") as f:
            return f.read()
    except (OSError, TypeError):
        return None


def _mod_listed(us):
    """Whether the user settings file names the mod or its marketplace (True when it cannot tell)."""
    if not us:
        return True
    try:
        obj, _, _ = read_settings(us)
    except (OSError, ValueError):
        return True
    return _lists_mod(obj)


def _lists_mod(obj):
    ep, km = obj.get("enabledPlugins"), obj.get("extraKnownMarketplaces")
    return (isinstance(ep, dict) and PLUGIN_ID in ep) or (isinstance(km, dict) and MARKETPLACE in km)


def _listed_mod():
    """A mod record rebuilt from the user settings file when that lists the mod
    or its marketplace, else None (also when the file cannot be read)."""
    us = settings_path(None)
    try:
        obj, _, _ = read_settings(us)
    except (OSError, ValueError):
        return None
    if not _lists_mod(obj):
        return None
    # No snapshot to restore bytes from: tidy by keys, where one that holds only
    # ctempo's entry is the plugin CLI's.
    return {"user_settings": us, "had_file": True, "had_keys": _user_keys(obj)}


def _forget_keys(mod):
    """A plugin key the user file does not hold of its own now (the user deleted
    or blanked the file since) is not the user's: tidy it away once empty."""
    try:
        now = _user_keys(read_settings(mod["user_settings"])[0])
    except (KeyError, TypeError, OSError, ValueError):
        return
    if mod.get("had_keys"):
        mod["had_keys"] = {k: bool(had) and now.get(k, False) for k, had in mod["had_keys"].items()}


def _cli_wrote(mod):
    """Whether the plugin CLI has written to the user settings file: it lists the
    mod, or has a plugin key it did not have before (True when it cannot tell)."""
    us = mod.get("user_settings")
    if _mod_listed(us):
        return True
    try:
        obj, _, _ = read_settings(us)
    except (OSError, ValueError):
        return True
    return any(k in obj and not had for k, had in (mod.get("had_keys") or {}).items())


def _tidy_user_settings(state, mod, skip_real, touched=True):
    """Undo what the plugin CLI left in the user settings file: back to its
    backup's bytes when nothing else changed there, else drop empty keys.
    When the plugin CLI never wrote there, only the file's kept record goes."""
    us = mod.get("user_settings") or settings_path(None)  # version 1 named none: the default one
    if os.path.realpath(us) == skip_real:
        return None
    key, rec = find_record(state, us)
    if rec is not None and rec.get("installed"):
        return None  # ctempo's statusLine is still there: its own uninstall restores the file
    if not touched or not os.path.exists(os.path.realpath(us)):
        _drop_records(state, os.path.realpath(us))  # the mod goes, and so does the file's kept record
        return None
    # Its own record (a statusLine uninstalled earlier, kept for this), else
    # the snapshot taken before the plugin CLI first wrote to it.
    base = dict(rec) if rec is not None else {
        "settings_path": us, "had_file": mod.get("had_file"),
        "backup_path": mod.get("backup_path"), "original_sha256": mod.get("original_sha256")}
    hk = dict(base.get("had_keys") or {})
    for k, had in (mod.get("had_keys") or {}).items():
        hk[k] = hk.get(k, had) and had
    base["had_keys"] = hk
    try:
        obj, raw, fmt = read_settings(us)
    except ValueError as e:
        _drop_records(state, os.path.realpath(us))
        return "Left %s alone: %s" % (us, e)
    _, new_raw = restore(obj, raw, fmt, base)
    _drop_records(state, os.path.realpath(us))
    if new_raw is None:
        os.remove(os.path.realpath(us))
        return "Removed %s (it did not exist before the mod was installed)" % us
    if new_raw == raw or new_raw == (raw or "").encode("utf-8"):
        return None
    atomic_write(us, new_raw)
    return "Tidied %s" % us


def _drop_records(state, real):
    """Remove every record of a file, whatever name it was recorded under."""
    records = state.setdefault("records", {})
    for p in [p for p in records if os.path.realpath(p) == real]:
        records.pop(p, None)


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
    key, rec = find_record(state, spath)
    rec = dict(rec or {})
    rec.setdefault("settings_path", spath)
    real = os.path.realpath(spath)
    mod = state.get("mod") or {}
    rebuilt = None if mod else _listed_mod()
    if rebuilt:
        # install.json was lost (or this is another CTEMPO_HOME), but the user file
        # still lists the mod: uninstall removes it all the same (--no-mod keeps it,
        # and runtime.json with it).
        mod = state["mod"] = rebuilt
    us = mod.get("user_settings")
    if mod.get("manual") and _mod_listed(us):
        mod.pop("manual")  # the user ran the printed commands: the plugin CLI has written there
    _forget_keys(mod)
    if mod.get("had_keys") and os.path.realpath(us or "") == real:
        # This is the file the plugin CLI edits: its record of plugin keys counts.
        hk = dict(rec.get("had_keys") or {})
        for k, had in mod["had_keys"].items():
            hk[k] = hk.get(k, had) and had
        rec["had_keys"] = hk
        if "had_file" not in rec:  # no record of its own: the snapshot taken before the plugin CLI wrote here
            rec.update({k: mod[k] for k in ("had_file", "backup_path", "original_sha256") if k in mod})
    if not os.path.exists(real):
        out("Settings: %s does not exist; nothing to restore there." % spath)
        obj, raw, fmt = None, None, None
    else:
        try:
            obj, raw, fmt = read_settings(spath)
        except ValueError as e:
            out("error: %s" % e)
            return 1
        out("Settings: %s" % spath)
    others = [p for p in installed_records(state) if os.path.realpath(p) != real]
    cmds = unmod_commands() if (mod and not args.no_mod and not others) else []
    if rebuilt and cmds:
        out("  (%s lists the ctempo mod, but %s has no record of it: uninstall removes it too)" % (us, state_path()))
    for c in cmds:
        out("  claude " + " ".join(c))
    # With claude not on PATH and no trace of the mod in the user file (the
    # commands never ran or failed, or the user undid them by hand), whatever
    # the record says, there is nothing to undo.
    never_ran = bool(cmds) and not claude_bin() and not _mod_listed(us)
    # The plugin CLI never wrote to the user file (install left the commands to
    # the user, who never ran them, or they timed out), so it is not ours to
    # rewrite, unless the commands below write there.
    untouched = bool(cmds and mod.get("manual")) and not _cli_wrote(mod)
    if never_ran:
        out("  (claude is not on PATH and %s lists no ctempo mod: nothing to undo)" % us)
    cur = obj.get("statusLine") if obj else None
    ours = isinstance(cur, dict) and is_ours(cur.get("command"))
    if obj is not None and not ours:
        out("  statusLine is not ctempo's; it is left alone")
    if not args.apply:
        if obj is not None and ours:
            new_obj, new_raw = restore(obj, raw, fmt, rec)
            d = diff(raw, new_raw if isinstance(new_raw, str) else (new_raw or b"").decode("utf-8"), spath) \
                if new_raw is not None else "  (file would be removed: it did not exist before install)\n"
            out(d if d else "  (no change)")
        if cmds:
            out("  (the plugin commands also remove enabledPlugins / extraKnownMarketplaces entries)")
        out("DRY RUN: nothing was written. Re-run with --apply to uninstall.")
        return 0
    mod_ok = True
    before = _peek(us)
    for c in [] if never_ran else cmds:
        code, so, se = run_claude(c)
        msg = (so.strip() or se.strip()).splitlines()
        out("  claude %s -> exit %d %s" % (" ".join(c[:3]), code, msg[-1] if msg else ""))
        mod_ok = mod_ok and _done(code, so, se)
    untouched = untouched and _peek(us) == before
    if os.path.exists(real):
        obj, raw, fmt = read_settings(spath)  # the plugin CLI may have rewritten (or created) it
        cur = obj.get("statusLine")
        ours = isinstance(cur, dict) and is_ours(cur.get("command"))
        mine = not (untouched and os.path.realpath(us) == real)  # not the user file the plugin CLI never wrote to
        if ours or (cmds and mod_ok and mine):
            new_obj, new_raw = restore(obj, raw, fmt, rec if (mod_ok or not cmds) else dict(rec, had_keys={}))
            if new_raw is None:
                os.remove(real)
                out("Removed %s (it did not exist before install)" % spath)
            elif new_raw != raw and new_raw != (raw or "").encode("utf-8"):
                atomic_write(spath, new_raw)
                out("Restored %s" % spath)
    if cmds and mod_ok:
        msg = _tidy_user_settings(state, mod, real, touched=not untouched)
        if msg:
            out(msg)
    _drop_records(state, real)
    # A mod record without user_settings (version 1) had its commands edit the default user file.
    if mod and not (cmds and mod_ok) and os.path.realpath(mod.get("user_settings") or settings_path(None)) == real:
        # The plugin keys in this file outlive this run (the mod stays for
        # another settings file, or its commands failed): keep the record so
        # whoever removes the mod can bring the file back byte for byte.
        records[spath] = dict(rec, installed=False)
    rc = 0
    if not installed_records(state):
        if cmds and not mod_ok:
            out("warning: the plugin commands failed, so the ctempo mod may still be installed. "
                "Run `ctempo uninstall --apply` again with claude on PATH, or the commands above yourself.")
            rc = 1  # keep state["mod"] and runtime.json so a rerun retries
        else:
            if cmds and mod_ok:
                state.pop("mod", None)
            if state.get("mod"):  # --no-mod: the mod stays, and it reads runtime.json
                out("Kept the mod (--no-mod) and %s, which it reads; `ctempo uninstall --apply` removes both."
                    % config.runtime_path())
            else:
                lp = state.pop("launcher", None)
                if lp and os.path.exists(lp) and LAUNCHER_MARK in read_text(lp):
                    os.remove(lp)
                    out("Removed launcher %s" % lp)
                if os.path.exists(config.runtime_path()):
                    os.remove(config.runtime_path())
    save_state(state)
    if rc == 0:
        out("Uninstalled. Your data stays in %s (delete it yourself if you want)." % config.home())
    else:
        out("Partly uninstalled.")
    return rc


def restore(obj, raw, fmt, rec):
    """Settings with ctempo's statusLine undone: (obj, text or bytes), or (obj, None) to delete the file."""
    new = dict(obj)
    cur = obj.get("statusLine")
    if isinstance(cur, dict) and is_ours(cur.get("command")):
        orig = rec.get("original_statusLine")
        orig_ours = isinstance(orig, dict) and is_ours(orig.get("command"))
        usable = rec.get("had_statusLine") is False or (rec.get("had_statusLine") is True and not orig_ours)
        found = None if usable or not rec.get("settings_path") else _original_from_backups(rec["settings_path"])
        if found is not None:
            # No usable record: rebuild it as install would (a pre-ctempo backup), so
            # a command the user put in --compose wins over the backup here too.
            rec = dict(rec, had_statusLine=found[0], original_statusLine=found[1])
        rec = _with_compose(rec, cur)
        orig = rec.get("original_statusLine")
        orig_ours = isinstance(orig, dict) and is_ours(orig.get("command"))
        if rec.get("had_statusLine") is True and not orig_ours:
            new["statusLine"] = orig
        elif rec.get("had_statusLine") is False:
            new.pop("statusLine", None)
        else:  # no usable record and no backup from before ctempo: what the command says
            comp = compose_of(cur["command"])
            if comp:
                new["statusLine"] = dict(cur, command=comp)
            else:
                new.pop("statusLine", None)
    for k, had in (rec.get("had_keys") or {}).items():
        if not had and k in new and new[k] in ({}, []):
            new.pop(k)
    pristine = rec.get("pristine") or {}
    if rec.get("had_file") is False and not new:
        return new, None
    # Byte-exact when nothing else changed: hand back the backup's bytes (the
    # file before this install, else the one before the mod).
    for snap in (rec, pristine):
        backup = snap.get("backup_path")
        if not (backup and os.path.exists(backup)):
            continue
        try:
            with open(backup, "rb") as f:
                orig_bytes = f.read()
            text = orig_bytes.decode("utf-8")
            orig_obj = json.loads(text) if text.strip() else {}  # a blank file is {}, as in read_settings
            if orig_obj == new and hashlib.sha256(orig_bytes).hexdigest() == snap.get("original_sha256"):
                return new, orig_bytes
        except (OSError, ValueError):
            pass
    if pristine.get("had_file") is False and not new:  # no file before the mod, nor one to come back to
        return new, None
    if new == obj and raw is not None:
        return new, raw
    return new, dumps(new, fmt)
