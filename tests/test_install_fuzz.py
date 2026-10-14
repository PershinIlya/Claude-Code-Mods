"""Model-based fuzzing of install / uninstall on throwaway settings files.

Random sequences (fixed seeds, so every run is the same) of installs into the
user file or a project file, uninstalls, claude coming and going, and the user
editing either file between steps. A model tracks what each file holds for the
user: its first content with the user's edits applied. Once claude is back
and everything is uninstalled, ctempo must have left no trace: no records, no
mod, no runtime.json, each file's JSON is the user's (with the user's own
statusLine and no ctempo plugin keys), and a file the user never edited is
byte for byte what it was, or still absent.

A failure prints the seed and the steps. To replay one seed with each step's
output: CTEMPO_FUZZ_SEED=<seed> python3 -m unittest tests.test_install_fuzz
"""

import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
import traceback
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ctempo import config, install

from . import test_install as ti

SEQUENCES = 300

# The user's own statusLine, in hand-formatted text.
WITH_STATUSLINE = ('{\n  "model": "opus",\n  "statusLine": {"type": "command", "command": "~/bin/line.sh", "padding": 1},\n'
                   '    "env": {"FOO": "bär"}\n}\n')
INITIAL = {
    "absent": None,
    "blank": "",
    "irregular": ti.IRREGULAR,
    "crlf": ti.IRREGULAR.replace("\n", "\r\n"),
    "statusline": WITH_STATUSLINE,
    "statusline-crlf": WITH_STATUSLINE.replace("\n", "\r\n"),
    "plugins": ti.WITH_PLUGINS,
}
CLAUDE = ("ok", "missing", "hang")


def hang(cmd, env=None):
    return 124, "", "claude %s timed out after 120 s" % " ".join(cmd[:3])


def fast_python(explicit=None):
    """The interpreter probe is not under test here, and it is slow."""
    return sys.executable, [(sys.executable, True, "3", "3", 1.0, "")]


def load(text):
    """A settings file as the user sees it: absent and blank are {}."""
    return json.loads(text) if text and text.strip() else {}


def generate(seed):
    """(initial file kinds, claude at the start, steps) for one seed."""
    r = random.Random(seed)
    initial = {"U": r.choice(sorted(INITIAL)), "P": r.choice(sorted(INITIAL))}
    claude = r.choice(("ok", "ok", "missing", "hang"))
    steps = []
    for i in range(r.randint(4, 12)):
        x = r.random()
        f = r.choice("UP")
        if x < 0.35:
            steps.append("i" + f + ("-nomod" if r.random() < 0.3 else "") + ("-replace" if r.random() < 0.2 else ""))
        elif x < 0.6:
            steps.append("u" + r.choice("UUPP*") + ("-nomod" if r.random() < 0.3 else ""))
        elif x < 0.75:
            steps.append("claude=" + r.choice(CLAUDE))
        else:
            what = r.choice(("key", "key", "reformat", "delete", "blank"))
            if what == "key":
                what = "k%d=%d" % (r.randint(0, 2), i)
            steps.append("edit %s %s" % (f, what))
    return initial, claude, steps


