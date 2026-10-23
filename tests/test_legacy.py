"""Installs from before the rename, when pacer was called ctempo (pacer/legacy.py)."""

import os
import subprocess
import sys
import unittest
from unittest import mock

from pacer import config, install, legacy, menubar

from .helpers import TempHome

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Legacy(TempHome):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.dir, "u")
        os.makedirs(self.home)
        self.env = mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home})
        self.env.start()
        for k in ("PACER_HOME", "CTEMPO_HOME"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def test_the_old_folder_is_used_until_it_moves(self):
        old = os.path.join(self.home, ".claude-tempo")
        new = os.path.join(self.home, ".pacer")
        self.assertEqual(config.home(), new)  # nothing yet: the new name
        os.makedirs(old)
        self.assertEqual(config.home(), old)
        with mock.patch.dict(os.environ, {"CTEMPO_HOME": "elsewhere"}):
            self.assertEqual(config.home(), os.path.join(self.home, "elsewhere"))
            self.assertFalse(legacy.move_home(lambda *_: None))  # a folder named by the user stays
        lines = []
        self.assertTrue(legacy.move_home(lines.append))
        self.assertTrue(os.path.isdir(new) and not os.path.exists(old))
        self.assertEqual(config.home(), new)
        self.assertIn("pacer was called ctempo", lines[0])
        self.assertFalse(legacy.move_home(lines.append))  # once

    def test_old_commands_and_backups_are_ours(self):
        self.assertTrue(install.is_ours("/usr/bin/python3 -I -S /repo/bin/ctempo statusline --source cli"))
        self.assertTrue(install.BACKUP_SUFFIX.search("settings.json.ctempo-backup-20261020-101010Z"))
        self.assertTrue(install.BACKUP_SUFFIX.search("settings.json.pacer-backup-20261020-101010Z-2"))

    def test_the_old_mod_goes_when_it_is_there(self):
        calls = []

        def run(args):
            calls.append(args)
            return (0, "", "") if "uninstall" in args else (0, "", "")

        self.assertTrue(legacy.remove_old_mod(run, lambda *_: None))
        self.assertEqual(calls, [["plugin", "uninstall", "ctempo@claude-tempo", "--scope", "user"],
                                 ["plugin", "marketplace", "remove", "claude-tempo"]])
        calls.clear()
        self.assertFalse(legacy.remove_old_mod(lambda a: calls.append(a) or (1, "", "not found"), lambda *_: None))
        self.assertEqual(len(calls), 1)  # not installed: the marketplace is left alone

    def test_the_old_mod_is_looked_for_in_the_user_settings(self):
        us = os.path.join(self.dir, "settings.json")
        with open(us, "w") as f:
            f.write('{"enabledPlugins": {"other@x": true}}')
        self.assertFalse(legacy.old_mod_listed(us))
        with open(us, "w") as f:
            f.write('{"enabledPlugins": {"ctempo@claude-tempo": true}}')
        self.assertTrue(legacy.old_mod_listed(us))
        self.assertFalse(legacy.old_mod_listed(os.path.join(self.dir, "none.json")))

    def test_swiftbars_old_plugin_is_replaced(self):
        folder = os.path.join(self.dir, "swiftbar")
        os.makedirs(folder)
        old = os.path.join(folder, "ctempo.1m.sh")
        with open(old, "w") as f:
            f.write("#!/bin/sh\n# ctempo menu bar plugin (written by `ctempo menubar --install`)\n")
        install.save_state({"menubar": old})
        with mock.patch("sys.stdout"):
            self.assertTrue(legacy.move_menubar(lambda *_: None))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(os.path.join(folder, menubar.PLUGIN)))
        self.assertEqual(install.load_state()["menubar"], os.path.join(folder, menubar.PLUGIN))

    def test_bin_ctempo_still_runs_pacer(self):
        p = subprocess.run([sys.executable, "-I", "-S", os.path.join(ROOT, "bin", "ctempo"), "--version"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.stdout.strip().split()[0], "pacer", p.stderr)


if __name__ == "__main__":
    unittest.main()
