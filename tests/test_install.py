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


# Hand-formatted: no serializer writes it back like this, so only a restore
# from the backup's bytes gives it back.
IRREGULAR = '{\n  "model": "opus",\n  "env": {"FOO": "b\u00e4r"},\n    "permissions": {"allow": ["Bash(git status)"], "deny": []}\n}\n\n'


def plugin_cli(user):
    """A fake `claude plugin` that edits the user settings file the way the real one does."""
    def run(cmd, env=None):
        obj = json.loads(install.read_text(user)) if os.path.exists(user) else {}
        if cmd[:2] == ["plugin", "install"]:
            obj.setdefault("enabledPlugins", {})[install.PLUGIN_ID] = True
        elif cmd[:3] == ["plugin", "marketplace", "add"]:
            obj.setdefault("extraKnownMarketplaces", {})[install.MARKETPLACE] = {"source": {}}
        elif cmd[:2] == ["plugin", "uninstall"]:
            obj.get("enabledPlugins", {}).pop(install.PLUGIN_ID, None)
        elif cmd[:3] == ["plugin", "marketplace", "remove"]:
            obj.get("extraKnownMarketplaces", {}).pop(install.MARKETPLACE, None)
        with open(user, "w") as f:
            f.write(json.dumps(obj, indent=2))
        return 0, "ok", ""
    return run


def claude_missing(cmd, env=None):
    return 127, "", "claude not found on PATH"


def args(settings, **kw):
    a = dict(apply=False, settings=settings, no_mod=True, python=sys.executable, replace=False, bin_dir=None,
             no_bootstrap=True)
    a.update(kw)
    return argparse.Namespace(**a)


