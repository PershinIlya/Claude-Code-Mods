"""install / uninstall on throwaway settings files."""

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ctempo import install

SETTINGS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(git status)"], "deny": []},
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]},
    "env": {"FOO": "bär"},
}


def args(settings, **kw):
    a = dict(apply=False, settings=settings, no_mod=True, python=sys.executable, replace=False, bin_dir=None)
    a.update(kw)
    return argparse.Namespace(**a)


class Install(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctempo-inst-")
        self.home = os.path.join(self.dir, "home")
        self.env = mock.patch.dict(os.environ, {"CTEMPO_HOME": self.home})
        self.env.start()
        self.path = os.path.join(self.dir, "settings.json")

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, obj, indent=2, newline=False):
        text = json.dumps(obj, indent=indent, ensure_ascii=False) + ("\n" if newline else "")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)
        return text

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def run_cmd(self, fn, a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = fn(a)
        return rc, buf.getvalue()

    def test_dry_run_writes_nothing(self):
        before = self.write(SETTINGS)
        rc, out = self.run_cmd(install.install, args(self.path))
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn('+  "statusLine"', out)
        self.assertEqual(self.read(), before)
        self.assertFalse(os.path.exists(self.home))

    def test_apply_then_uninstall_is_byte_identical(self):
        for indent, newline in ((2, False), (2, True), (4, True), ("\t", False)):
            with self.subTest(indent=indent, newline=newline):
                before = self.write(SETTINGS, indent, newline)
                rc, out = self.run_cmd(install.install, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                after = json.loads(self.read())
                self.assertEqual({k: v for k, v in after.items() if k != "statusLine"}, SETTINGS)
                self.assertTrue(install.is_ours(after["statusLine"]["command"]))
                self.assertEqual(after["statusLine"]["refreshInterval"], 60)
                backups = [f for f in os.listdir(self.dir) if ".ctempo-backup-" in f]
                self.assertTrue(backups)
                with open(os.path.join(self.dir, backups[-1]), encoding="utf-8") as f:
                    self.assertEqual(f.read(), before)
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.read(), before)
                for b in backups:
                    os.remove(os.path.join(self.dir, b))
                shutil.rmtree(self.home)

    def test_compose_with_existing_statusline_and_restore(self):
        s = dict(SETTINGS, statusLine={"type": "command", "command": "~/.claude/my line.sh", "padding": 1, "refreshInterval": 5})
        before = self.write(s, newline=True)
        self.run_cmd(install.install, args(self.path, apply=True))
        sl = json.loads(self.read())["statusLine"]
        self.assertEqual(install.compose_of(sl["command"]), "~/.claude/my line.sh")
        self.assertEqual(sl["padding"], 1)
        self.assertEqual(sl["refreshInterval"], 5)  # theirs was faster: keep it
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(self.read(), before)

    def test_replace_flag(self):
        s = dict(SETTINGS, statusLine={"type": "command", "command": "theirs"})
        before = self.write(s)
        self.run_cmd(install.install, args(self.path, apply=True, replace=True))
        sl = json.loads(self.read())["statusLine"]
        self.assertIsNone(install.compose_of(sl["command"]))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(self.read(), before)

    def test_idempotent(self):
        self.write(SETTINGS)
        self.run_cmd(install.install, args(self.path, apply=True))
        once = self.read()
        rc, out = self.run_cmd(install.install, args(self.path, apply=True))
        self.assertIn("already runs ctempo", out)
        self.assertEqual(self.read(), once)
        backups = [f for f in os.listdir(self.dir) if ".ctempo-backup-" in f]
        self.assertEqual(len(backups), 1)

    def test_other_changes_after_install_survive_uninstall(self):
        self.write(SETTINGS)
        self.run_cmd(install.install, args(self.path, apply=True))
        obj = json.loads(self.read())
        obj["theme"] = "dark"
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, indent=2))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        after = json.loads(self.read())
        self.assertEqual(after, dict(SETTINGS, theme="dark"))

    def test_missing_file_is_created_and_removed(self):
        self.run_cmd(install.install, args(self.path, apply=True))
        self.assertIn("statusLine", json.loads(self.read()))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertFalse(os.path.exists(self.path))

    def test_refuses_invalid_json(self):
        with open(self.path, "w") as f:
            f.write("{ nope")
        rc, out = self.run_cmd(install.install, args(self.path, apply=True))
        self.assertEqual(rc, 1)
        self.assertIn("refusing", out)
        self.assertEqual(self.read(), "{ nope")

    def test_symlinked_settings_keep_the_link(self):
        real = os.path.join(self.dir, "dotfiles-settings.json")
        with open(real, "w") as f:
            f.write(json.dumps(SETTINGS, indent=2))
        os.symlink(real, self.path)
        self.run_cmd(install.install, args(self.path, apply=True))
        self.assertTrue(os.path.islink(self.path))
        self.assertIn("statusLine", json.loads(install.read_text(real)))

    def test_plugin_cli_keys_are_cleaned_up(self):
        """Simulate `claude plugin` adding enabledPlugins / extraKnownMarketplaces
        on install and leaving them empty on uninstall."""
        before = self.write(SETTINGS)
        calls = []

        def fake(cmd, env=None):
            calls.append(cmd)
            obj = json.loads(self.read())
            if cmd[:2] == ["plugin", "install"]:
                obj.setdefault("enabledPlugins", {})[install.PLUGIN_ID] = True
            elif cmd[:3] == ["plugin", "marketplace", "add"]:
                obj.setdefault("extraKnownMarketplaces", {})[install.MARKETPLACE] = {"source": {"source": "directory", "path": cmd[3]}}
            elif cmd[:2] == ["plugin", "uninstall"]:
                obj["enabledPlugins"].pop(install.PLUGIN_ID)
            elif cmd[:3] == ["plugin", "marketplace", "remove"]:
                obj["extraKnownMarketplaces"].pop(install.MARKETPLACE)
            with open(self.path, "w") as f:
                f.write(json.dumps(obj, indent=2))
            return 0, "ok", ""

        with mock.patch.object(install, "run_claude", fake), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"):
            self.run_cmd(install.install, args(self.path, apply=True, no_mod=False))
            self.assertIn("enabledPlugins", json.loads(self.read()))
            self.run_cmd(install.uninstall, args(self.path, apply=True, no_mod=False))
        self.assertEqual(self.read(), before)
        self.assertEqual([c[:3] for c in calls], [["plugin", "marketplace", "add"], ["plugin", "install", install.PLUGIN_ID],
                                                    ["plugin", "uninstall", install.PLUGIN_ID], ["plugin", "marketplace", "remove"]])

    def test_launcher(self):
        self.write(SETTINGS)
        bindir = os.path.join(self.dir, "bin")
        self.run_cmd(install.install, args(self.path, apply=True, bin_dir=bindir))
        lp = os.path.join(bindir, "ctempo")
        self.assertTrue(os.access(lp, os.X_OK))
        self.assertIn(install.LAUNCHER_MARK, install.read_text(lp))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertFalse(os.path.exists(lp))

    def test_probe_and_command_quoting(self):
        ok, ver, sq, ms, note = install.probe(sys.executable)
        self.assertTrue(ok, note)
        self.assertFalse(install.probe("python3")[0])  # not absolute
        cmd = install.our_command("/opt/my py/python3", "/x/bin/ctempo", "echo 'a b'")
        self.assertEqual(install.compose_of(cmd), "echo 'a b'")
        self.assertTrue(install.is_ours(cmd))


if __name__ == "__main__":
    unittest.main()
