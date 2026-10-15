# ctempo mod

Draws ctempo's pace line above the prompt, for the Claude Desktop Code tab
where a custom statusLine does not render. All the math is in the Python CLI
(`../../ctempo`); this mod only:

1. on `session.measure` (when `rateLimits` changed) and every 60 s, turns
   `$.session.usage().rateLimits` into the statusLine payload shape and runs
   `python -I -S bin/ctempo statusline --source mod --format json` through
   `$.process.run` (the interpreter, its flags and the entry come from
   `runtime.json` in the ctempo home, `~/.claude-tempo` or `CTEMPO_HOME`,
   written by `ctempo install`: `-I -S -X utf8` on Windows, `-I -S` when the
   file lists none; a `~` or relative `CTEMPO_HOME` is taken from your home
   directory, `HOME`, or `USERPROFILE` on Windows);
2. stores the tone-tagged segments it prints in `$.state`;
3. draws them on the `AbovePrompt` band for the surfaces listed in the CLI
   config (`mod.surfaces`, default `["desktop"]`), keeping other mods' rows
   below its own.

Each call also records a sample, so the Desktop app is a collector too.

On Windows the mod finds your home directory as Python does: `USERPROFILE`,
else `HOMEDRIVE` plus `HOMEPATH`, never `HOME` (Git Bash sets one that Python
ignores). `CTEMPO_HOME` may be `C:\...`, `C:/...`, `~\...`, `~/...` or Git
Bash's `/c/...`, and `\ctempo` is on your home's drive. `C:ctempo` (relative to
a drive's current folder) is refused, and so is any other path that starts
with a single `/` (`/srv/ctempo`): Git Bash, which runs the terminal status
line, would rewrite it to a folder under its own install. The CLI refuses both
with the same messages, and the one for `/srv/ctempo` names the same path on
your home's drive, where an older version kept it. Keep the home on a local
drive: the mod usually cannot read a network (`\\server\share`) folder, and
says so. When the pinned `python.exe` is gone, the band says "Python not found
at <path>: run `ctempo install --apply`", and the mod reads `runtime.json`
again at its next refresh. The band draws in the Desktop app's local sessions,
not in WSL sessions, where plugins are not available.

Install it with the rest of ctempo (`ctempo install --apply`), or by hand:

```sh
claude plugin marketplace add /path/to/claude-tempo
claude plugin install ctempo@claude-tempo
```

On Windows: `claude plugin marketplace add C:/path/to/claude-tempo`; if claude
rejects the path, run `claude plugin marketplace add ./` from the repo folder.

Then `/reload-plugins` in an open session, or start a new one.

`ctempo uninstall --apply` removes it again, including when `ctempo install`
only printed these commands because `claude` was not on PATH. It also removes
a mod added without `ctempo install` (it finds it in your user settings); or
remove that with `claude plugin uninstall ctempo@claude-tempo` and `claude
plugin marketplace remove claude-tempo`.

Develop: `claude plugin validate .` and `claude plugin test .` from this
folder.
