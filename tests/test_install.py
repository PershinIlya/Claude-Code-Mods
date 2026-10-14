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
    a = dict(apply=False, settings=settings, no_mod=True, python=sys.executable, replace=False, bin_dir=None,
             no_bootstrap=True)
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

    def alt_python(self):
        """Another absolute path to the same interpreter: forces a repin."""
        alt = os.path.join(self.dir, "alt-python3")
        if not os.path.exists(alt):
            os.symlink(sys.executable, alt)
        return alt

    # -- review findings ---------------------------------------------------------------
    def test_reinstall_after_user_changed_statusline_restores_the_new_one(self):
        before = self.write(SETTINGS)
        self.run_cmd(install.install, args(self.path, apply=True))
        obj = json.loads(self.read())
        obj["statusLine"] = {"type": "command", "command": "~/.claude/statusline-command.sh"}
        changed = json.dumps(obj, indent=2)
        with open(self.path, "w") as f:
            f.write(changed)
        self.run_cmd(install.install, args(self.path, apply=True))
        self.assertEqual(install.compose_of(json.loads(self.read())["statusLine"]["command"]), "~/.claude/statusline-command.sh")
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(self.read(), changed)
        self.assertNotEqual(self.read(), before)

    def test_lost_install_record_never_restores_ctempo_itself(self):
        s = dict(SETTINGS, statusLine={"type": "command", "command": "~/bin/theirs.sh"})
        before = self.write(s)
        self.run_cmd(install.install, args(self.path, apply=True))
        shutil.rmtree(self.home)  # install.json gone
        self.run_cmd(install.install, args(self.path, apply=True, python=self.alt_python()))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        sl = json.loads(self.read())["statusLine"]
        self.assertFalse(install.is_ours(sl["command"]))
        self.assertEqual(sl["command"], "~/bin/theirs.sh")
        self.assertEqual(self.read(), before)

    def test_records_are_per_settings_file(self):
        a = self.path
        b = os.path.join(self.dir, "project-settings.json")
        sa = dict(SETTINGS, statusLine={"type": "command", "command": "~/.claude/my-line.sh", "padding": 0})
        before_a = self.write(sa)
        with open(b, "w") as f:
            f.write(json.dumps({"model": "sonnet"}, indent=2))
        self.run_cmd(install.install, args(a, apply=True))
        self.run_cmd(install.install, args(b, apply=True))
        rc, out = self.run_cmd(install.uninstall, args(None, apply=True))
        self.assertEqual(rc, 1)
        self.assertIn("several settings files", out)
        self.run_cmd(install.uninstall, args(b, apply=True))
        self.assertEqual(json.loads(install.read_text(b)), {"model": "sonnet"})
        self.run_cmd(install.uninstall, args(a, apply=True))
        self.assertEqual(self.read(), before_a)

    def test_crash_after_write_leaves_a_usable_record(self):
        s = dict(SETTINGS, statusLine={"type": "command", "command": "~/bin/theirs.sh", "padding": 2})
        before = self.write(s)

        def boom(cmd, env=None):
            raise RuntimeError("claude died")

        with mock.patch.object(install, "run_claude", boom), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"):
            with self.assertRaises(RuntimeError):
                self.run_cmd(install.install, args(self.path, apply=True, replace=True, no_mod=False))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(self.read(), before)

    def test_run_claude_reports_instead_of_raising(self):
        with mock.patch.object(install, "claude_bin", lambda: "/nonexistent/claude"):
            code, so, se = install.run_claude(["plugin", "list"])
        self.assertEqual(code, 127)

    def test_two_installs_in_one_second_keep_the_pristine_backup(self):
        before = self.write({"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}, indent=None)
        self.run_cmd(install.install, args(self.path, apply=True))
        self.run_cmd(install.install, args(self.path, apply=True, python=self.alt_python()))
        backups = sorted(f for f in os.listdir(self.dir) if ".ctempo-backup-" in f)
        self.assertEqual(len(backups), 2)
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(self.read(), before)

    def test_relative_settings_path_and_missing_file(self):
        self.write(SETTINGS)
        cwd = os.getcwd()
        try:
            os.chdir(self.dir)
            self.run_cmd(install.install, args("settings.json", apply=True))
            other = os.path.join(self.dir, "other")
            os.makedirs(other)
            os.chdir(other)
            self.run_cmd(install.uninstall, args(None, apply=True))
            self.assertFalse(os.path.exists(os.path.join(other, "settings.json")))
            self.assertEqual(json.loads(self.read()), SETTINGS)
            rc, out = self.run_cmd(install.uninstall, args(os.path.join(other, "nope.json"), apply=True))
            self.assertFalse(os.path.exists(os.path.join(other, "nope.json")))
        finally:
            os.chdir(cwd)

    def test_replace_after_compose(self):
        s = dict(SETTINGS, statusLine={"type": "command", "command": "sleep 5; echo slow"})
        before = self.write(s)
        self.run_cmd(install.install, args(self.path, apply=True))
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, replace=True))
        self.assertIn("REPLACE", out)
        self.assertIsNone(install.compose_of(json.loads(self.read())["statusLine"]["command"]))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(self.read(), before)

    def test_crlf_and_mode_survive(self):
        raw = b'{\r\n  "model": "opus"\r\n}\r\n'
        with open(self.path, "wb") as f:
            f.write(raw)
        os.chmod(self.path, 0o600)
        self.run_cmd(install.install, args(self.path, apply=True))
        with open(self.path, "rb") as f:
            mid = f.read()
        self.assertNotIn(b"\n", mid.replace(b"\r\n", b""))  # every newline still CRLF
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), raw)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_probe_and_command_quoting(self):
        ok, ver, sq, ms, note = install.probe(sys.executable)
        self.assertTrue(ok, note)
        self.assertFalse(install.probe("python3")[0])  # not absolute
        cmd = install.our_command("/opt/my py/python3", "/x/bin/ctempo", "echo 'a b'")
        self.assertEqual(install.compose_of(cmd), "echo 'a b'")
        self.assertTrue(install.is_ours(cmd))


if __name__ == "__main__":
    unittest.main()
