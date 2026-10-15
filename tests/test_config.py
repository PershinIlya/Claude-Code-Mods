"""The ctempo home and the config file, as the user writes them."""

import argparse
import codecs
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ctempo import cli, commands, config, install, statusline

from .helpers import TZ_UTC, payload, write_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "bin", "ctempo")
USER = "C:\\Users\\me"


def no_drive(value):
    """config.windows_home's message, word for word the mod's (resolveHome)."""
    return ("CTEMPO_HOME %s has no drive; Git Bash rewrites such a path to a folder under its own install. "
            "Write it with a drive, e.g. C:%s, or as ~/..." % (value, value))


class NoDriveHome(unittest.TestCase):
    """Review 15: Git Bash, which runs the statusLine on Windows, rewrites a
    CTEMPO_HOME rooted with a slash and no drive (/srv/ctempo) to a folder
    under its own install (C:\\Program Files\\Git\\srv\\ctempo) before
    python.exe sees it, while the mod and PowerShell used C:\\srv\\ctempo: two
    folders, and under Program Files none a user can create. Windows refuses
    such a value, the same way in the CLI and the mod
    (mods/ctempo/tests/register.test.ts); every entry point says why."""

    REFUSED = ("/srv/ctempo", "/ctempo", "/tmp/x", "/", "/cc/x", "/1/x", "/c\\x", "/\u00e9/x")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctempo-nodrive-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.claude = os.path.join(self.dir, "claude")
        os.makedirs(self.claude)
        env = mock.patch.dict(os.environ, {"USERPROFILE": USER, "HOME": os.path.join(self.dir, "home"),
                                           "CLAUDE_CONFIG_DIR": self.claude, "CTEMPO_HOME": "/srv/ctempo"})
        env.start()
        self.addCleanup(env.stop)
        # A home taken as C:\srv\ctempo (as before the fix) is a relative path off Windows: keep it in here.
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.dir)

    def windows(self, on=True):
        return mock.patch.object(config, "IS_WINDOWS", on)

    def test_windows_refuses_a_slash_rooted_home_without_a_drive(self):
        for value in self.REFUSED:
            with self.assertRaises(config.HomeError) as caught:
                config.windows_home(value)
            self.assertEqual(str(caught.exception), no_drive(value))
            self.assertIsInstance(caught.exception, ValueError)
            with self.windows(), mock.patch.dict(os.environ, {"CTEMPO_HOME": value}), \
                    self.assertRaises(config.HomeError):
                config.home()

    def test_drives_unc_paths_and_a_backslash_root_are_kept(self):
        kept = (("\\srv\\ctempo", "C:\\srv\\ctempo"), ("/c", "C:\\"), ("/d/srv/ctempo", "D:\\srv\\ctempo"),
                ("C:/srv/ctempo", "C:\\srv\\ctempo"), ("~/ctempo", USER + "\\ctempo"),
                ("//server/share/ctempo", "\\\\server\\share\\ctempo"),
                ("/\\server\\share\\ctempo", "\\\\server\\share\\ctempo"), ("", USER + "\\.claude-tempo"))
        for value, want in kept:
            self.assertEqual(config.windows_home(value), want, value)
        if os.name != "nt":
            with self.windows(False):  # macOS and Linux: an absolute path, as given
                self.assertEqual(config.home(), "/srv/ctempo")

    def test_the_statusline_still_prints_its_line(self):
        # Nowhere to log (errors.log is in the home): the bare line, exit 0.
        class Out:
            def __init__(self):
                self.buffer = io.BytesIO()

        out = Out()
        now = time.time()
        p = payload(five=(42, int(now + 3600)), week=(30, int(now + 86400)))
        with self.windows(), mock.patch.dict(os.environ, {"NO_COLOR": "1"}), mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(p).encode()))):
            self.assertEqual(statusline.main(["--no-compose"]), 0)
        line = out.buffer.getvalue().decode()
        self.assertTrue(line.startswith("5h 42% @") and " wk 30% @" in line, line)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with self.windows(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(install, "claude_bin", lambda: None):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_commands_say_why_without_a_traceback(self):
        for argv in (["now"], ["report"], ["check"], ["config", "path"], ["bootstrap", "--dry-run"],
                     ["uninstall"], ["uninstall", "--apply"]):
            rc, out, err = self.cli(*argv)
            self.assertEqual((rc, err), (1, "ctempo: %s\n" % no_drive("/srv/ctempo")), argv)
            self.assertEqual(out, "", argv)

    def test_doctor_reports_a_failing_row(self):
        rc, out, err = self.cli("doctor", "--settings", os.path.join(self.dir, "settings.json"))
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("FAIL CTEMPO_HOME  (%s)" % no_drive("/srv/ctempo"), out)
        self.assertNotIn("CTEMPO_HOME in the settings env", out)  # the same value: one row
        for gone in ("no runtime.json", "no database yet", "errors.log"):
            self.assertNotIn(gone, out)

    def test_install_refuses_up_front(self):
        settings = os.path.join(self.dir, "settings.json")
        write_json(settings, {"model": "opus"})
        for apply in (False, True):
            rc, out, err = self.cli(*(["install", "--settings", settings, "--no-mod", "--no-bootstrap"]
                                      + (["--apply"] if apply else [])))
            self.assertEqual((rc, out, err), (1, "error: %s\n" % no_drive("/srv/ctempo"), ""))
        with open(settings, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"model": "opus"})
        self.assertEqual(sorted(os.listdir(self.dir)), ["claude", "settings.json"])

    def test_install_and_doctor_check_the_raw_value_in_the_settings_env(self):
        # Under Git Bash the CLI only sees the rewritten value, which works: the
        # settings env, which Claude Code passes to the statusLine, has the raw one.
        home = os.path.join(self.dir, "ctempo")
        user = os.path.join(self.claude, "settings.json")
        write_json(user, {"env": {"CTEMPO_HOME": "/tmp/ctempo"}})
        with mock.patch.object(config, "home", lambda: home), mock.patch.dict(os.environ, {"CTEMPO_HOME": home}):
            rc, out, err = self.cli("install", "--settings", user, "--no-mod", "--no-bootstrap", "--apply")
            self.assertEqual((rc, out), (1, "error: %s\n" % no_drive("/tmp/ctempo")))
            self.assertFalse(os.path.exists(home))
            rc, out, err = self.cli("doctor", "--settings", user)
            self.assertIn("FAIL CTEMPO_HOME in the settings env  (%s)" % no_drive("/tmp/ctempo"), out)
            self.assertEqual(rc, 1)
            write_json(user, {"env": {"CTEMPO_HOME": "C:/tmp/ctempo"}})
            self.assertIsNone(install.home_problem())
            with self.windows(False):  # macOS and Linux: no such rule
                write_json(user, {"env": {"CTEMPO_HOME": "/tmp/ctempo"}})
                self.assertIsNone(install.home_problem())

    @unittest.skipUnless(os.name == "nt", "Windows rules; patched above everywhere else")
    def test_on_windows_the_cli_and_the_statusline(self):
        # No Git Bash between here and python.exe: the raw value, as PowerShell and the mod pass it.
        env = dict(os.environ, CTEMPO_HOME="/srv/ctempo-test-nodrive", TZ=TZ_UTC, USERPROFILE=self.dir, NO_COLOR="1")
        p = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", ENTRY, "now"], capture_output=True, env=env,
                           timeout=60)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(p.stderr.decode().strip(), "ctempo: " + no_drive("/srv/ctempo-test-nodrive"))
        data = json.dumps(payload(five=(42, int(time.time() + 3600)))).encode()
        p = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", ENTRY, "statusline"], input=data,
                           capture_output=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.decode().startswith("5h 42% @"), p.stdout)
        self.assertFalse(os.path.exists(os.path.splitdrive(self.dir)[0] + "\\srv\\ctempo-test-nodrive"))


