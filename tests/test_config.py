"""The pacer home and the config file, as the user writes them."""

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

from pacer import cli, commands, config, install, statusline

from .helpers import TZ_UTC, payload, write_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "bin", "pacer")
USER = "C:\\Users\\me"


def no_drive(value, drive="C:"):
    """config.windows_home's message, word for word the mod's (resolveHome)."""
    return ("PACER_HOME %s has no drive; Git Bash rewrites such a path to a folder under its own install. "
            "Write it with a drive, e.g. %s%s, or as ~/..." % (value, drive, value))


def drive_relative(value):
    """config.windows_home's message for C:x, word for word the mod's (resolveHome)."""
    return "PACER_HOME %s is relative to a drive's current folder; use an absolute path" % value


class NoDriveHome(unittest.TestCase):
    """Review 15: Git Bash, which runs the statusLine on Windows, rewrites a
    PACER_HOME rooted with a slash and no drive (/srv/pacer) to a folder
    under its own install (C:\\Program Files\\Git\\srv\\pacer) before
    python.exe sees it, while the mod and PowerShell used C:\\srv\\pacer: 2
    folders, and under Program Files none a user can create. Windows refuses
    such a value, the same way in the CLI and the mod
    (mods/pacer/tests/register.test.ts); every entry point says why."""

    REFUSED = ("/srv/pacer", "/pacer", "/tmp/x", "/", "/cc/x", "/1/x", "/c\\x", "/\u00e9/x")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pacer-nodrive-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.claude = os.path.join(self.dir, "claude")
        os.makedirs(self.claude)
        env = mock.patch.dict(os.environ, {"USERPROFILE": USER, "HOME": os.path.join(self.dir, "home"),
                                           "CLAUDE_CONFIG_DIR": self.claude, "PACER_HOME": "/srv/pacer"})
        env.start()
        self.addCleanup(env.stop)
        # A home taken as C:\srv\pacer (as before the fix) is a relative path off Windows: keep it in here.
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
            with self.windows(), mock.patch.dict(os.environ, {"PACER_HOME": value}), \
                    self.assertRaises(config.HomeError):
                config.home()

    def test_drives_unc_paths_and_a_backslash_root_are_kept(self):
        kept = (("\\srv\\pacer", "C:\\srv\\pacer"), ("/c", "C:\\"), ("/d/srv/pacer", "D:\\srv\\pacer"),
                ("C:/srv/pacer", "C:\\srv\\pacer"), ("~/pacer", USER + "\\pacer"),
                ("//server/share/pacer", "\\\\server\\share\\pacer"),
                ("/\\server\\share\\pacer", "\\\\server\\share\\pacer"), ("", USER + "\\.pacer"))
        for value, want in kept:
            self.assertEqual(config.windows_home(value), want, value)
        if os.name != "nt":
            with self.windows(False):  # macOS and Linux: an absolute path, as given
                self.assertEqual(config.home(), "/srv/pacer")

    def test_the_example_is_on_the_homes_drive(self):
        # Review 15 verify: older versions, the mod and PowerShell read /srv/pacer on the home's
        # drive. With the profile on D: the message said C:/srv/pacer, a folder with none of the data.
        for profile, drive in (("D:\\Users\\me", "D:"), ("\\\\server\\share\\me", "\\\\server\\share"),
                               ("\\Users\\me", "C:")):
            with mock.patch.dict(os.environ, {"USERPROFILE": profile}):
                with self.assertRaises(config.HomeError) as caught:
                    config.windows_home("/srv/pacer")
                self.assertEqual(str(caught.exception), no_drive("/srv/pacer", drive), profile)
        with mock.patch.dict(os.environ, {"USERPROFILE": "D:\\Users\\me"}):
            rc, out, err = self.cli("now")
        self.assertEqual((rc, err), (1, "pacer: %s\n" % no_drive("/srv/pacer", "D:")))
        env = {k: v for k, v in os.environ.items() if k not in ("USERPROFILE", "HOMEPATH", "HOMEDRIVE")}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(config.HomeError) as caught:
            config.windows_home("/srv/pacer")  # no home at all
        self.assertEqual(str(caught.exception), no_drive("/srv/pacer"))

    def test_windows_refuses_a_path_relative_to_a_drives_current_folder(self):
        # C:x is C:'s current folder plus x, which differs from one process to the next. The mod
        # refused it, while the CLI joined it to the home (C:\Users\me\x on the home's drive, an
        # unrelated folder on another), so the band and the line could use 2 folders.
        settings = os.path.join(self.dir, "settings.json")
        for value in ("C:x", "c:pacer", "D:", "C:.pacer", "D:x\\y"):
            with self.assertRaises(config.HomeError) as caught:
                config.windows_home(value)
            self.assertEqual(str(caught.exception), drive_relative(value))
            with mock.patch.dict(os.environ, {"PACER_HOME": value}):
                rc, out, err = self.cli("now")
                self.assertEqual((rc, out, err), (1, "", "pacer: %s\n" % drive_relative(value)), value)
                with self.windows(), self.assertRaises(config.HomeError):
                    config.home()
            # Install checks the value Claude Code passes to the statusLine: the settings env's.
            write_json(settings, {"env": {"PACER_HOME": value}})
            with mock.patch.dict(os.environ, {"PACER_HOME": os.path.join(self.dir, "pacer")}), \
                    mock.patch.object(config, "home", lambda: os.environ["PACER_HOME"]):
                rc, out, err = self.cli("install", "--settings", settings, "--no-mod", "--no-bootstrap", "--apply")
            self.assertEqual((rc, out), (1, "error: %s\n" % drive_relative(value)), value)
        self.assertEqual(sorted(os.listdir(self.dir)), ["claude", "settings.json"])
        for value, want in (("C:\\x", "C:\\x"), ("c:/x", "c:\\x"), ("/c/x", "C:\\x"), ("pacer", USER + "\\pacer")):
            self.assertEqual(config.windows_home(value), want, value)

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
            self.assertEqual((rc, err), (1, "pacer: %s\n" % no_drive("/srv/pacer")), argv)
            self.assertEqual(out, "", argv)

    def test_doctor_reports_a_failing_row(self):
        rc, out, err = self.cli("doctor", "--settings", os.path.join(self.dir, "settings.json"))
        self.assertEqual((rc, err), (1, ""))
        self.assertIn("FAIL PACER_HOME  (%s)" % no_drive("/srv/pacer"), out)
        self.assertNotIn("PACER_HOME in the settings env", out)  # the same value: one row
        for gone in ("no runtime.json", "no database yet", "errors.log"):
            self.assertNotIn(gone, out)

    def test_install_refuses_up_front(self):
        settings = os.path.join(self.dir, "settings.json")
        write_json(settings, {"model": "opus"})
        for apply in (False, True):
            rc, out, err = self.cli(*(["install", "--settings", settings, "--no-mod", "--no-bootstrap"]
                                      + (["--apply"] if apply else [])))
            self.assertEqual((rc, out, err), (1, "error: %s\n" % no_drive("/srv/pacer"), ""))
        with open(settings, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"model": "opus"})
        self.assertEqual(sorted(os.listdir(self.dir)), ["claude", "settings.json"])

    def test_install_and_doctor_check_the_raw_value_in_the_settings_env(self):
        # Under Git Bash the CLI only sees the rewritten value, which works: the
        # settings env, which Claude Code passes to the statusLine, has the raw one.
        home = os.path.join(self.dir, "pacer")
        user = os.path.join(self.claude, "settings.json")
        write_json(user, {"env": {"PACER_HOME": "/tmp/pacer"}})
        with mock.patch.object(config, "home", lambda: home), mock.patch.dict(os.environ, {"PACER_HOME": home}):
            rc, out, err = self.cli("install", "--settings", user, "--no-mod", "--no-bootstrap", "--apply")
            self.assertEqual((rc, out), (1, "error: %s\n" % no_drive("/tmp/pacer")))
            self.assertFalse(os.path.exists(home))
            rc, out, err = self.cli("doctor", "--settings", user)
            self.assertIn("FAIL PACER_HOME in the settings env  (%s)" % no_drive("/tmp/pacer"), out)
            self.assertEqual(rc, 1)
            write_json(user, {"env": {"PACER_HOME": "C:/tmp/pacer"}})
            self.assertIsNone(install.home_problem())
            with self.windows(False):  # macOS and Linux: no such rule
                write_json(user, {"env": {"PACER_HOME": "/tmp/pacer"}})
                self.assertIsNone(install.home_problem())

    @unittest.skipUnless(os.name == "nt", "Windows rules; patched above everywhere else")
    def test_on_windows_the_cli_and_the_statusline(self):
        # No Git Bash between here and python.exe: the raw value, as PowerShell and the mod pass it.
        env = dict(os.environ, PACER_HOME="/srv/pacer-test-nodrive", TZ=TZ_UTC, USERPROFILE=self.dir, NO_COLOR="1")
        p = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", ENTRY, "now"], capture_output=True, env=env,
                           timeout=60)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(p.stderr.decode().strip(), "pacer: " + no_drive("/srv/pacer-test-nodrive"))
        data = json.dumps(payload(five=(42, int(time.time() + 3600)))).encode()
        p = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", ENTRY, "statusline"], input=data,
                           capture_output=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.decode().startswith("5h 42% @"), p.stdout)
        self.assertFalse(os.path.exists(os.path.splitdrive(self.dir)[0] + "\\srv\\pacer-test-nodrive"))


class ConfigEncoding(unittest.TestCase):
    """Review 15: Windows PowerShell 5.1's `>` and Out-File write UTF-16 with a
    BOM. config.json saved so was ignored as a whole, with no word anywhere,
    and `pacer config show` printed the defaults under its path."""

    WANT = {"target_week": 80, "display": {"symbols": "ascii"}}

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pacer-cfgenc-")
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
        with mock.patch.dict(os.environ, {"PACER_HOME": self.dir}), contextlib.redirect_stdout(out):
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


class ComposeTimeout(unittest.TestCase):
    def test_longer_on_windows(self):
        # Review 15 CI: on windows-latest a composed `echo mine` run in a nested Windows
        # PowerShell 5.1 once took longer than 1.5 s to start, and the line lost it.
        self.assertEqual((config.compose_timeout_ms(True), config.compose_timeout_ms(False)), (3000, 1500))
        self.assertEqual(config.DEFAULTS["compose"]["timeout_ms"], config.compose_timeout_ms(os.name == "nt"))
        self.assertLessEqual(config.compose_timeout_ms(True), config.RANGES[("compose", "timeout_ms")][1])


if __name__ == "__main__":
    unittest.main()
