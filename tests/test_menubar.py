"""The menu bar indicator (ctempo/menubar.py)."""

import contextlib
import io
import os
import subprocess
import sys
from unittest import mock

from ctempo import band, config, install, menubar

from .helpers import MON, H, D, TempHome
from .test_band import NOW, STATES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Lines(TempHome):
    def out(self, state, updated=NOW - 60):
        return menubar.lines(band.windows(*STATES[state], NOW), updated, NOW, self.cfg)

    def test_a_calm_title_is_plain_and_the_menu_has_each_window(self):
        out = self.out("on pace")
        self.assertEqual(out[0], "5h 55% · Wk 48%")
        self.assertEqual(out[1], "---")
        self.assertEqual(out[2], "5h ━━━━╍╍╍╍ 55%  resets 16:30 | font=Menlo size=12 trim=false")
        self.assertTrue(out[3].startswith("5-hour: 55% used, resets 16:30. On pace"), out[3])
        self.assertIn("Updated 1 min ago | size=11 color=%s" % menubar.DIM, out)
        self.assertEqual(out[-1], "Refresh | refresh=true")

    def test_a_window_that_needs_you_colors_the_title(self):
        self.assertEqual(self.out("runs out")[0], "5h 70% Out 15:40 · Wk 48% | color=" + menubar.TONE["error"])
        self.assertEqual(self.out("unused")[0], "5h 12% · Wk 40% 40% unused | color=" + menubar.TONE["success"])
        self.assertEqual(self.out("maxed")[0], "5h 100% Back 16:30 · Wk 52% | color=" + menubar.TONE["error"])

    def test_old_readings_dim_the_title_and_say_where_they_come_from(self):
        out = self.out("on pace", updated=NOW - 2 * H)
        self.assertEqual(out[0], "5h 55% · Wk 48% | color=" + menubar.DIM)
        self.assertIn("Updated 2 h ago | size=11 color=%s" % menubar.DIM, out)
        self.assertTrue(any(line.startswith("Readings come from Claude Code sessions") for line in out))

    def test_waiting(self):
        out = menubar.lines(band.windows({}, {}, NOW), None, NOW, self.cfg)
        self.assertEqual(out[0], "ctempo | color=%s" % menubar.DIM)
        self.assertTrue(out[2].startswith("Waiting for the first reading"))

    def test_no_pipe_reaches_swiftbar_from_the_words(self):
        # SwiftBar reads " | " as the start of a line's parameters.
        for state in STATES:
            for line in self.out(state):
                self.assertLessEqual(line.count(" | "), 1, line)


class Show(TempHome):
    def test_reads_the_store_and_records_nothing(self):
        r5, r7 = int(MON + 4 * H), int(MON + 5 * D)
        self.known("s1")
        for k in range(6):
            self.feed(MON + H + 300 * k, five=(10 + k, r5), week=(30, r7))
        count = self.st.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("time.time", return_value=MON + H + 1600):
            self.assertEqual(menubar.show(self.cfg), 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("5h 15% · Wk 30%"), lines[0])
        self.assertEqual(self.st.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], count)

    def test_no_database_yet_is_waiting(self):
        self.st.close()
        os.remove(os.path.join(self.dir, "tempo.db"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(menubar.show(self.cfg), 0)
        self.assertTrue(out.getvalue().startswith("ctempo | color="))


class Plugin(TempHome):
    def test_install_runs_and_uninstall_removes_it(self):
        folder = os.path.join(self.dir, "plugins")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(menubar.install(folder), 0)
        path = os.path.join(folder, menubar.PLUGIN)
        self.assertTrue(os.access(path, os.X_OK) or os.name == "nt")
        self.assertEqual(install.load_state()["menubar"], path)
        if os.name != "nt":  # SwiftBar is macOS; the script is sh
            env = {k: v for k, v in os.environ.items() if k != "CTEMPO_HOME"}
            p = subprocess.run(["sh", path], capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue(p.stdout.startswith("ctempo") or p.stdout.startswith("5h"), p.stdout)
        with contextlib.redirect_stdout(io.StringIO()):
            menubar.uninstall()
        self.assertFalse(os.path.exists(path))
        self.assertNotIn("menubar", install.load_state())

    def test_never_overwrites_a_plugin_that_is_not_ours(self):
        folder = os.path.join(self.dir, "plugins")
        os.makedirs(folder)
        path = os.path.join(folder, menubar.PLUGIN)
        with open(path, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(menubar.install(folder), 1)
            menubar.uninstall(folder)
        with open(path) as f:
            self.assertEqual(f.read(), "#!/bin/sh\necho mine\n")

    def test_without_a_folder_it_says_how_to_get_one(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(menubar, "plugin_dir", return_value=None):
            self.assertEqual(menubar.install(None), 1)
        self.assertIn("brew install --cask swiftbar", out.getvalue())

    def test_the_cli_command(self):
        p = subprocess.run([sys.executable, "-I", "-S", os.path.join(ROOT, "bin", "ctempo"), "menubar"],
                           capture_output=True, text=True, env=dict(os.environ, CTEMPO_HOME=self.dir), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("\n---\n", p.stdout)


class Notify(TempHome):
    def test_what_is_worth_a_notification(self):
        def titles(state):
            return [t for _, t, _ in menubar.events(band.windows(*STATES[state], NOW))]

        self.assertEqual(titles("runs out"), ["5-hour limit runs out at 15:40"])
        self.assertEqual(titles("maxed"), ["5-hour limit reached, back at 16:30"])
        self.assertEqual(titles("unused"), ["Weekly: 40% will go unused"])
        self.assertEqual(titles("week dry"), ["Weekly limit runs out Wed 11:00"])
        for calm in ("on pace", "early", "idle", "half"):
            self.assertEqual(titles(calm), [], calm)

    def test_once_per_window_and_never_from_old_readings(self):
        ws = band.windows(*STATES["runs out"], NOW)
        sent = []
        send = lambda title, body: sent.append((title, body))  # noqa: E731
        self.assertEqual(menubar.notify(self.st, ws, NOW - 3 * H, NOW, send), [])  # 3 h old: nothing
        menubar.notify(self.st, ws, NOW - 60, NOW, send)
        menubar.notify(self.st, ws, NOW, NOW + 60, send)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][1].startswith("5-hour: 70% used"), sent[0])

    def test_a_lone_waiting_window_reads_waiting(self):
        out = menubar.lines(band.windows(*STATES["half"], NOW), NOW - 60, NOW, self.cfg)
        self.assertIn("Week waiting | font=Menlo size=12 trim=false", out)
