"""winshell: the shell Claude Code runs the statusLine in on Windows, and
commands that read right there. Windows path rules on any OS, so this runs
everywhere; the last class needs a real Windows."""

import ntpath
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from ctempo import config, winshell

GIT_DEFAULT = r"C:\Program Files\Git\bin\bash.exe"
PS51 = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def fs(*paths):
    """An exists/is_file stand-in over these paths, case-insensitive like NTFS."""
    have = {p.lower() for p in paths}
    return lambda p: p.lower() in have


class PredictShell(unittest.TestCase):
    """The search order of Claude Code 2.1.300: CLAUDE_CODE_GIT_BASH_PATH, the
    two Program Files installs, git on PATH, then PowerShell (pwsh first)."""

    def predict(self, env, *files, settings_env=None):
        f = fs(*files)
        return winshell.predict_shell(env, settings_env, f, f)

    def test_git_bash_path_wins_when_it_names_bash_or_sh(self):
        for name in ("bash.exe", "sh.exe", "bash", "SH"):
            given = "D:\\tools\\git\\usr\\bin\\" + name
            self.assertEqual(self.predict({"CLAUDE_CODE_GIT_BASH_PATH": given}, given, GIT_DEFAULT), ("bash", given))
        # Not a bash or sh, or missing: the search goes on.
        for given in (r"D:\tools\zsh.exe", r"D:\missing\bash.exe"):
            self.assertEqual(self.predict({"CLAUDE_CODE_GIT_BASH_PATH": given}, r"D:\tools\zsh.exe", GIT_DEFAULT),
                             ("bash", GIT_DEFAULT))

    def test_settings_env_counts_over_the_process_env(self):
        given = r"D:\git\bin\bash.exe"
        env = {"claude_code_git_bash_path": r"D:\old\bash.exe"}  # names ignore case on Windows
        got = self.predict(env, given, r"D:\old\bash.exe", settings_env={"CLAUDE_CODE_GIT_BASH_PATH": given, "N": 1})
        self.assertEqual(got, ("bash", given))

    def test_program_files_then_x86_then_git_on_path(self):
        x86 = r"C:\Program Files (x86)\Git\bin\bash.exe"
        self.assertEqual(self.predict({}, GIT_DEFAULT, x86), ("bash", GIT_DEFAULT))
        self.assertEqual(self.predict({}, x86), ("bash", x86))
        env = {"Path": 'C:\\Windows\\System32;;"D:\\Apps\\Git\\cmd"', "PATHEXT": ".COM;.EXE"}
        self.assertEqual(self.predict(env, r"D:\Apps\Git\cmd\git.exe", r"D:\Apps\Git\bin\bash.exe"),
                         ("bash", r"D:\Apps\Git\bin\bash.exe"))
        # git without a bash two folders up: PowerShell.
        self.assertEqual(self.predict(env, r"D:\Apps\Git\cmd\git.exe", PS51), ("powershell", PS51))

    def test_git_in_the_current_directory_is_skipped(self):
        here = os.getcwd()
        env = {"PATH": here + ";D:\\Git\\cmd"}
        self.assertEqual(winshell.which("git", env, fs(ntpath.join(here, "git.EXE"))), None)
        self.assertEqual(winshell.which("git", env, fs(ntpath.join(here, "git.EXE"), r"D:\Git\cmd\git.exe")),
                         r"D:\Git\cmd\git.EXE")
        self.assertEqual(winshell.which("git", {"PATH": "D:\\a;D:\\b", "PATHEXT": ".CMD;.EXE"},
                                        fs(r"D:\a\git.exe", r"D:\b\git.cmd")), r"D:\a\git.EXE")

    def test_powershell_order(self):
        env = {"PATH": r"C:\bin;C:\Windows\System32\WindowsPowerShell\v1.0", "ProgramFiles": r"C:\Program Files",
               "LOCALAPPDATA": r"C:\Users\me\AppData\Local", "USERPROFILE": r"C:\Users\me", "SystemRoot": r"C:\Windows"}
        on_path = r"C:\bin\pwsh.exe"
        pf = r"C:\Program Files\PowerShell\7\pwsh.exe"
        apps = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\pwsh.exe"
        dotnet = r"C:\Users\me\.dotnet\tools\pwsh.exe"
        self.assertEqual(self.predict(env, on_path, pf, PS51), ("powershell", r"C:\bin\pwsh.EXE"))  # PATHEXT's case
        self.assertEqual(self.predict(env, pf, apps, PS51), ("powershell", pf))
        self.assertEqual(self.predict(env, apps, dotnet, PS51), ("powershell", apps))
        self.assertEqual(self.predict(env, dotnet, PS51), ("powershell", dotnet))
        self.assertEqual(self.predict(env, PS51), ("powershell", PS51[:-3] + "EXE"))  # found on PATH
        self.assertEqual(self.predict({"SystemRoot": r"C:\Windows"}, PS51), ("powershell", PS51))
        self.assertEqual(self.predict({}), ("powershell", None))


