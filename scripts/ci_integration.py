#!/usr/bin/env python3
"""ctempo end to end, the way Claude Code runs it, in throwaway folders.

    python scripts/ci_integration.py [--no-marketplace] [--keep]

CI runs it on Windows and macOS (.github/workflows/tests.yml); it runs on
Linux too. CTEMPO_HOME, CLAUDE_CONFIG_DIR, HOME and USERPROFILE point into
a temporary folder for every step: the real ones are never read or written.

1. Report: the shell Claude Code would run the statusLine with here
   (winshell.predict_shell(), which matters on Windows) and the installer's
   interpreter report (install.choose_python()).
2. For each starting settings.json (no statusLine; CRLF line endings; a
   statusLine of the user's to compose with; ctempo and its interpreter in
   folders with a space): `python bin/ctempo install --apply --settings
   <tmp>/settings.json --no-bootstrap --no-mod`, then the statusLine command
   is read back and a statusLine payload with rate limits is piped into it
   through each shell Claude Code may use on this OS, with COLUMNS set
   (Windows: Git Bash's `bash.exe -c`, and pwsh and powershell.exe with
   -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command; elsewhere
   `/bin/sh -c`). Each must exit 0 and print the ctempo line with its UTF-8
   intact. A command that reads the same in every shell must work in every
   one; one written for a single shell (a quoted interpreter on Windows), in
   that shell, which must be the one Claude Code would use. Then `ctempo
   uninstall --apply`, after which settings.json must be byte for byte what
   it was.
3. Which forms of this repo's path `claude plugin marketplace add` accepts
   (absolute; with forward slashes on Windows; ./ with the repo as the
   working directory), each in its own temporary config, and whether
   `claude plugin install` works from the first accepted one. Reported
   only: this part never fails the run.

Exit 1 when a check in 1 or 2 fails.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)

from ctempo import winshell  # noqa: E402

WINDOWS = os.name == "nt"
GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
PS_FLAGS = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
COLUMNS = "120"  # Claude Code sets COLUMNS from its terminal
SEP = "\u00b7"  # the line's separator: not ASCII, so it shows whether UTF-8 came through intact
FAILURES = []
ROWS = []  # (scenario, shell, result) for the GitHub step summary

REPORT = r"""
import sys
sys.path.insert(0, sys.argv[1])
from ctempo import config, install, winshell
print("winshell.predict_shell(): %r" % (winshell.predict_shell(),))
if not config.IS_WINDOWS:
    print("  (Windows only: here Claude Code runs the statusLine with /bin/sh -c)")
elif hasattr(install, "windows_candidates"):
    print("install.windows_candidates(): %r" % (install.windows_candidates(),))
chosen, report = install.choose_python()
print("install.choose_python():")
for row in report:
    path, ok, ver, sq, ms, note = row[:6]
    print("  %s %s: %s%s%s" % ("*" if path == chosen else ("+" if ok else "-"), path,
                               ("Python %s, sqlite %s" % (ver, sq)) if ver else "failed",
                               (", %.0f ms" % ms) if ms else "", (" (%s)" % note) if note else ""))