class Install(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctempo-inst-")
        self.home = os.path.join(self.dir, "home")
        # Hermetic: no code path (old or new) can reach the real ~/.claude.
        self.env = mock.patch.dict(os.environ, {"CTEMPO_HOME": self.home, "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "claude"),
                                                "HOME": os.path.join(self.dir, "userhome")})
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

    # -- second review ---------------------------------------------------------------
    def test_v1_record_with_relative_path_is_recovered(self):
        s = dict(SETTINGS, statusLine={"type": "command", "command": "echo mine"})
        before = self.write(s)
        backup = os.path.realpath(self.path) + ".ctempo-backup-20261014-090000"
        with open(backup, "w") as f:
            f.write(before)
        obj = dict(SETTINGS, statusLine={"type": "command", "command": install.our_command(sys.executable, install.entry_path()),
                                         "refreshInterval": 60})
        with open(self.path, "w") as f:
            f.write(json.dumps(obj, indent=2))
        os.makedirs(self.home)
        with open(install.state_path(), "w") as f:  # what version 1 wrote for `--settings settings.json`
            json.dump({"installed": True, "settings_path": "settings.json", "had_file": True, "backup_path": backup,
                       "original_sha256": install.sha(before), "had_statusLine": True,
                       "original_statusLine": s["statusLine"], "had_keys": {k: False for k in install.PLUGIN_KEYS}}, f)
        rc, out = self.run_cmd(install.uninstall, args(None, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), before)

    def test_failed_plugin_uninstall_keeps_the_mod_for_a_retry(self):
        before = self.write(SETTINGS)
        self.env2 = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir})
        self.env2.start()
        try:
            def ok(cmd, env=None):
                obj = json.loads(self.read())
                if cmd[:2] == ["plugin", "install"]:
                    obj.setdefault("enabledPlugins", {})[install.PLUGIN_ID] = True
                elif cmd[:3] == ["plugin", "marketplace", "add"]:
                    obj.setdefault("extraKnownMarketplaces", {})[install.MARKETPLACE] = {"source": {}}
                elif cmd[:2] == ["plugin", "uninstall"]:
                    obj["enabledPlugins"].pop(install.PLUGIN_ID, None)
                elif cmd[:3] == ["plugin", "marketplace", "remove"]:
                    obj["extraKnownMarketplaces"].pop(install.MARKETPLACE, None)
                with open(self.path, "w") as f:
                    f.write(json.dumps(obj, indent=2))
                return 0, "ok", ""

            with mock.patch.object(install, "run_claude", ok), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"):
                self.run_cmd(install.install, args(self.path, apply=True, no_mod=False))
            with mock.patch.object(install, "run_claude", lambda c, env=None: (127, "", "claude not found on PATH")):
                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
            self.assertEqual(rc, 1)
            self.assertIn("still installed", out)
            self.assertTrue(install.load_state().get("mod"))
            self.assertTrue(os.path.exists(os.path.join(self.home, "runtime.json")))
            with mock.patch.object(install, "run_claude", ok):
                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
            self.assertEqual(rc, 0, out)
            self.assertEqual(self.read(), before)
        finally:
            self.env2.stop()

    def test_plugin_keys_are_tidied_in_the_user_file_when_it_went_first(self):
        user = self.path  # CLAUDE_CONFIG_DIR/settings.json: the file the plugin CLI edits
        proj = os.path.join(self.dir, "proj-settings.json")
        before_user = self.write(SETTINGS)
        with open(proj, "w") as f:
            f.write(json.dumps({"model": "sonnet"}, indent=2))

        def fake(cmd, env=None):
            obj = json.loads(install.read_text(user))
            if cmd[:2] == ["plugin", "install"]:
                obj.setdefault("enabledPlugins", {})[install.PLUGIN_ID] = True
            elif cmd[:3] == ["plugin", "marketplace", "add"]:
                obj.setdefault("extraKnownMarketplaces", {})[install.MARKETPLACE] = {"source": {}}
            elif cmd[:2] == ["plugin", "uninstall"]:
                obj["enabledPlugins"].pop(install.PLUGIN_ID, None)
            elif cmd[:3] == ["plugin", "marketplace", "remove"]:
                obj["extraKnownMarketplaces"].pop(install.MARKETPLACE, None)
            with open(user, "w") as f:
                f.write(json.dumps(obj, indent=2))
            return 0, "ok", ""

        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir}), \
                mock.patch.object(install, "run_claude", fake), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"):
            self.run_cmd(install.install, args(user, apply=True, no_mod=False))
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
            self.run_cmd(install.uninstall, args(user, apply=True, no_mod=False))
            self.run_cmd(install.uninstall, args(proj, apply=True, no_mod=False))
        self.assertEqual(json.loads(install.read_text(user)), SETTINGS)
        self.assertEqual(install.read_text(user), before_user)
        self.assertEqual(json.loads(install.read_text(proj)), {"model": "sonnet"})

    def _user_file(self, text):
        if text is None:  # no user file
            if os.path.exists(self.path):
                os.remove(self.path)
        else:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
        return mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir})  # self.path is the user file

    def test_retry_after_failed_plugin_uninstall_is_byte_identical(self):
        # Review #4: the partial run must not drop the record the retry needs.
        with self._user_file(IRREGULAR), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"):
            with mock.patch.object(install, "run_claude", plugin_cli(self.path)):
                self.run_cmd(install.install, args(self.path, apply=True, no_mod=False))
            with mock.patch.object(install, "run_claude", lambda c, env=None: (127, "", "claude not found")):
                rc, _ = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
            self.assertEqual(rc, 1)
            with mock.patch.object(install, "run_claude", plugin_cli(self.path)):
                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), IRREGULAR)
        self.assertEqual(install.load_state().get("records"), {})

    def test_user_file_uninstalled_first_comes_back_byte_identical(self):
        # Review #5: the user file goes first, the mod stays for a project file.
        proj = os.path.join(self.dir, "proj-settings.json")
        with open(proj, "w") as f:
            f.write('{"model": "sonnet"}')
        with self._user_file(IRREGULAR), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                mock.patch.object(install, "run_claude", plugin_cli(self.path)):
            self.run_cmd(install.install, args(self.path, apply=True, no_mod=False))
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
            self.run_cmd(install.uninstall, args(self.path, apply=True, no_mod=False))
            self.assertIn("enabledPlugins", json.loads(self.read()))  # the mod is still in use
            rc, out = self.run_cmd(install.uninstall, args(proj, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), IRREGULAR)
        self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
        self.assertEqual(install.load_state().get("records"), {})

    def test_user_file_touched_only_by_the_plugin_cli_comes_back_byte_identical(self):
        # Review #5: no statusLine there, so no record; the baseline is the
        # snapshot taken before the plugin CLI first wrote to it.
        proj = os.path.join(self.dir, "proj-settings.json")
        with open(proj, "w") as f:
            f.write('{"model": "sonnet"}')
        with self._user_file(IRREGULAR), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                mock.patch.object(install, "run_claude", plugin_cli(self.path)):
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
            self.assertIn("enabledPlugins", json.loads(self.read()))
            rc, out = self.run_cmd(install.uninstall, args(proj, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), IRREGULAR)

    def test_two_records_of_one_file_both_go(self):
        # Review #6: a version-1 record and a later one under another name.
        real = os.path.join(self.dir, "dot-settings.json")
        with open(real, "w") as f:
            f.write(IRREGULAR)
        os.symlink(real, self.path)
        self.run_cmd(install.install, args(real, apply=True))
        st = install.load_state()
        (k, rec), = st["records"].items()
        st["records"][self.path] = dict(rec, settings_path=self.path)
        install.save_state(st)
        rc, out = self.run_cmd(install.uninstall, args(None, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(install.load_state().get("records"), {})
        self.assertEqual(install.read_text(real), IRREGULAR)

    def test_symlink_alias_is_one_record(self):
        real = os.path.join(self.dir, "dot-settings.json")
        with open(real, "w") as f:
            f.write(json.dumps(dict(SETTINGS, statusLine={"type": "command", "command": "echo X"}), indent=4))
        with open(real) as f:
            before = f.read()
        os.symlink(real, self.path)
        self.run_cmd(install.install, args(self.path, apply=True))
        self.run_cmd(install.install, args(real, apply=True))
        self.assertEqual(len(install.installed_records(install.load_state())), 1)
        rc, out = self.run_cmd(install.uninstall, args(None, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(install.read_text(real), before)

    def test_reinstall_after_file_deleted_leaves_no_file(self):
        self.write({"model": "opus"})
        self.run_cmd(install.install, args(self.path, apply=True))
        os.remove(self.path)
        self.run_cmd(install.install, args(self.path, apply=True))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertFalse(os.path.exists(self.path))

    # -- fourth review ---------------------------------------------------------------
    def _reset(self):
        """A clean slate between subtests: no install.json, no backups."""
        shutil.rmtree(self.home, ignore_errors=True)
        for n in os.listdir(self.dir):
            if ".ctempo-backup-" in n:
                os.remove(os.path.join(self.dir, n))

    def _proj(self):
        proj = os.path.join(self.dir, "proj-settings.json")
        with open(proj, "w") as f:
            f.write('{"model": "sonnet"}')
        return proj

    def _user_text(self):
        return install.read_text(self.path) if os.path.exists(self.path) else None

    def _steps(self, proj, *steps):
        """Run i(nstall) / u(ninstall) with the mod on the U(ser) file, the P(roject)
        file or * (no --settings); "-nomod" adds --no-mod, "-fail" makes claude exit 127."""
        rcs = []
        for s in steps:
            fn = install.install if s[0] == "i" else install.uninstall
            target = {"U": self.path, "P": proj, "*": None}[s[1]]
            cli = claude_missing if s.endswith("-fail") else plugin_cli(self.path)
            with mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), mock.patch.object(install, "run_claude", cli):
                rc, out = self.run_cmd(fn, args(target, apply=True, no_mod=s.endswith("-nomod")))
            rcs.append(rc)
        return rcs

    def test_user_file_without_a_record_of_its_own_comes_back_from_the_mod_snapshot(self):
        # Review 4 (install#0): a retry without --settings after a failed project
        # uninstall, a full uninstall after `--no-mod`, and a stray uninstall of the
        # user file all reach the user file with no record of its own. Its baseline
        # is the snapshot taken before the plugin CLI first wrote there.
        proj = self._proj()
        for steps in (("iP", "uP-fail", "u*"), ("iP", "uP-nomod", "u*"), ("iP", "uU", "u*")):
            for before in (IRREGULAR, None):
                with self.subTest(steps=steps, before=before):
                    self._reset()
                    with self._user_file(before):
                        rcs = self._steps(proj, *steps)
                    self.assertEqual(rcs[-1], 0)
                    self.assertEqual(self._user_text(), before)  # None: the file the plugin CLI created is gone
                    self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
                    self.assertEqual(install.load_state().get("records"), {})

    def test_installing_into_the_user_file_after_the_mod_keeps_the_pristine_baseline(self):
        # Review 4 (install#1): the user file already holds the plugin CLI's keys, so
        # its baseline is its kept record, else the mod's snapshot, not the file as it is.
        proj = self._proj()
        for steps in (("iP", "iU", "uU", "uP"), ("iP", "iU", "uP", "uU"),
                      ("iU", "uU-nomod", "iU", "uU"), ("iU", "uU-fail", "iU", "uU")):
            for before in (IRREGULAR, None):
                with self.subTest(steps=steps, before=before):
                    self._reset()
                    with self._user_file(before):
                        rcs = self._steps(proj, *steps)
                    self.assertEqual(rcs[-1], 0)
                    self.assertEqual(self._user_text(), before)
                    self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
                    self.assertEqual(install.load_state().get("records"), {})

    def test_command_edited_inside_compose_is_the_one_uninstall_restores(self):
        # Review 4 (install#2): settings.json shows what runs (D-17), so a command the
        # user put in --compose is the original now, also after a reinstall or --replace.
        s = dict(SETTINGS, statusLine={"type": "command", "command": "~/.claude/statusline-old.sh", "padding": 0})
        newer = "npx -y ccstatusline@latest"
        for then in (None, {}, {"replace": True}):
            with self.subTest(then=then):
                self._reset()
                self.write(s)
                self.run_cmd(install.install, args(self.path, apply=True))
                obj = json.loads(self.read())
                obj["statusLine"]["command"] = install.our_command(sys.executable, install.entry_path(), newer)
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(obj, indent=2, ensure_ascii=False))
                if then is not None:
                    rc, out = self.run_cmd(install.install, args(self.path, apply=True, **then))
                    self.assertIn("REPLACE" if then else "already runs ctempo", out)
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                after = json.loads(self.read())
                self.assertEqual(after.pop("statusLine"), {"type": "command", "command": newer, "padding": 0})
                self.assertEqual(after, SETTINGS)

    def test_plugin_commands_run_by_hand_are_undone_by_uninstall(self):
        # Review 4 (install#3): claude was not on PATH at install, so the user ran the
        # plugin commands. Uninstall undoes them, and keeps the mod record and
        # runtime.json (exit 1) while claude is still missing.
        cli = plugin_cli(self.path)
        calls = []

        def recording(cmd, env=None):
            calls.append(cmd)
            return cli(cmd)

        runtime = os.path.join(self.home, "runtime.json")
        with self._user_file(IRREGULAR):
            with mock.patch.object(install, "claude_bin", lambda: None), mock.patch.object(install, "run_claude", claude_missing):
                rc, out = self.run_cmd(install.install, args(self.path, apply=True, no_mod=False))
                self.assertIn("run these yourself", out)
                for c in install.mod_commands(install.repo_root()):
                    cli(c)  # the user, by hand
                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                self.assertEqual(rc, 1, out)
                self.assertIn("still installed", out)
                self.assertTrue(os.path.exists(runtime))
            with mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), mock.patch.object(install, "run_claude", recording):
                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls, install.unmod_commands())
        self.assertEqual(self.read(), IRREGULAR)
        self.assertFalse(os.path.exists(runtime))
        self.assertNotIn("mod", install.load_state())

    def test_plugin_commands_never_run_by_hand_do_not_block_uninstall(self):
        # Review 4 (install#3): claude still missing and no trace of the mod in the
        # user file: the printed commands never ran, so there is nothing to undo.
        proj = self._proj()
        for target in (self.path, proj):
            with self.subTest(target=target):
                self._reset()
                with self._user_file(IRREGULAR), mock.patch.object(install, "claude_bin", lambda: None), \
                        mock.patch.object(install, "run_claude", claude_missing):
                    self.run_cmd(install.install, args(target, no_mod=False))
                    self.assertFalse(os.path.exists(self.home))  # the dry run writes nothing
                    self.run_cmd(install.install, args(target, apply=True, no_mod=False))
                    rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                self.assertEqual(rc, 0, out)
                self.assertIn("Uninstalled.", out)
                self.assertEqual(self.read(), IRREGULAR)
                self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
                state = install.load_state()
                self.assertEqual(state.get("records"), {})
                self.assertNotIn("mod", state)
                self.assertFalse(os.path.exists(os.path.join(self.home, "runtime.json")))

    def test_blank_settings_file_comes_back_byte_identical(self):
        # Review 4 (install#4): install reads a blank file as {}; restore must read
        # its backup the same way, or the file comes back as "{}".
        for text in ("", "\n", "  \n", "\r\n"):
            for mod in (False, True):
                with self.subTest(text=text, mod=mod):
                    self._reset()
                    with self._user_file(text):
                        rcs = self._steps(None, "iU" + ("" if mod else "-nomod"), "u*" + ("" if mod else "-nomod"))
                    self.assertEqual(rcs, [0, 0])
                    with open(self.path, "rb") as f:
                        self.assertEqual(f.read(), text.encode("utf-8"))

    def test_version_1_uninstalled_state_still_gets_a_user_file_snapshot(self):
        # Review 4 (install#5): version 1's uninstall left installed false and a "mod"
        # entry with no user-file snapshot. The spent record must not be the user
        # file's baseline, and the entry must not stop install from taking the snapshot.
        proj = self._proj()
        v1_text = '{"model": "opus"}\n'  # the user file when version 1 installed; edited since
        backup = os.path.realpath(self.path) + ".ctempo-backup-20261001-090000"
        with open(backup, "w") as f:
            f.write(v1_text)
        os.makedirs(self.home)
        with open(install.state_path(), "w") as f:  # what c016f98's uninstall left
            json.dump({"installed": False, "settings_path": self.path, "had_file": True, "backup_path": backup,
                       "original_sha256": install.sha(v1_text), "had_statusLine": False, "original_statusLine": None,
                       "had_keys": {k: False for k in install.PLUGIN_KEYS}, "python": sys.executable,
                       "entry": install.entry_path(), "written_statusLine": None,
                       "mod": {"marketplace": install.MARKETPLACE, "plugin": install.PLUGIN_ID, "root": install.repo_root()}}, f)
        with self._user_file(IRREGULAR):
            rcs = self._steps(proj, "iP", "uP")
        self.assertEqual(rcs, [0, 0])
        self.assertEqual(self.read(), IRREGULAR)
        self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
        state = install.load_state()
        self.assertEqual(state.get("records"), {})
        self.assertNotIn("mod", state)

    def test_probe_and_command_quoting(self):
        ok, ver, sq, ms, note = install.probe(sys.executable)
        self.assertTrue(ok, note)
        self.assertFalse(install.probe("python3")[0])  # not absolute
        cmd = install.our_command("/opt/my py/python3", "/x/bin/ctempo", "echo 'a b'")
        self.assertEqual(install.compose_of(cmd), "echo 'a b'")
        self.assertTrue(install.is_ours(cmd))


if __name__ == "__main__":
    unittest.main()
