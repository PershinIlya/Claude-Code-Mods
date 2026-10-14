# Step 1: ground truth from the official docs (checked 2026-10-14)

Sources: code.claude.com/docs/en/statusline, plugins/mods/{overview,reference,api},
plugins/manifest-reference, plugins/marketplace-reference, plugins/cli-reference,
settings-reference, and the mods type file bundled with Claude Code 2.1.299
(authoritative for this build; the docs say to trust it over any page).

## Environment where ctempo was built

| Item | Value |
|---|---|
| Machine | Cloud container (Claude Code on the web), not the MacBook Air |
| OS | Ubuntu 24.04.5 LTS, Linux 6.18 x86_64 |
| `claude --version` | 2.1.299 (Claude Code) |
| Python | `/usr/bin/python3` 3.13.16 (sqlite 3.45.1), `python3.12`, `python3.11`; 3.8 and 3.9 fetched with `uv` for testing only |
| Node | 22.22.0 (only the `claude` CLI uses it) |

The Mac has macOS system Python 3.9 (Command Line Tools) plus conda. ctempo
supports Python 3.8+ and the test suite passes on 3.8, 3.9, 3.11, 3.12, 3.13.

## statusLine

- stdin JSON includes `rate_limits.five_hour` and `rate_limits.seven_day`, each
  `{used_percentage, resets_at}`. `used_percentage` is 0 to 100 (may be
  fractional). `resets_at` is **Unix epoch seconds**. Also `spend_limit` behind a
  Claude apps gateway (can exceed 100).
- CONFIRMED: `rate_limits` "appears only for claude.ai Pro and Max subscribers,
  or behind a Claude apps gateway ... and only after the first API response in
  the session. Each window ... may be independently absent, and Claude Code drops
  a window once its `resets_at` time passes."
- CONFIRMED by omission: no per-model weekly windows on stdin (they exist in the
  SDK's `seven_day_opus` / `seven_day_sonnet` and in /api/oauth/usage).
- NOT STATED: whether a custom statusLine renders in the Desktop Code tab. The
  statusline page never mentions Desktop; Desktop shows its own usage ring. We
  treat the user's claim (it does not) as true and cover Desktop with the mod.
- Runs at session start, on each new assistant message, after /compact, on
  permission or vim mode change, when `refreshInterval` elapses, and when a
  rate-limit window in the last payload reaches `resets_at`.
- Debounce 300 ms; "If a new update triggers while your script is still running,
  Claude Code cancels the in-flight script." No timeout is documented.
- `refreshInterval`: seconds, minimum 1. Output shown only after exit 0; non-zero
  exit or empty output blanks the line. stdout only; ANSI colors and OSC 8 links
  work; `COLUMNS`/`LINES` env vars give the width.
- Settings shape: `{"statusLine": {"type": "command", "command": "...",
  "padding": n, "refreshInterval": n, "hideVimModeIndicator": bool}}`. Plugins
  cannot ship a `statusLine`.

## Mods (Claude Code 2.1.287+ in the terminal, 2.1.286+ in Desktop)

- A mod is a plugin whose `hooks/hooks.json` names one hooks module
  (`{"modules": ["./register.tsx"]}`) exporting `register(on, options)`.
- Where they draw: terminal and the Desktop Code tab (not WSL). The VS Code panel
  runs hooks but draws nothing; `claude -p` and the SDK run hooks only.
- Band above the prompt: `on('ui.render', {component: 'AbovePrompt'}, ...)`,
  return a tree or `next(e)` to show nothing. Props include `bodyColumns`,
  `hasSurvey`, `isWorking`. Only one mod's band shows unless a mod nests
  `await next(e)` in its tree.
- `$.session.usage()` returns `{startedAt, context, rateLimits, cost}`;
  `rateLimits` is `[{kind, percentUsed, resetsAt?}]`, `resetsAt` an ISO 8601
  string (not epoch). `session.measure` fires after each main-thread turn and when
  a window moves a whole point.
- `kind` values: the docs pages do not list them (user claim CONFIRMED for the
  pages); the bundled types say "`five_hour`, `seven_day`, or a Claude gateway's
  `spend_limit`" and type it as `string`. ctempo logs every kind it sees in the
  `kinds` table (`ctempo doctor` prints them).
- `$.process.run(argv, {cwd, env, stdin, timeoutMs})` runs a host command with no
  shell; 30 s default timeout; no permission prompt (mods are not sandboxed).
  `$.env.get("HOME")`, `$.fs.read/exists`, `$.clock.every/after/now`,
  `$.session.id()`, `$.ui.invalidate('ui.render')`, `$.state` atoms (declared in
  `types/index.d.ts`). Hook budget 10 s, not counting time inside `$` calls.

## Plugins and marketplaces

- `.claude-plugin/marketplace.json` at the marketplace root: `name`, `owner.name`,
  `plugins[{name, source: "./relative/path"}]`. Relative sources resolve from the
  folder that contains `.claude-plugin/`.
- `claude plugin marketplace add <path>` writes `extraKnownMarketplaces` to
  `~/.claude/settings.json`; `claude plugin install name@marketplace` (user scope
  by default) writes `enabledPlugins`. A plugin from a local-folder marketplace is
  read in place, so edits apply on `/reload-plugins`.
- `claude plugin validate <dir>`, `claude plugin test <dir>` (runs `*.test.ts`
  against the engine via `claude-code/testing`).
