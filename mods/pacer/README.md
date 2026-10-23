# pacer mod

Draws pacer's band above the prompt, for the Claude Desktop Code tab where a
custom statusLine does not render: one row with a small bar, the percent and,
its pace in a few words per window; Details (`d`) adds a card per window with
a chart and a few numbers. All the math and all the words are in the Python CLI (`../../pacer`);
this mod only:

1. when a reading moved (a `session.measure` whose `rateLimits` changed, or its
   5 s poll seeing new limits in this session or a new `changed` stamp from
   another chat's run) and every 60 s, turns
   `$.session.usage().rateLimits` into the statusLine payload shape and runs
   `python -I -S bin/pacer statusline --source mod --format json` through
   `$.process.run` (the interpreter, its flags and the entry come from
   `runtime.json` in the pacer home, `~/.pacer` or `PACER_HOME`,
   written by `pacer install`: `-I -S -X utf8` on Windows, `-I -S` when the
   file lists none; a `~` or relative `PACER_HOME` is taken from your home
   directory, `HOME`, or `USERPROFILE` on Windows);
2. stores what it prints in `$.state`: one entry per window (bar kind,
   percent, words, reset, sentence) and the text line;
3. draws them on the `AbovePrompt` band for the surfaces listed in the CLI
   config (`mod.surfaces`, default `["desktop"]`) as SVG bars sized to the
   band's width, keeping other mods' rows below its own. The terminal surface,
   or a CLI too old to print windows, gets the text line.

Each call also records a sample, so the Desktop app is a collector too.

On Windows the mod finds your home directory as Python does: `USERPROFILE`,
else `HOMEDRIVE` plus `HOMEPATH`, never `HOME` (Git Bash sets one that Python
ignores). `PACER_HOME` may be `C:\...`, `C:/...`, `~\...`, `~/...` or Git
Bash's `/c/...`, and `\pacer` is on your home's drive. `C:pacer` (relative to
a drive's current folder) is refused, and so is any other path that starts
with a single `/` (`/srv/pacer`): Git Bash, which runs the terminal status
line, would rewrite it to a folder under its own install. The CLI refuses both
with the same messages, and the one for `/srv/pacer` names the same path on
your home's drive, where an older version kept it. Keep the home on a local
drive: the mod usually cannot read a network (`\\server\share`) folder, and
says so. When the pinned `python.exe` is gone, the band says "Python not found
at <path>: run `pacer install --apply`", and the mod reads `runtime.json`
again at its next refresh. The band draws in the Desktop app's local sessions,
not in WSL sessions, where plugins are not available.

Install it with the rest of pacer (`pacer install --apply`), or by hand:

```sh
claude plugin marketplace add /path/to/pacer
claude plugin install pacer@pacer
```

On Windows: `claude plugin marketplace add C:/path/to/pacer`; if claude
rejects the path, run `claude plugin marketplace add ./` from the repo folder.

Then `/reload-plugins` in an open session, or start a new one.

`pacer uninstall --apply` removes it again, including when `pacer install`
only printed these commands because `claude` was not on PATH. It also removes
a mod added without `pacer install` (it finds it in your user settings); or
remove that with `claude plugin uninstall pacer@pacer` and `claude
plugin marketplace remove pacer`.

Develop: `claude plugin validate .` and `claude plugin test .` from this
folder.