class World:
    """One sequence: its temp files, the claude in use, and the model."""

    def __init__(self, root, initial, claude):
        self.paths = {"U": os.path.join(root, "claude", "settings.json"),
                      "P": os.path.join(root, "proj", ".claude", "settings.json")}
        self.initial, self.user, self.edited = {}, {}, set()
        for f, kind in initial.items():
            os.makedirs(os.path.dirname(self.paths[f]))
            self.initial[f] = INITIAL[kind]
            if INITIAL[kind] is not None:
                self.write(f, INITIAL[kind])
            self.user[f] = load(INITIAL[kind])  # what the file holds for the user
        self.claude = claude
        self.trace = []

    def text(self, f):
        return install.read_text(self.paths[f]) if os.path.exists(self.paths[f]) else None

    def write(self, f, text):
        with open(self.paths[f], "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def run(self, name, fn, target, **kw):
        exe, cli = {"ok": ("/bin/claude", ti.plugin_cli(self.paths["U"])), "missing": (None, ti.claude_missing),
                    "hang": ("/bin/claude", hang)}[self.claude]
        buf = io.StringIO()
        with mock.patch.object(install, "claude_bin", lambda: exe), mock.patch.object(install, "run_claude", cli), \
                redirect_stdout(buf):
            rc = fn(ti.args(target, apply=True, **kw))
        self.trace.append((name, rc, buf.getvalue()))
        return rc

    def step(self, s):
        if s.startswith("claude="):
            self.claude = s.split("=")[1]
            return 0
        if s.startswith("edit "):
            _, f, what = s.split()
            self.edit(f, what)
            return 0
        fn = install.install if s[0] == "i" else install.uninstall
        return self.run(s, fn, self.paths.get(s[1]), no_mod="-nomod" in s, replace="-replace" in s)

    def edit(self, f, what):
        """The user edits the file by hand; the model follows."""
        text = self.text(f)
        if what == "delete":
            if text is None:
                return
            os.remove(self.paths[f])
            self.user[f] = {}
        elif what == "blank":
            self.write(f, "")
            self.user[f] = {}
        elif what == "reformat":  # same JSON
            if not (text and text.strip()):
                return
            self.write(f, json.dumps(json.loads(text), indent=4, ensure_ascii=False) + "\n")
        else:  # add or change a key ctempo has nothing to do with
            key, value = what.split("=")
            obj = load(text)
            eol = "\r\n" if text and "\r\n" in text else "\n"
            if key in obj:
                new = re.sub(r'("%s":\s*)-?\d+' % key, r"\g<1>" + value, text, count=1)
            elif obj:
                new = text.replace("{", "{" + eol + '  "%s": %s,' % (key, value), 1)
            else:
                new = "{" + eol + '  "%s": %s' % (key, value) + eol + "}" + eol
            self.write(f, new)
            self.user[f][key] = int(value)
        self.edited.add(f)

    def finish(self):
        """Claude back, then uninstall whatever is installed until nothing is."""
        self.claude = "ok"
        for _ in range(4):
            state = install.load_state()
            live = sorted(install.installed_records(state))
            if not live and "mod" not in state:
                return
            for target in live or [None]:
                self.run("cleanup u%s" % ("*" if target is None else "UP"[target == self.paths["P"]]),
                         install.uninstall, target, no_mod=False)

    def problems(self):
        out = []
        state = install.load_state()
        if state.get("records"):
            out.append("records left: %s" % state["records"])
        if "mod" in state:
            out.append("mod left: %s" % state["mod"])
        if os.path.exists(config.runtime_path()):
            out.append("runtime.json left")
        for f in "UP":
            text = self.text(f)
            try:
                got = load(text)
            except ValueError:
                got = "invalid JSON"
            if got != self.user[f]:
                out.append("%s holds %r, the user's is %r" % (f, got, self.user[f]))
            if f not in self.edited and text != self.initial[f]:
                out.append("%s never edited, but %r is now %r" % (f, self.initial[f], text))
        return out


def run_sequence(seed):
    """(initial, claude, steps, problems, trace) for one seed, in its own temp dirs."""
    initial, claude, steps = generate(seed)
    root = tempfile.mkdtemp(prefix="ctempo-fuzz-")
    env = {"CTEMPO_HOME": os.path.join(root, "home"), "CLAUDE_CONFIG_DIR": os.path.join(root, "claude"),
           "HOME": os.path.join(root, "userhome")}
    problems, world = [], None
    try:
        with mock.patch.dict(os.environ, env), mock.patch.object(install, "choose_python", fast_python):
            world = World(root, initial, claude)
            try:
                for s in steps:
                    world.step(s)
                world.finish()
                problems += ["%s exited %r" % (name, rc) for name, rc, _ in world.trace if rc not in (0, 1)]
                problems += world.problems()
            except Exception:
                problems.append("raised: " + traceback.format_exc().strip().splitlines()[-1])
                problems.append(traceback.format_exc())
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return initial, claude, steps, problems, world.trace if world else []


def describe(seed, initial, claude, steps, problems, trace=()):
    return "seed %d: U %s, P %s, claude %s\n  steps: %s\n  exits: %s\n  %s" % (
        seed, initial["U"], initial["P"], claude, ", ".join(steps),
        ", ".join("%s %d" % (name, rc) for name, rc, _ in trace), "\n  ".join(problems))


class Fuzz(unittest.TestCase):
    def test_random_sequences_leave_no_trace(self):
        replay = os.environ.get("CTEMPO_FUZZ_SEED")
        seeds = [int(replay)] if replay else range(SEQUENCES)
        failures = []
        for seed in seeds:
            initial, claude, steps, problems, trace = run_sequence(seed)
            if replay:
                for name, rc, out in trace:
                    print("== %s -> %d\n%s" % (name, rc, out))
            if problems:
                failures.append(describe(seed, initial, claude, steps, problems, trace))
        self.assertFalse(failures, "%d of %d sequences failed (replay one with CTEMPO_FUZZ_SEED=<seed>):\n%s" % (
            len(failures), len(seeds), "\n".join(failures[:10])))


if __name__ == "__main__":
    unittest.main()