class ConfigEncoding(unittest.TestCase):
    """Review 15: Windows PowerShell 5.1's `>` and Out-File write UTF-16 with a
    BOM. config.json saved so was ignored as a whole, with no word anywhere,
    and `ctempo config show` printed the defaults under its path."""

    WANT = {"target_week": 80, "display": {"symbols": "ascii"}}

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctempo-cfgenc-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "config.json")

    def save(self, data):
        with open(self.path, "wb") as f:
            f.write(data)

    def test_utf16_and_utf8_with_or_without_a_bom_are_read(self):
        text = json.dumps(self.WANT, indent=2).replace("\n", "\r\n") + "\r\n"
        for name, data in (("UTF-16 LE", codecs.BOM_UTF16_LE + text.encode("utf-16-le")),
                           ("UTF-16 BE", codecs.BOM_UTF16_BE + text.encode("utf-16-be")),
                           ("UTF-8 with a BOM", codecs.BOM_UTF8 + text.encode("utf-8")),
                           ("UTF-8", text.encode("utf-8"))):
            self.save(data)
            cfg = config.load(self.path)
            self.assertEqual((cfg["target_week"], cfg["display"]["symbols"]), (80, "ascii"), name)
            self.assertIsNone(config.unreadable(self.path), name)

    def test_a_file_that_does_not_parse_still_loads_the_defaults(self):
        for data in (b"{\"target_week\": 80,", b"\xff\xfe{\x00", b"[1, 2]", b"", b"[" * 100000 + b"]" * 100000):
            self.save(data)
            self.assertEqual(config.load(self.path), config.DEFAULTS, data[:20])
            self.assertTrue(config.unreadable(self.path), data[:20])
        self.assertIsNone(config.unreadable(os.path.join(self.dir, "missing.json")))

    def show(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CTEMPO_HOME": self.dir}), contextlib.redirect_stdout(out):
            commands.cmd_config(argparse.Namespace(action="show"), config.load())
        return out.getvalue().splitlines()[0]

    def test_config_show_says_when_the_file_is_not_used(self):
        self.assertEqual(self.show(), "# %s (not created; defaults shown)" % self.path)
        self.save(json.dumps(self.WANT).encode("utf-16"))
        self.assertEqual(self.show(), "# %s" % self.path)
        self.save(b"{\"target_week\": 80,")
        self.assertRegex(self.show(), r"^# .*config\.json \(not used: .* \(char 19\); defaults shown\)$")
        self.save(b"[1, 2]")
        self.assertEqual(self.show(), "# %s (not used: not a JSON object; defaults shown)" % self.path)


if __name__ == "__main__":
    unittest.main()