class ShellArgv(unittest.TestCase):
    """The Composer runs the old command as Claude Code would: never cmd.exe."""

    def argv(self, cmd, env, kind, path):
        with mock.patch.object(winshell, "predict_shell", return_value=(kind, path)):
            return winshell.shell_argv(cmd, env)

    def test_bash_puts_its_folder_first_on_path(self):
        argv, env = self.argv("printf x", {"Path": r"C:\Windows", "A": "1"}, "bash", GIT_DEFAULT)
        self.assertEqual(argv, [GIT_DEFAULT, "-c", "printf x"])
        self.assertEqual(env, {"Path": r"C:\Program Files\Git\bin;C:\Windows", "A": "1"})
        argv, env = self.argv("x", {}, "bash", "bash")  # a relative path is left off PATH, as Claude Code does
        self.assertEqual((argv, env), (["bash", "-c", "x"], {}))

    def test_bash_runs_a_script_first_word_with_bash(self):
        for cmd in ("~/sl.sh --x", "'C:/my dir/sl.sh'", "C:/my\\ dir/sl.sh a"):
            self.assertEqual(self.argv(cmd, {}, "bash", GIT_DEFAULT)[0][2], "bash " + cmd)
        self.assertEqual(self.argv("sl.sh.exe", {}, "bash", GIT_DEFAULT)[0][2], "sl.sh.exe")

    def test_powershell_flags_and_placeholders(self):
        argv, _ = self.argv("node ${CLAUDE_PROJECT_DIR}/x.js ${env:CLAUDE_PLUGIN_ROOT}", {}, "powershell", PS51)
        self.assertEqual(argv, [PS51, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                                "node ${env:CLAUDE_PROJECT_DIR}/x.js ${env:CLAUDE_PLUGIN_ROOT}"])
        argv, _ = self.argv("x", {"CLAUDE_CODE_POWERSHELL_RESPECT_EXECUTION_POLICY": "1"}, "powershell", None)
        self.assertEqual(argv, ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "x"])

    def test_first_word_reads_quotes_and_escapes_as_claude_code(self):
        self.assertEqual(winshell.first_word('  "C:/my dir/x.sh" a'), "C:/my dir/x.sh")
        self.assertEqual(winshell.first_word("a\\ b.sh c"), "a b.sh")
        self.assertEqual(winshell.first_word("'open"), "open")


