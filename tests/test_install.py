"""install / uninstall on throwaway settings files."""

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
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
# A user file that lists another plugin: both plugin keys exist before ctempo.
WITH_PLUGINS = ('{\n  "model": "opus",\n  "enabledPlugins": {"other@mkt": true},\n'
                '  "extraKnownMarketplaces": {"mkt": {"source": {"source": "github", "repo": "a/b"}}}\n}\n')


def plugin_cli(user):
    """A fake `claude plugin` that edits the user settings file the way the real one does."""
    def run(cmd, env=None):
        text = install.read_text(user) if os.path.exists(user) else ""
        obj = json.loads(text) if text.strip() else {}  # a blank file is {}
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


def claude_hangs(cmd, env=None):
    return 124, "", "claude %s timed out after 120 s" % " ".join(cmd[:3])


def nothing_installed(cmd, env=None):
    """The plugin CLI with nothing to remove: it says so and writes nothing."""
    if cmd[:2] == ["plugin", "uninstall"]:
        return 1, "", 'Plugin "%s" not found in installed plugins' % install.PLUGIN_ID
    return 1, "", "Marketplace '%s' not found" % install.MARKETPLACE


def args(settings, **kw):
    a = dict(apply=False, settings=settings, no_mod=True, python=sys.executable, replace=False, bin_dir=None,
             no_bootstrap=True)
    a.update(kw)
    return argparse.Namespace(**a)