print("  chosen: %s" % chosen)
"""


def fail(msg):
    FAILURES.append(msg)
    print("FAIL: " + msg)
    if os.environ.get("GITHUB_ACTIONS"):
        print("::error::" + msg.replace("\n", " "))


def show(text, prefix="    | "):
    for line in text.rstrip().splitlines():
        print(prefix + line)


def sandbox(tmp):
    """The environment of a user whose every folder is under tmp."""
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ)
    env.update(CTEMPO_HOME=os.path.join(tmp, "ctempo"), CLAUDE_CONFIG_DIR=os.path.join(tmp, "claude"),
               HOME=home, USERPROFILE=home, COLUMNS=COLUMNS,
               DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    return env


def hidden():
    """Claude Code spawns the statusLine with windowsHide: no console window."""
    if not WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}


def family(shell):
    return "powershell" if shell in ("pwsh", "powershell") else shell


def shells(env):
    """(name, argv before the command, env) of each shell Claude Code may run the statusLine
    with here that exists (report() fails the run for a missing one)."""
    if not WINDOWS:
        return [("sh", ["/bin/sh", "-c"], env)]
    found = []
    if os.path.exists(GIT_BASH):
        e = dict(env)  # Claude Code puts Git Bash's folder first on PATH
        e["PATH"] = os.path.dirname(GIT_BASH) + os.pathsep + e.get("PATH", "")
        found.append(("bash", [GIT_BASH, "-c"], e))
    for name in ("pwsh", "powershell"):  # Claude Code prefers pwsh
        exe = shutil.which(name, path=env.get("PATH"))
        if exe:
            found.append((name, [exe] + PS_FLAGS, env))
    return found


def shell_versions(env):
    found = shells(env)
    for name, argv, e in found:
        if name == "sh":
            print("  sh: /bin/sh -> %s" % os.path.realpath("/bin/sh"))
            continue
        probe = "echo $BASH_VERSION" if name == "bash" else "$PSVersionTable.PSVersion.ToString()"
        try:
            p = subprocess.run(argv + [probe], capture_output=True, env=e, timeout=60)
            print("  %s: %s (%s)" % (name, p.stdout.decode("utf-8", "replace").strip(), argv[0]))
        except (OSError, subprocess.SubprocessError) as ex:
            print("  %s: %r (%s)" % (name, ex, argv[0]))
    want = ["bash", "pwsh", "powershell"] if WINDOWS else ["sh"]
    for name in want:
        if name not in [f[0] for f in found]:
            fail("%s not found (%s)" % (name, GIT_BASH if name == "bash" else "PATH"))


def ctempo(entry, env, args):
    p = subprocess.run([sys.executable, entry] + args, env=env, capture_output=True, timeout=600)
    print("  $ python %s %s -> exit %d" % (entry, " ".join(args), p.returncode))
    show((p.stdout + p.stderr).decode("utf-8", "replace"))
    return p.returncode


def render(argv, cmd, env):
    """The command run as Claude Code runs it: the payload and a newline on stdin as UTF-8,
    stdout read as UTF-8, trimmed, split into lines, each trimmed, blank ones dropped."""
    now = time.time()
    data = json.dumps({
        "session_id": "ci-integration",
        "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
        "workspace": {"current_dir": ROOT, "project_dir": ROOT},
        "rate_limits": {
            "five_hour": {"used_percentage": 42, "resets_at": int(now + 2 * 3600)},
            "seven_day": {"used_percentage": 17, "resets_at": int(now + 3 * 86400)},
        },
    }) + "\n"
    t = time.perf_counter()
    try:
        p = subprocess.run(argv + [cmd], input=data.encode("utf-8"), capture_output=True, env=env, timeout=120,
                           **hidden())
    except subprocess.TimeoutExpired as e:  # reported as a failed render, and the run goes on
        p = subprocess.CompletedProcess(e.cmd, -9, e.stdout or b"", (e.stderr or b"") + b"\n(timed out after 120 s)")
    secs = time.perf_counter() - t
    try:
        text, utf8 = p.stdout.decode("utf-8"), True
    except UnicodeDecodeError:
        text, utf8 = p.stdout.decode("utf-8", "replace"), False
    lines = [ANSI.sub("", s).strip() for s in text.strip().split("\n")]
    return p, [s for s in lines if s], utf8, secs


def problems_of(p, lines, utf8, extra):
    ours = next((s for s in lines if "5h 42%" in s and "wk 17%" in s), None)
    out = []
    if p.returncode != 0:
        out.append("exit %d" % p.returncode)
    if ours is None:
        out.append("no ctempo line (5h 42% ... wk 17%)")
    elif not utf8 or SEP not in ours:
        out.append("UTF-8 garbled on the way (no %r in the line)" % SEP)
    if extra and not any(extra in s for s in lines):
        out.append("the composed command's %r is missing" % extra)
    return out


def try_shells(name, cmd, env, target=None, extra=None):
    """Run cmd through every shell; target None: it must work in all of them."""
    for sh, argv, e in shells(env):
        p, lines, utf8, secs = render(argv, cmd, e)
        print("  %-10s %4.1f s, exit %d: %s" % (sh, secs, p.returncode, " / ".join(lines) or "(no output)"))
        problems = problems_of(p, lines, utf8, extra)
        if not problems:
            ROWS.append((name, sh, "ok, %.1f s" % secs))
            continue
        if target and family(sh) != target:
            print("    (not required: the command is written for %s)" % target)
            ROWS.append((name, sh, "not required (%s)" % "; ".join(problems)))
            continue
        ROWS.append((name, sh, "FAILED: %s" % "; ".join(problems)))
        fail("%s, through %s: %s" % (name, sh, "; ".join(problems)))
        show(p.stderr.decode("utf-8", "replace"))


def recorded_shell(env, path):
    """The shell install.json records for this settings file (Windows), or None."""
    try:
        with open(os.path.join(env["CTEMPO_HOME"], "install.json"), encoding="utf-8-sig") as f:
            records = json.load(f).get("records") or {}
    except (OSError, ValueError, AttributeError):
        return None
    want = os.path.normcase(os.path.realpath(path))
    recs = [r for k, r in records.items() if os.path.normcase(os.path.realpath(k)) == want] or list(records.values())
    return recs[0].get(winshell.RECORD_KEY) if len(recs) == 1 and isinstance(recs[0], dict) else None


def target_shell(name, cmd, env, obj, path):
    """None when the command must run in every shell here, else the one it is written for."""
    if not WINDOWS:
        return None
    target = winshell.written_for(cmd)
    settings_env = obj.get("env") if isinstance(obj, dict) else None
    predicted = winshell.predict_shell(env, settings_env)[0]
    print("  written for: %s; Claude Code here would use %s; install.json records %s"
          % (target or "every shell", predicted, recorded_shell(env, path)))
    if target and target != predicted:
        fail("%s: the command is written for %s, but Claude Code here runs %s" % (name, target, predicted))
    return target


def scenario(name, tmp, before, entry, python=None, extra=None):
    """Install into a settings file holding `before`, render through the shells,
    uninstall, and compare the file with what it was."""
    print("\n== %s" % name)
    env = sandbox(tmp)
    path = os.path.join(tmp, "settings.json")
    with open(path, "wb") as f:
        f.write(before)
    args = ["install", "--apply", "--settings", path, "--no-bootstrap", "--no-mod"]
    if ctempo(entry, env, args + (["--python", python] if python else [])) != 0:
        return fail("%s: install failed" % name)
    try:
        with open(path, encoding="utf-8-sig") as f:
            obj = json.load(f)
        cmd = obj["statusLine"]["command"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return fail("%s: no statusLine command after install (%r)" % (name, e))
    print("  statusLine command: %s" % cmd)
    try_shells(name, cmd, env, target_shell(name, cmd, env, obj, path), extra)
    log = os.path.join(env["CTEMPO_HOME"], "errors.log")
    if os.path.exists(log):
        fail("%s: ctempo logged errors" % name)
        with open(log, encoding="utf-8", errors="replace") as f:
            show(f.read())
    if ctempo(entry, env, ["uninstall", "--apply", "--settings", path, "--no-mod"]) != 0:
        return fail("%s: uninstall failed" % name)
    with open(path, "rb") as f:
        after = f.read()
    if after != before:
        fail("%s: settings.json differs after uninstall:\n  before %r\n  after  %r" % (name, before, after))
    else:
        print("  settings.json is byte for byte what it was")
    return env


def spaced_copy(base):
    """ctempo copied into base (a folder with a space), and a venv interpreter there (None if no venv)."""
    for part in ("bin", "ctempo"):
        shutil.copytree(os.path.join(ROOT, part), os.path.join(base, "repo", part),
                        ignore=shutil.ignore_patterns("__pycache__"))
    venv = os.path.join(base, "venv")
    p = subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv], capture_output=True, timeout=300)
    python = os.path.join(venv, "Scripts", "python.exe") if WINDOWS else os.path.join(venv, "bin", "python")
    if p.returncode != 0 or not os.path.exists(python):
        print("  (no venv, so the interpreter's path has no space: %s)" % p.stderr.decode("utf-8", "replace").strip())
        python = None
    return os.path.join(base, "repo", "bin", "ctempo"), python


def own_forms(env, python, entry):
    """Windows: ctempo's command in each shell's own syntax (what install writes when a
    path must be quoted) runs in that shell, whichever one Claude Code would pick."""
    argv = [python.replace("\\", "/")] + winshell.python_flags(True) + [entry.replace("\\", "/"), "statusline"]
    for sh, sargv, e in shells(env):
        name = "the %s form" % family(sh)
        cmd = winshell.command_line(argv, family(sh))
        print("\n== %s, through %s: %s" % (name, sh, cmd))
        p, lines, utf8, secs = render(sargv, cmd, e)
        print("  %-10s %4.1f s, exit %d: %s" % (sh, secs, p.returncode, " / ".join(lines) or "(no output)"))
        problems = problems_of(p, lines, utf8, None)
        ROWS.append((name, sh, "FAILED: %s" % "; ".join(problems) if problems else "ok, %.1f s" % secs))
        if problems:
            fail("%s, through %s: %s" % (name, sh, "; ".join(problems)))
            show(p.stderr.decode("utf-8", "replace"))


def report(tmp):
    print("\n== The statusLine shell and the installer's interpreter report")
    env = sandbox(tmp)
    shell_versions(env)
    p = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", REPORT, ROOT], env=env, capture_output=True,
                       timeout=300)
    show((p.stdout + p.stderr).decode("utf-8", "replace"), "  ")
    if p.returncode != 0:
        fail("the interpreter report failed (exit %d)" % p.returncode)


def marketplace(tmp):
    print("\n== claude plugin marketplace add (reported only, never fails the run)")
    claude = shutil.which("claude")
    if not claude:
        print("  claude is not on PATH (npm install -g @anthropic-ai/claude-code): not tried")
        return

    def run(args, env, cwd):
        try:
            p = subprocess.run([claude] + args, env=env, cwd=cwd, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as e:
            print("  $ claude %s (in %s) -> %r" % (" ".join(args), cwd, e))
            return None, ""
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        print("  $ claude %s (in %s) -> exit %d" % (" ".join(args), cwd, p.returncode))
        show(out)
        return p.returncode, out

    forms = [("absolute path", ROOT, None)]
    if WINDOWS:
        forms.append(("absolute path with forward slashes", ROOT.replace("\\", "/"), None))
    forms.append(("./ with the repo as the working directory", "./", ROOT))
    accepted = []
    for i, (label, source, cwd) in enumerate(forms):
        print("\n  -- %s: %s" % (label, source))
        env = sandbox(os.path.join(tmp, str(i)))
        cwd = cwd or env["HOME"]
        if i == 0:
            run(["--version"], env, cwd)
        code, _ = run(["plugin", "marketplace", "add", source], env, cwd)
        _, listed = run(["plugin", "marketplace", "list", "--json"], env, cwd)
        ok = code == 0 and "claude-tempo" in listed
        ROWS.append(("marketplace add", label, "accepted" if ok else "rejected"))
        if ok:
            accepted.append((label, env, cwd))
    names = [a[0] for a in accepted]
    print("\n  marketplace add accepts: %s" % (", ".join(names) or "none of the forms"))
    print("  marketplace add rejects: %s" % (", ".join(f[0] for f in forms if f[0] not in names) or "none of the forms"))
    if os.environ.get("GITHUB_ACTIONS"):
        print("::notice title=claude plugin marketplace add::accepts: %s" % (", ".join(names) or "none of the forms"))
    if accepted:
        label, env, cwd = accepted[0]
        print("\n  -- plugin install, after the marketplace was added by %s" % label)
        code, _ = run(["plugin", "install", "ctempo@claude-tempo", "--scope", "user"], env, cwd)
        _, listed = run(["plugin", "list", "--json"], env, cwd)
        ROWS.append(("plugin install", "ctempo@claude-tempo",
                     "installed" if code == 0 and "ctempo@claude-tempo" in listed else "failed"))


def summary():
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not ROWS:
        return
    lines = ["### ctempo integration on %s, Python %s" % (sys.platform, sys.version.split()[0]), "",
             "| step | shell or form | result |", "| --- | --- | --- |"]
    lines += ["| %s | %s | %s |" % tuple(c.replace("|", "/") for c in row) for row in ROWS]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")


def main(argv):
    for stream in (sys.stdout, sys.stderr):  # a pipe in CI: the ANSI code page on Windows
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-marketplace", action="store_true", help="skip the claude plugin marketplace forms")
    ap.add_argument("--keep", action="store_true", help="keep the temporary folder")
    args = ap.parse_args(argv)
    print("%s, Python %s (%s), repo %s" % (sys.platform, sys.version.split()[0], sys.executable, ROOT))
    tmp = tempfile.mkdtemp(prefix="ctempo-ci-")
    try:
        report(os.path.join(tmp, "report"))
        entry = os.path.join(ROOT, "bin", "ctempo")
        scenario("no statusLine before", os.path.join(tmp, "add"), b'{\n  "theme": "dark"\n}\n', entry)
        scenario("CRLF line endings", os.path.join(tmp, "crlf"), b'{\r\n  "theme": "dark"\r\n}\r\n', entry)
        theirs = b'{\n  "statusLine": {\n    "type": "command",\n    "command": "echo mine"\n  }\n}\n'
        scenario("composed with the user's statusLine", os.path.join(tmp, "compose"), theirs, entry, extra="mine")
        spaces = os.path.join(tmp, "spaces", "with space")  # settings.json and CTEMPO_HOME too
        entry, python = spaced_copy(spaces)
        env = scenario("paths with a space", spaces, b"{}\n", entry, python=python)
        if WINDOWS and python and env:
            own_forms(env, python, entry)
        if not args.no_marketplace:
            marketplace(os.path.join(tmp, "marketplace"))
    finally:
        if args.keep:
            print("\nkept %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    summary()
    print("\n%s" % ("%d failure(s):\n  %s" % (len(FAILURES), "\n  ".join(FAILURES)) if FAILURES else "all good"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