class Quoting(unittest.TestCase):
    PY = "C:/Users/me/AppData/Local/Programs/Python/Python312/python.exe"
    ENTRY = "C:/Users/me/Claude-Code-Mods/bin/ctempo"
    TRICKY = ["", " ", "a b", "it's", "\u2019x\u2018", 'say "hi"', "$HOME `x` ${y} %PATH% !", "a\\b\\", "tab\there",
              "semi;colon&amp|pipe", "~/x", "@a,b", "C:/Program Files/x.exe", "J\u00fcrgen", "en\u2013dash", "''"]

    def test_bare_paths_read_the_same_in_both_shells(self):
        argv = [self.PY, "-I", "-S", "-X", "utf8", self.ENTRY, "statusline"]
        cmd = winshell.command_line(argv, "bash")
        self.assertEqual(cmd, " ".join(argv))
        self.assertEqual(winshell.command_line(argv, "powershell"), cmd)
        self.assertTrue(winshell.reads_alike(cmd))
        self.assertIsNone(winshell.written_for(cmd))
        # Non-ASCII letters need no quotes either.
        self.assertTrue(winshell.reads_alike(winshell.command_line(["C:/Users/J\u00fcrgen/py.exe", "-I"], "bash")))

    def test_a_quoted_first_word_needs_the_call_operator_in_powershell(self):
        argv = ["C:/Program Files/Python312/python.exe", "-I", "-S", "-X", "utf8", self.ENTRY, "statusline"]
        b, p = winshell.command_line(argv, "bash"), winshell.command_line(argv, "powershell")
        self.assertEqual(b, "'C:/Program Files/Python312/python.exe' -I -S -X utf8 %s statusline" % self.ENTRY)
        self.assertEqual(p, "& " + b)
        self.assertEqual((winshell.written_for(b), winshell.written_for(p)), ("bash", "powershell"))
        self.assertIsNone(winshell.words(b, "powershell"))  # a string, not a command, in PowerShell
        self.assertIsNone(winshell.words(p, "bash"))

    def test_compose_argument_reads_alike_without_a_single_quote(self):
        argv = [self.PY, self.ENTRY, "statusline", "--compose", 'powershell -NoProfile -File "C:/x y/sl.ps1"']
        cmd = winshell.command_line(argv, "bash")
        self.assertEqual(cmd, winshell.command_line(argv, "powershell"))
        self.assertTrue(winshell.reads_alike(cmd))
        argv[-1] = "echo it's"
        b, p = winshell.command_line(argv, "bash"), winshell.command_line(argv, "powershell")
        self.assertTrue(b.endswith("""'echo it'"'"'s'"""), b)
        self.assertTrue(p.endswith("'echo it''s'"), p)
        self.assertEqual(winshell.written_for(b), "bash")
        self.assertIsNone(winshell.written_for(p))  # bash reads other words there: only the record can tell

    def test_every_word_comes_back(self):
        for shell in ("bash", "powershell"):
            for w in self.TRICKY:
                for argv in ([self.PY, w], [w, "-I"], [self.PY, w, w + w]):
                    cmd = winshell.command_line(argv, shell)
                    self.assertEqual(winshell.words(cmd, shell), argv, (shell, cmd))
        self.assertEqual(winshell.quote("\u2019x", "powershell"), "'\u2019\u2019x'")
        self.assertEqual(winshell.command_line([".\\x", "a"], "powershell"), "& '.\\x' a")
        self.assertEqual(winshell.command_line(["-x", "a"], "powershell"), "& -x a")

    def test_bare_words(self):
        for w in ("C:/x/y.exe", "-I", "utf8", "--compose", "a+b=c:d_e.f", "\u00fc\u4e2d"):
            self.assertTrue(winshell.is_bare(w), w)
        for w in ("", "a b", "C:\\x", "~", "$x", "a,b", "@a", "%x%", "\u2013", "\u2019", "\u00a0", "a;b", "{x}"):
            self.assertFalse(winshell.is_bare(w), w)
        self.assertEqual(winshell.slashes(r"C:\Users\me\x"), "C:/Users/me/x")

    def test_what_powershell_51_passes_intact(self):
        self.assertTrue(winshell.passes_intact("printf 'a b' C:\\x"))
        self.assertTrue(winshell.passes_intact("C:\\x\\"))  # no whitespace: not wrapped in quotes
        self.assertFalse(winshell.passes_intact('echo "hi"'))
        self.assertFalse(winshell.passes_intact("dir C:\\"))

    def test_outside_the_simple_form(self):
        for cmd in ("echo $(date)", "a | b", "'open", "x 'a'b", "x \"$y\"", "&x"):
            self.assertIsNone(winshell.words(cmd, "powershell"), cmd)
        for cmd in ("echo $(date)", "'open", "& x", "x \"a$b\""):
            self.assertIsNone(winshell.words(cmd, "bash"), cmd)
        self.assertFalse(winshell.reads_alike(""))

    def test_python_flags(self):
        self.assertEqual(winshell.python_flags(False), ["-I", "-S"])
        self.assertEqual(winshell.python_flags(True), ["-I", "-S", "-X", "utf8"])
        with mock.patch.object(config, "IS_WINDOWS", True):
            self.assertEqual(winshell.python_flags(), ["-I", "-S", "-X", "utf8"])


class KillTree(unittest.TestCase):
    def test_no_taskkill_is_a_false(self):
        self.assertFalse(winshell.kill_tree(2 ** 22, {"SYSTEMROOT": os.path.join(os.sep, "nowhere")}))

    @unittest.skipUnless(os.name == "nt", "needs Windows")
    def test_kills_a_process_and_its_children(self):
        code = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); time.sleep(30)"
        p = subprocess.Popen([sys.executable, "-c", code])
        time.sleep(1.0)
        self.assertTrue(winshell.kill_tree(p.pid))
        self.assertIsNotNone(p.wait(10))


if __name__ == "__main__":
    unittest.main()