@contextlib.contextmanager
def time_zone(tz):
    """Local time in another time zone (a POSIX TZ value): the wall clock moves, time does not."""
    try:
        with mock.patch.dict(os.environ, {"TZ": tz}):
            time.tzset()
            yield
    finally:
        time.tzset()


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
            self.assertIn("may still be installed", out)
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
        file or * (no --settings); "-nomod" adds --no-mod, "-fail" makes claude exit 127,
        "-hang" makes it time out (124), "-none" makes it find nothing to remove, "-nobin"
        takes claude off PATH. "mod-by-hand" / "unmod-by-hand": the user runs the plugin
        commands (no exit code is recorded for those)."""
        rcs = []
        for s in steps:
            if s.endswith("-by-hand"):
                cli = plugin_cli(self.path)
                for c in install.mod_commands(install.repo_root()) if s == "mod-by-hand" else install.unmod_commands():
                    cli(c)
                continue
            fn = install.install if s[0] == "i" else install.uninstall
            target = {"U": self.path, "P": proj, "*": None}[s[1]]
            cli = {"-fail": claude_missing, "-nobin": claude_missing, "-hang": claude_hangs,
                   "-none": nothing_installed}.get(s[2:]) or plugin_cli(self.path)
            exe = None if s.endswith("-nobin") else "/bin/claude"
            with mock.patch.object(install, "claude_bin", lambda: exe), mock.patch.object(install, "run_claude", cli):
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
                self.assertIn("may still be installed", out)
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

    # -- fifth review ----------------------------------------------------------------
    def test_command_put_in_compose_with_no_statusline_before_is_restored(self):
        # Review 5 (D-install#0): with no statusLine (or no file) before ctempo, a command
        # the user put in --compose is the original too, also after a repin or --replace
        # (which must record it first). The refreshInterval 60 ctempo added goes with it.
        newer = "npx -y ccstatusline@latest"
        for before in (SETTINGS, None):
            for then in (None, {}, {"replace": True}):
                with self.subTest(before=before is not None, then=then):
                    self._reset()
                    if before is None:
                        if os.path.exists(self.path):
                            os.remove(self.path)
                    else:
                        self.write(before)
                    self.run_cmd(install.install, args(self.path, apply=True))
                    obj = json.loads(self.read())
                    obj["statusLine"]["command"] = install.our_command(sys.executable, install.entry_path(), newer)
                    obj["statusLine"]["padding"] = 0
                    with open(self.path, "w", encoding="utf-8") as f:
                        f.write(json.dumps(obj, indent=2, ensure_ascii=False))
                    if then is not None:
                        rc, out = self.run_cmd(install.install, args(self.path, apply=True, **then))
                        self.assertIn("REPLACE" if then else "already runs ctempo", out)
                    if then:  # "kept in the backup and in install.json"
                        rec = install.load_state()["records"][self.path]
                        self.assertEqual(rec["original_statusLine"], {"type": "command", "command": newer, "padding": 0})
                    rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                    self.assertEqual(rc, 0, out)
                    after = json.loads(self.read())
                    self.assertEqual(after.pop("statusLine"), {"type": "command", "command": newer, "padding": 0})
                    self.assertEqual(after, before or {})

    def test_uninstall_completes_with_claude_missing_once_the_user_file_lists_no_mod(self):
        # Review 5 (D-install#1): claude is missing at uninstall. Once the user file lists
        # neither the plugin nor its marketplace (the user ran the reverse commands by hand
        # as the warning says, or the install commands timed out), there is nothing to
        # undo whatever the mod record says, and uninstall completes byte for byte. The
        # warning never claims the mod is installed when nothing shows it is.
        proj = self._proj()
        runtime = os.path.join(self.home, "runtime.json")
        cli = plugin_cli(self.path)

        def timed_out(cmd, env=None):
            return 124, "", "claude %s timed out after 120 s" % " ".join(cmd[:3])

        for target in (self.path, proj):
            for how in ("claude", "by hand", "timed out"):
                with self.subTest(target=os.path.basename(target), install=how):
                    self._reset()
                    with self._user_file(IRREGULAR):
                        with mock.patch.object(install, "claude_bin", lambda: None if how == "by hand" else "/bin/claude"), \
                                mock.patch.object(install, "run_claude", timed_out if how == "timed out" else cli):
                            self.run_cmd(install.install, args(target, apply=True, no_mod=False))
                            if how == "by hand":
                                for c in install.mod_commands(install.repo_root()):
                                    cli(c)  # the user, by hand
                            if how == "timed out":  # claude still there, still hanging: retry later
                                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                                self.assertEqual(rc, 1, out)
                                self.assertNotIn("mod is still installed", out)
                                self.assertIn("may still be installed", out)
                        with mock.patch.object(install, "claude_bin", lambda: None), \
                                mock.patch.object(install, "run_claude", claude_missing):
                            if how != "timed out":
                                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                                self.assertEqual(rc, 1, out)  # the user file still lists the mod
                                self.assertIn("may still be installed", out)
                                for c in install.unmod_commands():
                                    cli(c)  # the user, by hand
                            rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                    self.assertEqual(rc, 0, out)
                    self.assertIn("Uninstalled.", out)
                    self.assertEqual(self.read(), IRREGULAR)
                    self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
                    state = install.load_state()
                    self.assertEqual(state.get("records"), {})
                    self.assertNotIn("mod", state)
                    self.assertFalse(os.path.exists(runtime))

    def test_user_file_edited_before_installing_into_it_comes_back_as_edited(self):
        # Review 5 (D-install#2): the mod went in first (for the project file, or for the
        # user file with --no-mod uninstalled since), then the user edited the user file,
        # then ctempo was installed into it. The file right before that install is its
        # baseline (the one before the mod only a fallback), so it comes back byte for byte.
        proj = self._proj()
        for first, last in ((("iP",), ("iU", "uU")), (("iP",), ("iU-nomod", "uU-nomod")),
                            (("iU", "uU-nomod"), ("iU", "uU-nomod")),
                            (("iP-nobin",), ("iU-nobin", "uU-nobin", "uP-nobin"))):
            with self.subTest(steps=first + ("edit",) + last):
                self._reset()
                with self._user_file(IRREGULAR):
                    self._steps(proj, *first)
                    edited = self._user_text().replace("{\n", '{\n  "sandbox": {"enabled": true},\n', 1)
                    with open(self.path, "w", encoding="utf-8") as f:
                        f.write(edited)
                    rcs = self._steps(proj, *last)
                self.assertEqual(rcs, [0] * len(last))
                self.assertEqual(self._user_text(), edited)

    def test_uninstall_leaves_a_user_file_the_mod_never_touched_alone(self):
        # Review 5 (D-install#3): claude was not on PATH at install and the printed commands
        # never ran, so the plugin CLI never wrote to the user file. Uninstall must not
        # rewrite it (here reformatted, or created since), with claude still missing or
        # back on PATH and reporting nothing to remove.
        proj = self._proj()

        def not_installed(cmd, env=None):  # the plugin CLI with nothing to remove
            if cmd[:2] == ["plugin", "uninstall"]:
                return 1, "", 'Plugin "%s" not found in installed plugins' % install.PLUGIN_ID
            return 1, "", "Marketplace '%s' not found" % install.MARKETPLACE

        reformatted = json.dumps(json.loads(IRREGULAR), indent=4, ensure_ascii=False)  # same JSON
        for before, now in ((IRREGULAR, reformatted), (None, "{}")):
            for exe, cli in ((None, claude_missing), ("/bin/claude", not_installed)):
                with self.subTest(before=before is not None, claude=exe):
                    self._reset()
                    with self._user_file(before):
                        with mock.patch.object(install, "claude_bin", lambda: None), \
                                mock.patch.object(install, "run_claude", claude_missing):
                            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
                        with open(self.path, "w", encoding="utf-8") as f:
                            f.write(now)
                        with mock.patch.object(install, "claude_bin", lambda: exe), mock.patch.object(install, "run_claude", cli):
                            rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                    self.assertEqual(rc, 0, out)
                    self.assertEqual(self._user_text(), now)
                    self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
                    state = install.load_state()
                    self.assertEqual(state.get("records"), {})
                    self.assertNotIn("mod", state)

    def test_plugin_commands_first_run_at_a_later_install_take_a_fresh_snapshot(self):
        # Review 5 (D-install#3): a manual install whose commands never ran, a hand edit of
        # the user file, then an install with claude on PATH that runs them for the first
        # time. The user file comes back as it was right before they ran.
        proj = self._proj()
        with self._user_file(IRREGULAR):
            self._steps(proj, "iP-nobin")
            edited = IRREGULAR.replace("{\n", '{\n  "sandbox": {"enabled": true},\n', 1)
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(edited)
            rcs = self._steps(proj, "iP", "uP")
        self.assertEqual(rcs, [0, 0])
        self.assertEqual(self.read(), edited)
        self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
        state = install.load_state()
        self.assertEqual(state.get("records"), {})
        self.assertNotIn("mod", state)

    def test_blank_backup_means_no_statusline_when_the_record_is_lost(self):
        # Review 5 (D-install#4): with install.json gone, the newest backup is the blank file
        # from before the last install, so there was no statusLine then. An older backup's
        # statusLine must not come back, at uninstall or through a reinstall's record.
        for reinstall in (False, True):
            with self.subTest(reinstall=reinstall):
                self._reset()
                self.write(dict(SETTINGS, statusLine={"type": "command", "command": "~/old-line.sh"}))
                self.run_cmd(install.install, args(self.path, apply=True))
                self.run_cmd(install.uninstall, args(self.path, apply=True))
                with open(self.path, "w") as f:
                    f.write("")
                self.run_cmd(install.install, args(self.path, apply=True))
                self.assertEqual(len([n for n in os.listdir(self.dir) if ".ctempo-backup-" in n]), 2)
                shutil.rmtree(self.home)  # install.json gone
                if reinstall:
                    self.run_cmd(install.install, args(self.path, apply=True))
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                self.assertEqual(json.loads(self.read()), {})

    # -- sixth review ----------------------------------------------------------------
    def _assert_clean(self, proj):
        self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
        state = install.load_state()
        self.assertEqual(state.get("records"), {})
        self.assertNotIn("mod", state)

    def test_user_file_the_plugin_cli_rewrote_comes_back_although_its_plugin_keys_existed(self):
        # Review 6 (F-install#0): the user file lists another plugin, so both plugin keys were
        # there before ctempo. Once ctempo has seen the mod listed there (a failed uninstall
        # after the user ran the printed commands, or a repin without claude after the plugin
        # CLI installed it), the plugin CLI has written there, whatever comes next: after the
        # user removes the plugin by hand, uninstall still gives the file back byte for byte.
        proj = self._proj()
        for steps in (("iP-nobin", "mod-by-hand", "uP-nobin", "unmod-by-hand", "uP-nobin"),
                      ("iP-nobin", "mod-by-hand", "uP-nobin", "unmod-by-hand", "uP"),
                      ("iP", "iP-nobin", "unmod-by-hand", "uP-nobin"),
                      ("iP", "iP-nobin", "unmod-by-hand", "uP"),
                      ("iP", "iP-nobin", "unmod-by-hand", "iP", "uP")):
            with self.subTest(steps=steps):
                self._reset()
                with self._user_file(WITH_PLUGINS):
                    rcs = self._steps(proj, *steps)
                self.assertEqual(rcs[-1], 0)
                self.assertEqual(self._user_text(), WITH_PLUGINS)
                self._assert_clean(proj)

    def test_user_file_the_plugin_cli_never_wrote_to_is_never_rewritten(self):
        # Review 6 (F-install#1): the plugin CLI never wrote to the user file (claude was off
        # PATH, or the install commands timed out), and the user reformatted it since (or
        # created it). No uninstall rewrites it: not one that reaches it as its own target
        # (after a --no-mod uninstall, or a rerun after claude hung), not one that finds
        # claude back with nothing to remove, not one after install commands that timed out.
        proj = self._proj()
        reformatted = json.dumps(json.loads(IRREGULAR), indent=4, ensure_ascii=False)  # same JSON
        for before, now, first, last in ((IRREGULAR, reformatted, ("iP-nobin", "uP-nomod"), ("u*-nobin",)),
                                         (IRREGULAR, reformatted, ("iP-nobin", "uP-nomod"), ("u*-none",)),
                                         (IRREGULAR, reformatted, ("iP-nobin",), ("uP-hang", "u*-nobin")),
                                         (IRREGULAR, reformatted, ("iP-nobin",), ("uP-hang", "u*-none")),
                                         (IRREGULAR, reformatted, ("iP-hang",), ("uP-nobin",)),
                                         (IRREGULAR, reformatted, ("iP-hang",), ("uP-none",)),
                                         (None, "{}", ("iP-nobin", "uP-nomod"), ("u*-nobin",))):
            with self.subTest(before=before is not None, steps=first + ("edit",) + last):
                self._reset()
                with self._user_file(before):
                    self._steps(proj, *first)
                    with open(self.path, "w", encoding="utf-8") as f:
                        f.write(now)
                    rcs = self._steps(proj, *last)
                self.assertEqual(rcs[-1], 0)
                self.assertEqual(self._user_text(), now)
                self._assert_clean(proj)

    def test_full_uninstall_without_a_user_file_leaves_no_record_of_it(self):
        # Review 6 (F-install#2): no user file, ctempo installed into it and into a project
        # file, then everything uninstalled. The user file's kept record must go with the mod,
        # or a later install cycle takes it as that file's baseline from before the mod and
        # loses the byte-for-byte restore. Install also drops such a record left by older code.
        proj = self._proj()
        for first in (("iP-nobin", "iU-nobin", "uU-nobin", "uP-nobin"), ("iP-hang", "uU-nobin", "uP-nobin"), None):
            with self.subTest(first=first):
                self._reset()
                with self._user_file(None):
                    if first is None:  # what the code before this fix left behind
                        install.save_state({"records": {self.path: {
                            "settings_path": self.path, "had_file": False, "installed": False,
                            "had_keys": {k: False for k in install.PLUGIN_KEYS}}}})
                    else:
                        rcs = self._steps(proj, *first)
                        self.assertEqual(rcs[-1], 0)
                        self._assert_clean(proj)
                    with open(self.path, "w", encoding="utf-8") as f:
                        f.write(IRREGULAR)
                    rcs = self._steps(proj, "iP", "iU", "uU", "uP")
                self.assertEqual(rcs, [0, 0, 0, 0])
                self.assertEqual(self._user_text(), IRREGULAR)
                self._assert_clean(proj)

    def test_fresh_user_file_snapshot_replaces_its_kept_record(self):
        # Review 6 (F-install#3): ctempo went into the user file and out again while a manual
        # mod stayed for the project file, so the user file has a kept record. The user then
        # edited (or created) it, and an install with claude on PATH runs the plugin commands
        # for the first time. The snapshot taken then is the newer baseline from before the
        # plugin CLI, so the file comes back as it was right before them.
        proj = self._proj()
        edits = (("key", IRREGULAR, lambda t: t.replace("{\n", '{\n  "sandbox": {"enabled": true},\n', 1)),
                 ("reformat", IRREGULAR, lambda t: json.dumps(json.loads(t), indent=4, ensure_ascii=False)),
                 ("create {}", None, lambda t: "{}\n"),
                 ("create", None, lambda t: IRREGULAR))
        for name, before, edit in edits:
            for last in ("uP", "u*"):
                with self.subTest(edit=name, last=last):
                    self._reset()
                    with self._user_file(before):
                        self._steps(proj, "iU-nobin", "iP-nobin", "uU-nobin")
                        edited = edit(self._user_text())
                        with open(self.path, "w", encoding="utf-8") as f:
                            f.write(edited)
                        rcs = self._steps(proj, "iP", last)
                    self.assertEqual(rcs, [0, 0])
                    self.assertEqual(self._user_text(), edited)
                    self._assert_clean(proj)

    def test_command_put_in_compose_survives_replace_after_the_record_is_lost(self):
        # Review 6 (F-install#4): install.json is gone, and the user put or changed a command in
        # ctempo's --compose. `install --replace` drops it from the statusLine, so the record it
        # rebuilds must hold it ("kept in the backup and in install.json"), whether the newest
        # backup from before ctempo shows no statusLine, another one, or there is no backup left.
        newer = "npx -y ccstatusline@latest"
        theirs = {"type": "command", "command": "~/old-line.sh"}
        for start, backups in ((SETTINGS, True), (dict(SETTINGS, statusLine=theirs), True), (SETTINGS, False)):
            with self.subTest(statusLine=start.get("statusLine"), backups=backups):
                self._reset()
                self.write(start)
                self.run_cmd(install.install, args(self.path, apply=True))
                shutil.rmtree(self.home)  # install.json gone
                if not backups:  # and the backups: a repin rebuilds a record that knows nothing
                    for n in os.listdir(self.dir):
                        if ".ctempo-backup-" in n:
                            os.remove(os.path.join(self.dir, n))
                    self.run_cmd(install.install, args(self.path, apply=True))
                obj = json.loads(self.read())
                obj["statusLine"]["command"] = install.our_command(sys.executable, install.entry_path(), newer)
                obj["statusLine"]["padding"] = 0
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(obj, indent=2, ensure_ascii=False))
                rc, out = self.run_cmd(install.install, args(self.path, apply=True, replace=True))
                self.assertIn("REPLACE", out)
                self.assertIsNone(install.compose_of(json.loads(self.read())["statusLine"]["command"]))
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                after = json.loads(self.read())
                self.assertEqual((after.pop("statusLine", None) or {}).get("command"), newer)
                self.assertEqual(after, SETTINGS)

    def test_user_file_created_after_the_mod_is_restored_not_deleted(self):
        # Review 6 (F-install#5): there was no user file when the mod went in; the user then
        # created one ('{}', or blank), and ctempo went into it and out again. The file right
        # before that install is its first baseline, so it comes back; the snapshot from before
        # the mod (no file) is only a fallback and must not get it deleted.
        proj = self._proj()
        for created in ("{}\n", "{}", ""):
            for steps in (("iU-nobin", "uU-nobin"), ("iU-nobin", "uU-nobin", "uP-nobin"),
                          ("iU", "uU", "uP"), ("iU", "uP", "uU")):
                with self.subTest(created=created, steps=steps):
                    self._reset()
                    with self._user_file(None):
                        self._steps(proj, "iP-nobin")
                        with open(self.path, "w", encoding="utf-8") as f:
                            f.write(created)
                        rcs = self._steps(proj, *steps)
                    self.assertEqual(rcs, [0] * len(steps))
                    self.assertEqual(self._user_text(), created)

    def test_user_file_the_plugin_cli_writes_during_uninstall_comes_back(self):
        # Install fuzzer: the plugin CLI never wrote for a manual mod, but the reverse commands
        # at uninstall rewrite the user file (or create it) although there is nothing to
        # remove. Then it is the plugin CLI's doing, so the file comes back as it was (or goes).
        proj = self._proj()
        for before, steps in ((IRREGULAR, ("iP-nobin", "uP")), (IRREGULAR.replace("\n", "\r\n"), ("iP-nobin", "uP")),
                              (None, ("iP-nobin", "uP")), (None, ("iU-hang", "uU-hang", "u*"))):
            with self.subTest(before=before, steps=steps):
                self._reset()
                with self._user_file(before):
                    rcs = self._steps(proj, *steps)
                self.assertEqual(rcs[-1], 0)
                if before is None:
                    self.assertFalse(os.path.exists(self.path))
                else:
                    with open(self.path, "rb") as f:
                        self.assertEqual(f.read(), before.encode("utf-8"))
                self._assert_clean(proj)

    def test_plugin_keys_gone_from_the_user_file_are_not_left_behind_empty(self):
        # Install fuzzer: the user file listed another plugin, so both plugin keys were there at
        # the snapshot. The user then blanked or deleted the file, and the plugin CLI wrote its
        # keys again (a later install, or the printed commands run by hand): they are not the
        # user's any more, so uninstall must not leave them behind as empty objects.
        proj = self._proj()
        for how in ("blank", "delete"):
            for steps in (("iP", "edit", "iP", "uP"), ("iU", "edit", "iU", "uU"),
                          ("iP-nobin", "edit", "mod-by-hand", "uP"),
                          ("iP-nobin", "edit", "mod-by-hand", "iP-nobin", "unmod-by-hand", "uP")):
                with self.subTest(how=how, steps=steps):
                    self._reset()
                    with self._user_file(WITH_PLUGINS):
                        i = steps.index("edit")
                        self._steps(proj, *steps[:i])
                        if how == "blank":
                            with open(self.path, "w") as f:
                                f.write("")
                        else:
                            os.remove(self.path)
                        rcs = self._steps(proj, *steps[i + 1:])
                    self.assertEqual(rcs[-1], 0)
                    text = self._user_text()
                    self.assertEqual(json.loads(text) if text and text.strip() else {}, {})
                    state = install.load_state()
                    self.assertEqual(state.get("records"), {})
                    self.assertNotIn("mod", state)

    def test_plugin_keys_that_hold_only_ctempo_are_not_the_users(self):
        # Install fuzzer: the user ran the plugin commands by hand before ctempo's snapshot of
        # the user file, so its plugin keys hold nothing but ctempo's entries. They are the
        # plugin CLI's, so uninstall removes them once empty.
        proj = self._proj()
        for steps in (("mod-by-hand", "iP", "uP"), ("mod-by-hand", "iU", "uU"), ("mod-by-hand", "iP-nobin", "uP")):
            with self.subTest(steps=steps):
                self._reset()
                with self._user_file(IRREGULAR):
                    rcs = self._steps(proj, *steps)
                self.assertEqual(rcs[-1], 0)
                self.assertEqual(json.loads(self._user_text()), json.loads(IRREGULAR))
                self._assert_clean(proj)

    def test_probe_and_command_quoting(self):
        ok, ver, sq, ms, note = install.probe(sys.executable)
        self.assertTrue(ok, note)
        self.assertFalse(install.probe("python3")[0])  # not absolute
        cmd = install.our_command("/opt/my py/python3", "/x/bin/ctempo", "echo 'a b'")
        self.assertEqual(install.compose_of(cmd), "echo 'a b'")
        self.assertTrue(install.is_ours(cmd))

    def test_tidy_leaves_an_invalid_user_file_alone(self):
        # Review 6 follow-up: a user settings file that is not valid JSON made
        # the tidy raise out of uninstall.
        proj = os.path.join(self.dir, "proj-settings.json")
        with open(proj, "w") as f:
            f.write('{"model": "sonnet"}')
        quiet = lambda cmd, env=None: (0, "ok", "")  # the plugin CLI, writing nothing
        with self._user_file(IRREGULAR), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                mock.patch.object(install, "run_claude", plugin_cli(self.path)):
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
            with open(self.path, "w") as f:
                f.write('{"model": "opus",')  # the user broke it by hand
            with mock.patch.object(install, "run_claude", quiet):
                rc, out = self.run_cmd(install.uninstall, args(proj, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertIn("Left", out)
        self.assertEqual(self.read(), '{"model": "opus",')

    def test_snapshot_of_an_invalid_user_file(self):
        # Review 6 follow-up: an invalid user file is not "absent": it exists,
        # and its plugin keys are unknown, so none is ever removed.
        proj = os.path.join(self.dir, "proj-settings.json")
        with open(proj, "w") as f:
            f.write('{"model": "sonnet"}')
        quiet = lambda cmd, env=None: (0, "ok", "")
        with self._user_file('{"oops"'), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                mock.patch.object(install, "run_claude", quiet):
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
            mod = install.load_state()["mod"]
            self.assertTrue(mod["had_file"])
            self.assertEqual(mod["had_keys"], {k: True for k in install.PLUGIN_KEYS})
            rc, out = self.run_cmd(install.uninstall, args(proj, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), '{"oops"')

    def test_backups_sort_by_time_then_sequence(self):
        # Review 6 follow-up: names sorted as strings put "-9" after "-10".
        self.write(SETTINGS)
        for suffix, cmd in (("", "zero"), ("-9", "nine"), ("-10", "ten")):
            with open(self.path + ".ctempo-backup-20261014-090000" + suffix, "w") as f:
                f.write(json.dumps(dict(SETTINGS, statusLine={"type": "command", "command": cmd})))
        with open(self.path + ".ctempo-backup-20261013-235959-99", "w") as f:
            f.write(json.dumps(dict(SETTINGS, statusLine={"type": "command", "command": "yesterday"})))
        had, sl = install._original_from_backups(self.path)
        self.assertTrue(had)
        self.assertEqual(sl["command"], "ten")

    # -- seventh review --------------------------------------------------------------
    def test_uninstall_no_mod_keeps_what_the_kept_mod_needs(self):
        # Review 7 (install-fuzz#0): `uninstall --no-mod` keeps the mod, which reads
        # runtime.json, so runtime.json and the launcher stay with it; the full uninstall
        # that removes the mod later removes them too.
        proj = self._proj()
        runtime = os.path.join(self.home, "runtime.json")
        lp = os.path.join(self.dir, "bin", "ctempo")
        for target in (self.path, proj):
            with self.subTest(target=os.path.basename(target)):
                self._reset()
                with self._user_file(IRREGULAR), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                        mock.patch.object(install, "run_claude", plugin_cli(self.path)):
                    self.run_cmd(install.install, args(target, apply=True, no_mod=False, bin_dir=os.path.dirname(lp)))
                    rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=True))
                    self.assertEqual(rc, 0, out)
                    self.assertIn(install.PLUGIN_ID, json.loads(self.read())["enabledPlugins"])  # the mod stays
                    self.assertTrue(install.load_state().get("mod"))
                    self.assertTrue(os.path.exists(runtime), out)
                    self.assertTrue(os.path.exists(lp), out)
                    rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.read(), IRREGULAR)
                self.assertFalse(os.path.exists(runtime))
                self.assertFalse(os.path.exists(lp))
                self._assert_clean(proj)

    def test_uninstall_removes_the_mod_after_install_json_is_lost(self):
        # Review 7 (install-fuzz#1): the data folder was wiped (or the shell has another
        # CTEMPO_HOME), so install.json has no mod record, but the user file still lists
        # the mod. Uninstall rebuilds the record from the user file, says so, and runs the
        # reverse commands, also in the dry run; with claude missing it exits 1 to retry.
        proj = self._proj()
        cli = plugin_cli(self.path)
        for target in (self.path, proj):
            for first in ((), ("u*-nobin",)):
                with self.subTest(target=os.path.basename(target), first=first):
                    self._reset()
                    calls = []

                    def recording(cmd, env=None):
                        calls.append(cmd)
                        return cli(cmd)

                    with self._user_file(IRREGULAR):
                        self._steps(proj, "iU" if target == self.path else "iP")
                        shutil.rmtree(self.home)  # install.json gone
                        if first:
                            rcs = self._steps(proj, *first)
                            self.assertEqual(rcs, [1])
                            self.assertTrue(install.load_state().get("mod"))  # kept for the rerun
                        with mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                                mock.patch.object(install, "run_claude", recording):
                            rc, out = self.run_cmd(install.uninstall, args(target, no_mod=False))  # dry run
                            self.assertEqual(rc, 0, out)
                            self.assertIn("claude plugin uninstall", out)
                            if not first:
                                self.assertIn("no record of it", out)
                            self.assertEqual(calls, [])
                            rc, out = self.run_cmd(install.uninstall, args(target, apply=True, no_mod=False))
                    self.assertEqual(rc, 0, out)
                    self.assertEqual(calls, install.unmod_commands())
                    # Without install.json no snapshot is left to restore bytes from: the JSON comes back.
                    self.assertEqual(json.loads(self.read()), json.loads(IRREGULAR))
                    self.assertEqual(json.loads(install.read_text(proj)), {"model": "sonnet"})
                    state = install.load_state()
                    self.assertEqual(state.get("records"), {})
                    self.assertNotIn("mod", state)

    def _v1_install(self, text):
        """What version 1 (c016f98) left after `install --apply` into the user file with
        claude on PATH: its backup, the file with the plugin CLI's keys, and its record."""
        backup = os.path.realpath(self.path) + ".ctempo-backup-20261001-090000"
        with open(backup, "w", encoding="utf-8") as f:
            f.write(text)
        sl = {"type": "command", "command": install.our_command(sys.executable, install.entry_path()),
              "refreshInterval": 60}
        obj = dict(json.loads(text), statusLine=sl, enabledPlugins={install.PLUGIN_ID: True},
                   extraKnownMarketplaces={install.MARKETPLACE: {"source": {}}})
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, indent=2))
        os.makedirs(self.home, exist_ok=True)
        with open(os.path.join(self.home, "runtime.json"), "w") as f:
            f.write("{}")
        with open(install.state_path(), "w") as f:
            json.dump({"installed": True, "settings_path": self.path, "had_file": True, "backup_path": backup,
                       "original_sha256": install.sha(text), "had_statusLine": False, "original_statusLine": None,
                       "had_keys": {k: False for k in install.PLUGIN_KEYS}, "python": sys.executable,
                       "entry": install.entry_path(), "written_statusLine": sl,
                       "mod": {"marketplace": install.MARKETPLACE, "plugin": install.PLUGIN_ID, "root": install.repo_root()}}, f)

    def test_version_1_mod_record_keeps_the_user_file_baseline_for_a_rerun(self):
        # Review 7 (install-fuzz#2): version 1's mod record names no user file; its plugin
        # commands always edited the default one. An uninstall that leaves the mod (claude
        # missing or hanging, or --no-mod) must keep that file's record, so the run that
        # removes the mod brings the file back byte for byte, without empty plugin keys.
        for first in ("u*-nobin", "u*-hang", "u*-nomod"):
            with self.subTest(first=first):
                self._reset()
                with self._user_file(IRREGULAR):
                    self._v1_install(IRREGULAR)
                    rcs = self._steps(None, first, "u*")
                self.assertEqual(rcs, [0 if first.endswith("nomod") else 1, 0])
                self.assertEqual(self.read(), IRREGULAR)
                state = install.load_state()
                self.assertEqual(state.get("records"), {})
                self.assertNotIn("mod", state)

    def test_version_1_mod_record_takes_the_user_files_kept_record_with_it(self):
        # Review 7 (install-fuzz#2, found again by the stress fuzzer on the fix): version 1
        # installed into a project file, its plugin commands wrote the default user file, and
        # ctempo went into that file with --no-mod and out again while the mod stayed. The
        # user file's record is kept for the mod; the uninstall that removes the mod must
        # tidy that file and drop the record, though the version-1 mod names no user file.
        proj = self._proj()
        sl = {"type": "command", "command": install.our_command(sys.executable, install.entry_path()),
              "refreshInterval": 60}
        with open(proj, "w") as f:
            f.write(json.dumps({"model": "sonnet", "statusLine": sl}, indent=2))
        backup = os.path.realpath(proj) + ".ctempo-backup-20261001-090000"
        with open(backup, "w") as f:
            f.write('{"model": "sonnet"}')
        os.makedirs(self.home)
        with open(install.state_path(), "w") as f:  # what version 1 wrote for `install --apply --settings proj`
            json.dump({"installed": True, "settings_path": proj, "had_file": True, "backup_path": backup,
                       "original_sha256": install.sha('{"model": "sonnet"}'), "had_statusLine": False,
                       "original_statusLine": None, "had_keys": {k: False for k in install.PLUGIN_KEYS},
                       "python": sys.executable, "entry": install.entry_path(), "written_statusLine": sl,
                       "mod": {"marketplace": install.MARKETPLACE, "plugin": install.PLUGIN_ID, "root": install.repo_root()}}, f)
        with self._user_file(IRREGULAR):
            for c in install.mod_commands(install.repo_root()):
                plugin_cli(self.path)(c)  # version 1's plugin commands
            rcs = self._steps(proj, "iU-nomod", "uU", "uP")
        self.assertEqual(rcs, [0, 0, 0])
        self.assertEqual(json.loads(self.read()), json.loads(IRREGULAR))
        self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
        state = install.load_state()
        self.assertEqual(state.get("records"), {})
        self.assertNotIn("mod", state)

    def test_command_put_in_compose_wins_over_a_backup_when_uninstall_finds_no_record(self):
        # Review 7 (install-fuzz#3): install.json is gone and the user changed the command in
        # ctempo's --compose. Uninstall restores that command, as a repin would have recorded
        # it, not the older one in the backup from before ctempo (and, when there was no
        # statusLine before ctempo, without the refreshInterval ctempo added).
        newer = "npx -y ccstatusline@latest"
        theirs = {"type": "command", "command": "~/old-line.sh"}
        for start, want in ((dict(SETTINGS, statusLine=theirs), dict(theirs, command=newer)),
                            (SETTINGS, {"type": "command", "command": newer, "padding": 0})):
            with self.subTest(statusLine=start.get("statusLine")):
                self._reset()
                self.write(start)
                self.run_cmd(install.install, args(self.path, apply=True))
                shutil.rmtree(self.home)  # install.json gone
                obj = json.loads(self.read())
                obj["statusLine"]["command"] = install.our_command(sys.executable, install.entry_path(), newer)
                obj["statusLine"]["padding"] = 0
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(obj, indent=2, ensure_ascii=False))
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                after = json.loads(self.read())
                self.assertEqual(after.pop("statusLine", None), want)
                self.assertEqual(after, SETTINGS)

    def test_command_put_in_compose_survives_replace_and_then_a_lost_record(self):
        # Review 7 (install fuzzer): the user changed the command in ctempo's --compose, then
        # `install --replace` dropped it from the file (kept in the backup it took and in
        # install.json), then install.json was lost. That backup's --compose wins over the
        # older backup from before ctempo, or is used when there is none (then with the
        # statusLine's other keys as ctempo ran it: nothing tells which were ctempo's).
        newer = "npx -y ccstatusline@latest"
        theirs = {"type": "command", "command": "~/old-line.sh"}
        for start, want in ((dict(SETTINGS, statusLine=theirs), dict(theirs, command=newer)),
                            (SETTINGS, {"type": "command", "command": newer, "padding": 0}),
                            (None, {"type": "command", "command": newer, "padding": 0, "refreshInterval": 60})):
            with self.subTest(start=start and start.get("statusLine", "none")):
                self._reset()
                if start is None:
                    if os.path.exists(self.path):
                        os.remove(self.path)
                else:
                    self.write(start)
                self.run_cmd(install.install, args(self.path, apply=True))
                obj = json.loads(self.read())
                obj["statusLine"]["command"] = install.our_command(sys.executable, install.entry_path(), newer)
                obj["statusLine"]["padding"] = 0
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(obj, indent=2, ensure_ascii=False))
                rc, out = self.run_cmd(install.install, args(self.path, apply=True, replace=True))
                self.assertIn("REPLACE", out)
                shutil.rmtree(self.home)  # install.json gone
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                after = json.loads(self.read())
                self.assertEqual(after.pop("statusLine", None), want)
                self.assertEqual(after, start and SETTINGS or {})

    def test_uninstall_no_mod_keeps_runtime_json_for_a_mod_only_the_user_file_lists(self):
        # Review 7 (install fuzzer): install.json was lost while the mod was installed, an
        # `install --no-mod` wrote runtime.json again, and `uninstall --no-mod` deleted it as if
        # no mod were left, so a later uninstall that failed kept a mod record without it. The
        # user file lists the mod: it is kept, and runtime.json with it, until a full uninstall.
        runtime = os.path.join(self.home, "runtime.json")
        with self._user_file(IRREGULAR):
            self._steps(None, "iU")
            shutil.rmtree(self.home)  # install.json gone
            rcs = self._steps(None, "iU-nomod", "u*-nomod")
            self.assertEqual(rcs, [0, 0])
            self.assertTrue(install.load_state().get("mod"))
            self.assertTrue(os.path.exists(runtime))
            rcs = self._steps(None, "u*-nobin")
            self.assertEqual(rcs, [1])
            self.assertTrue(os.path.exists(runtime))
            rcs = self._steps(None, "u*")
        self.assertEqual(rcs, [0])
        self.assertEqual(json.loads(self.read()), json.loads(IRREGULAR))
        self.assertFalse(os.path.exists(runtime))
        state = install.load_state()
        self.assertEqual(state.get("records"), {})
        self.assertNotIn("mod", state)

    def test_the_users_own_empty_plugin_key_survives(self):
        # Review 7 follow-up: the plugin CLI leaves "enabledPlugins": {} after
        # its last plugin goes, so many user files hold it. Once the mod was in
        # it, uninstall took the key for the plugin CLI's and removed it.
        proj = os.path.join(self.dir, "proj-settings.json")
        with open(proj, "w") as f:
            f.write('{"model": "sonnet"}')
        before = '{\n  "model": "opus",\n  "enabledPlugins": {}\n}\n'
        with self._user_file(before), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                mock.patch.object(install, "run_claude", plugin_cli(self.path)):
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))  # a repin looks again
            rc, out = self.run_cmd(install.uninstall, args(proj, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), before)

    # -- eighth review ---------------------------------------------------------------
    def test_launcher_follows_a_repin_and_only_the_newest_is_ctempos(self):
        # Review 8 (delta-install#0): a repin without --bin-dir left the launcher running the
        # old interpreter, and a launcher in an earlier --bin-dir outlived the full uninstall.
        before = self.write(SETTINGS)
        first, second = os.path.join(self.dir, "bin"), os.path.join(self.dir, "bin2")
        lp1, lp2 = os.path.join(first, "ctempo"), os.path.join(second, "ctempo")
        alt = self.alt_python()
        self.run_cmd(install.install, args(self.path, apply=True, bin_dir=first))
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=alt))
        self.assertEqual(rc, 0, out)
        self.assertEqual(install.read_text(lp1), install.launcher_text(alt, install.entry_path()))
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=second))
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(lp1), out)
        self.assertEqual(install.read_text(lp2), install.launcher_text(sys.executable, install.entry_path()))
        os.remove(lp2)  # the user deleted it: only --bin-dir brings it back
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=alt))
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(lp2), out)
        self.assertNotIn("launcher", install.load_state())
        # A file the user put in a launcher's place is theirs: never rewritten or removed.
        self.run_cmd(install.install, args(self.path, apply=True, bin_dir=first))
        with open(lp1, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        self.run_cmd(install.install, args(self.path, apply=True, python=alt))
        self.run_cmd(install.install, args(self.path, apply=True, bin_dir=second))
        rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(install.read_text(lp1), "#!/bin/sh\necho mine\n")
        self.assertFalse(os.path.exists(lp2), out)
        self.assertEqual(self.read(), before)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_backups_keep_their_order_when_the_clock_moves_west(self):
        # Review 8 (delta-install#1): backups were named in local time, so after a move west
        # (or the fall-back hour) a newer backup sorted before an older one, and an uninstall
        # without install.json restored a statusLine the user had removed.
        with time_zone("EST5"):  # New York
            self.write(dict(SETTINGS, statusLine={"type": "command", "command": "~/bin/one.sh"}))
            self.run_cmd(install.install, args(self.path, apply=True))
            self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.write(SETTINGS)  # the user removed their statusLine
        with time_zone("PST8"):  # Los Angeles, the same day
            self.run_cmd(install.install, args(self.path, apply=True))
            shutil.rmtree(self.home)  # install.json goes with the data folder
            rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.read()), SETTINGS)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_backups_named_in_utc_sort_among_older_local_time_names(self):
        # Review 8 (delta-install#1): new backups are named in UTC (a Z marks them), older ones
        # in local time; the order reads each name in its own time.
        with time_zone("JST-9"):  # Tokyo, 9 hours ahead of UTC
            for legacy, want in (("20261014-120000", "utc"), ("20261014-150000", "legacy")):
                with self.subTest(legacy=legacy):
                    self._reset()
                    for name, cmd in ((legacy, "legacy"), ("20261014-050000Z", "utc")):
                        with open(self.path + ".ctempo-backup-" + name, "w") as f:
                            f.write(json.dumps(dict(SETTINGS, statusLine={"type": "command", "command": cmd})))
                    self.assertEqual(install._original_from_backups(self.path)[1]["command"], want)

    def test_the_users_own_empty_plugin_key_survives_other_plugins_coming_and_going(self):
        # Review 8 (delta-install#2): ctempo saw the user's key with another plugin in it, and the
        # user then removed that plugin with the plugin CLI. Holding only ctempo's entry at the
        # next look, the key counted as changed out of ctempo's sight, so uninstall removed it.
        def other(add):
            """The user installs or removes another plugin with the plugin CLI."""
            obj = json.loads(self.read())
            if add:
                obj["enabledPlugins"]["other@mkt2"] = True
            else:
                obj["enabledPlugins"] = {k: v for k, v in obj["enabledPlugins"].items() if not k.startswith("other@")}
            with open(self.path, "w") as f:
                f.write(json.dumps(obj, indent=2))

        empty = '{\n  "model": "opus",\n  "enabledPlugins": {}\n}\n'
        listed = '{\n  "model": "opus",\n  "enabledPlugins": {"other@mkt": true}\n}\n'
        for before, steps in ((empty, ("iU", "iP", "add", "uP", "remove", "uU")), (listed, ("iU", "remove", "uU")),
                              (listed, ("iP", "remove", "uP")), (empty, ("iP", "add", "iP", "remove", "uP"))):
            with self.subTest(steps=steps):
                self._reset()
                proj = self._proj()
                with self._user_file(before):
                    for s in steps:
                        if s in ("add", "remove"):
                            other(s == "add")
                        else:
                            self.assertEqual(self._steps(proj, s), [0])
                self.assertEqual(json.loads(self.read()), {"model": "opus", "enabledPlugins": {}})
                if before == empty:
                    self.assertEqual(self.read(), before)
                self._assert_clean(proj)


if __name__ == "__main__":
    unittest.main()
