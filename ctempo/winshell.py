"""How Claude Code runs a statusLine command on Windows, and how to write one.

On Windows Claude Code runs the statusLine through Git Bash when it finds one
(`bash.exe -c "<command>"`), else through PowerShell (pwsh first, then
Windows PowerShell 5.1) as `-NoProfile -NonInteractive -ExecutionPolicy
Bypass -Command "<command>"`. The statusLine setting has no `shell` or `args`
field, so ctempo predicts the shell the way Claude Code finds it, writes a
command that reads right there (D-17), and runs a composed command the same
way. A command made of bare words reads the same in both shells, so it keeps
working when the shell Claude Code picks changes.

Every function takes the environment as a mapping and uses Windows path rules
(ntpath) on any OS, so the Windows branches are tested anywhere;
config.IS_WINDOWS says when they apply.
"""

import ntpath
import os
import re
import subprocess

from . import config

SHELLS = {"bash": "Git Bash", "powershell": "PowerShell"}
# Key of a settings record in install.json: the shell the command was written for.
RECORD_KEY = "shell"
GIT_BASH = (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe")
# ${VAR} placeholders Claude Code rewrites to ${env:VAR} for PowerShell.
PS_PLACEHOLDERS = ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")
# PowerShell reads each of these as a single quote, inside a quoted word too.
PS_QUOTES = "'\u2018\u2019\u201a\u201b"
# Characters that mean nothing to bash or PowerShell inside a word.
BARE_ASCII = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:+=-")


def python_flags(windows=None):
    """Interpreter flags for every Python command ctempo writes (statusLine,
    runtime.json, launcher). -I ignores PYTHONUTF8 and PYTHONIOENCODING, and on
    Windows a pipe gets the ANSI code page, so Windows adds -X utf8."""
    windows = config.IS_WINDOWS if windows is None else windows
    return ["-I", "-S", "-X", "utf8"] if windows else ["-I", "-S"]


# -- which shell ------------------------------------------------------------------
def getenv(env, name):
    """env[name], ignoring case as Windows does (a plain dict keeps the case it was given)."""
    if name in env:
        return env[name]
    up = name.upper()
    for k, v in env.items():
        if k.upper() == up:
            return v
    return None


def merged(env, settings_env=None):
    """The environment Claude Code runs with: its own, settings.json `env` over it."""
    out = dict(env)
    if isinstance(settings_env, dict):
        for k, v in settings_env.items():
            if isinstance(k, str) and isinstance(v, str):
                for old in [x for x in out if x.upper() == k.upper()]:
                    del out[old]
                out[k] = v
    return out


def which(name, env, is_file=os.path.isfile):
    """`name` with a PATHEXT extension in env's PATH, in order, as where.exe
    finds it. A match in the current directory is skipped, as Claude Code
    skips it. None if there is none."""
    exts = [e for e in (getenv(env, "PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if e]
    try:
        here = ntpath.normcase(ntpath.normpath(os.getcwd()))
    except OSError:
        here = None
    for d in (getenv(env, "PATH") or "").split(";"):
        d = d.strip().strip('"')
        if not d or ntpath.normcase(ntpath.normpath(d)) == here:
            continue
        for ext in exts:
            p = ntpath.join(d, name + ext)
            if is_file(p):
                return p
    return None


def git_bash(env, exists=os.path.exists, is_file=os.path.isfile):
    """Claude Code's Git Bash, or None: CLAUDE_CODE_GIT_BASH_PATH when it names
    an existing bash or sh, the two Program Files installs, then the git on
    PATH and ..\\..\\bin\\bash.exe from it."""
    given = getenv(env, "CLAUDE_CODE_GIT_BASH_PATH")
    if given and ntpath.basename(given).lower() in ("bash.exe", "sh.exe", "bash", "sh") and exists(given):
        return given
    for p in GIT_BASH:
        if exists(p):
            return p
    git = which("git", env, is_file)
    if git:
        p = ntpath.normpath(ntpath.join(git, "..", "..", "bin", "bash.exe"))
        if exists(p):
            return p
    return None


def powershell(env, is_file=os.path.isfile):
    """The PowerShell Claude Code falls back to, or None: pwsh on PATH, pwsh in
    its usual installs, then Windows PowerShell 5.1."""
    found = which("pwsh", env, is_file)
    if found:
        return found
    for var, parts in (("ProgramFiles", ("PowerShell", "7")), ("LOCALAPPDATA", ("Microsoft", "WindowsApps")),
                       ("USERPROFILE", (".dotnet", "tools"))):
        root = getenv(env, var)
        if root and is_file(ntpath.join(root, *parts, "pwsh.exe")):
            return ntpath.join(root, *parts, "pwsh.exe")
    found = which("powershell", env, is_file)
    if found:
        return found
    p = ntpath.join(getenv(env, "SYSTEMROOT") or r"C:\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return p if is_file(p) else None


def predict_shell(env=None, settings_env=None, exists=os.path.exists, is_file=os.path.isfile):
    """("bash", path) when Claude Code would run the statusLine in Git Bash,
    else ("powershell", path), path None when no PowerShell is found either.
    env defaults to os.environ; settings_env is settings.json `env`, which
    Claude Code applies over its own environment."""
    env = merged(os.environ if env is None else env, settings_env)
    bash = git_bash(env, exists, is_file)
    if bash:
        return "bash", bash
    return "powershell", powershell(env, is_file)


def first_word(cmd):
    """The first word as Claude Code reads it for its `.sh` rule: quotes
    dropped, a backslash takes the next character as is."""
    s, out, i = cmd.strip(), [], 0
    while i < len(s):
        c = s[i]
        if c in "\"'":
            j = s.find(c, i + 1)
            if j < 0:
                out.append(s[i + 1:])
                break
            out.append(s[i + 1:j])
            i = j + 1
        elif c == "\\" and i + 1 < len(s):
            out.append(s[i + 1])
            i += 2
        elif c.isspace():
            break
        else:
            out.append(c)
            i += 1
    return "".join(out)


def shell_argv(cmd, env):
    """(argv, env) that run `cmd` the way Claude Code runs a statusLine
    command on Windows: `bash -c` with bash's folder first on PATH (and `bash `
    before a first word ending in .sh), or PowerShell with Claude Code's flags
    and its ${env:...} placeholders. Never cmd.exe."""
    kind, path = predict_shell(env)
    env = dict(env)
    if kind == "bash":
        if path[:1] in ("/", "\\") or path[1:3] in (":/", ":\\"):  # path.win32.isAbsolute, as Claude Code checks
            key = next((k for k in env if k.upper() == "PATH"), "PATH")
            env[key] = ntpath.dirname(path) + (";" + env[key] if env.get(key) else "")
        return [path, "-c", "bash " + cmd if first_word(cmd).endswith(".sh") else cmd], env
    flags = ["-NoProfile", "-NonInteractive"]
    if not getenv(env, "CLAUDE_CODE_POWERSHELL_RESPECT_EXECUTION_POLICY"):
        flags += ["-ExecutionPolicy", "Bypass"]
    for name in PS_PLACEHOLDERS:
        cmd = cmd.replace("${%s}" % name, "${env:%s}" % name)
    return [path or "powershell.exe"] + flags + ["-Command", cmd], env


def kill_tree(pid, env=None):
    """End a process and every process it started (`taskkill /T /F`): Git
    Bash's bash.exe runs the real bash as its child. True if taskkill did."""
    root = getenv(os.environ if env is None else env, "SYSTEMROOT") or r"C:\Windows"
    try:
        r = subprocess.run([ntpath.join(root, "System32", "taskkill.exe"), "/T", "/F", "/PID", str(pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=2, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


# -- writing a command ------------------------------------------------------------
def slashes(path):
    """Forward slashes: Git Bash drops unquoted backslashes, Windows reads both."""
    return path.replace("\\", "/")


def is_bare(word):
    """True when the word means the same unquoted in bash and in PowerShell:
    ASCII letters, digits and _./:+=-, and non-ASCII letters and digits (so
    C:/Users/Jürgen/... needs no quotes); never empty."""
    return word != "" and all(c in BARE_ASCII or (ord(c) > 127 and c.isalnum()) for c in word)


def quote(word, shell):
    """The word as `shell` must read it: as is when bare, else single quotes
    (bash closes and reopens them around a ', PowerShell doubles it)."""
    if is_bare(word):
        return word
    if shell == "powershell":
        return "'" + "".join(c * 2 if c in PS_QUOTES else c for c in word) + "'"
    return "'" + word.replace("'", "'\"'\"'") + "'"


def command_line(argv, shell):
    """The command string that runs argv in `shell` ("bash" or "powershell"),
    each word quoted only when it needs it. Bare words come out the same for
    both shells, and so do quoted words without a single quote, except a
    quoted first word: PowerShell runs it only after `& `, which bash rejects."""
    out = [quote(w, shell) for w in argv]
    if shell == "powershell" and argv and (out[0] != argv[0] or argv[0][:1] in ".-"):
        return "& " + " ".join(out)
    return " ".join(out)


def passes_intact(word):
    """True when every PowerShell passes the word to a native program as is.
    Windows PowerShell 5.1 wraps a word with whitespace in double quotes but
    escapes nothing, so a double quote inside, or a backslash ending such a
    word, reaches the program changed (pwsh 7.3+ escapes both)."""
    return '"' not in word and not (re.search(r"\s", word) and word.endswith("\\"))


def words(cmd, shell):
    """The words `shell` reads in a command made of bare words and quoted
    ones (what command_line writes), or None when the command is not of that
    form, or would not run as a command there: `&` cannot start a bash
    command, and PowerShell needs `& ` before a quoted first word."""
    s = cmd.strip()
    if shell == "powershell":
        call = s[:1] == "&" and s[1:2].isspace()
        return _ps_words(s[1:] if call else s, call)
    return None if s[:1] == "&" else _bash_words(s)


def _bash_words(s):
    out, cur, i, n = [], None, 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t":
            if cur is not None:
                out.append(cur)
                cur = None
            i += 1
            continue
        cur = cur or ""
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                return None
            cur += s[i + 1:j]
            i = j + 1
        elif c == '"':  # only a plain double-quoted run, as the '"'"' in quote()
            j = s.find('"', i + 1)
            if j < 0 or any(x in s[i + 1:j] for x in "$`\\!"):
                return None
            cur += s[i + 1:j]
            i = j + 1
        elif is_bare(c):
            cur += c
            i += 1
        else:
            return None
    if cur is not None:
        out.append(cur)
    return out or None


def _ps_words(s, call):
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c in PS_QUOTES:
            word, j = [], i + 1
            while True:
                if j >= n:
                    return None
                if s[j] in PS_QUOTES:
                    if j + 1 < n and s[j + 1] in PS_QUOTES:
                        word.append(s[j + 1])
                        j += 2
                        continue
                    break
                word.append(s[j])
                j += 1
            if not out and not call:
                return None  # a quoted first word is a string, not a command
            out.append("".join(word))
            i = j + 1
        else:
            j = i
            while j < n and not s[j].isspace() and s[j] not in PS_QUOTES:
                j += 1
            word = s[i:j]
            if not is_bare(word) or (not out and not call and word[:1] in ".-"):
                return None
            out.append(word)
            i = j
        if i < n and not s[i].isspace():
            return None  # text glued to a quoted word: outside the simple form
    return out or None


def reads_alike(cmd):
    """True when bash and PowerShell read the same words in the command, so
    it runs whichever shell Claude Code picks."""
    b = words(cmd, "bash")
    return b is not None and b == words(cmd, "powershell")


def written_for(cmd):
    """The one shell the command runs in ("bash" or "powershell"), or None:
    it reads the same in both, or is not of the simple form, or reads
    differently in each (a quoted ' in a word)."""
    b, p = words(cmd, "bash"), words(cmd, "powershell")
    if b is not None and p is None:
        return "bash"
    if p is not None and b is None:
        return "powershell"
    return None
