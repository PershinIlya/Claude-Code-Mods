# ctempo mod

Draws ctempo's pace line above the prompt, for the Claude Desktop Code tab
where a custom statusLine does not render. All the math is in the Python CLI
(`../../ctempo`); this mod only:

1. on `session.measure` (when `rateLimits` changed) and every 60 s, turns
   `$.session.usage().rateLimits` into the statusLine payload shape and runs
   `python -I -S bin/ctempo statusline --source mod --format json` through
   `$.process.run` (the interpreter and entry come from `runtime.json` in the
   ctempo home, `~/.claude-tempo` or `CTEMPO_HOME`, written by
   `ctempo install`; a `~` or relative `CTEMPO_HOME` is taken from `HOME`);
2. stores the tone-tagged segments it prints in `$.state`;
3. draws them on the `AbovePrompt` band for the surfaces listed in the CLI
   config (`mod.surfaces`, default `["desktop"]`), keeping other mods' rows
   below its own.

Each call also records a sample, so the Desktop app is a collector too.

Install it with the rest of ctempo (`ctempo install --apply`), or by hand:

```sh
claude plugin marketplace add /path/to/claude-tempo
claude plugin install ctempo@claude-tempo
```

Then `/reload-plugins` in an open session, or start a new one.

`ctempo uninstall --apply` removes it again, including when `ctempo install`
only printed these commands because `claude` was not on PATH. A mod added
without `ctempo install` is removed with
`claude plugin uninstall ctempo@claude-tempo` and
`claude plugin marketplace remove claude-tempo`.

Develop: `claude plugin validate .` and `claude plugin test .` from this
folder.
