# ctempo (claude-tempo)

A local pace monitor for Claude's two usage limits: the rolling 5-hour window
and the 7-day weekly window, shared by claude.ai, the Desktop app and Claude
Code. It sits in your main working view (the Claude Code status line in the
terminal, a band above the prompt in the Desktop Code tab) and answers, for
both windows: how fast am I spending, how fast can I afford to, and where will
I land at the reset.

```
5h ━━━━╍╍╍╍ 55%  resets 16:30 · Week ━━━━╍╍╍╍ 48%  resets Fri
```

No network, no token: it reads the rate limits Claude Code already hands to the
status line, keeps a history in SQLite, and learns your hour-of-week rhythm.
Python 3.8+ standard library only; macOS, Linux and Windows.

## Reading the band

One row per window: its name, a small bar, the percent, and words only when
there is something to say.

- **The bar.** Solid orange is what you have used. After a thin gap, a pale
  segment runs to where you will land at the reset; it fades out while the
  estimate is still rough (the first minutes of work, or an early week). A
  red segment into a short red wall means you will run out before the reset.
  In a window's last stretch (its last hour, or the last 2 days of the week),
  green dots fill quota that will go unused, when that is at least 10 points.
  A solid red bar means the limit is reached, a grey fill that nothing is
  moving.
- **The percent.** The last reading, rounded down, as Claude Code's own
  usage bar shows it. When one window needs you, the other one's percent
  turns grey. After a 5-hour window ends, the next one starts only with your
  next message: until then the band reads `5h 0% Not started`.
- **The words.** A window on pace says nothing. With room to spare it says
  by how much in grey: `Can go 2.7x faster` (`about 2x` while the estimate is
  rough). A window that needs you says one thing in color: `Runs out 15:40,
  go 2x slower` (red; `May run out` in yellow when it is close), `Go 1.4x
  slower` (above a target below 100), `40% will go unused` (green), `Back at
  16:30` when the limit is reached, `Over your 80% target`, or `Idle`. While
  the week is at its limit, the 5-hour bar turns grey: nothing more can be
  spent there either.
- **Resets** follow when the band is wide. In a narrower window they move to
  Details and the words get shorter (`2.7x faster`, `Out 15:40`); with the
  smallest bars only one window keeps its grey words.
- **Details** (the button, or `d` when the band has the focus) adds one
  sentence per window, such as "5-hour: 70% used, resets 16:30. Runs out at
  15:40, 50 min before the reset. Go 2x slower.", and how old the last
  reading is.

The terminal status line says the same in text: `━` used, `╍` forecast, `┈`
unused (`#`, `=`, `.` with `display.symbols: "ascii"`), in color unless
`NO_COLOR` is set. When it does not fit `display.max_width`, the resets go
first, then the words get shorter, then the grey words go, then the bars
shrink.

A reading is only as new as the last answer Claude got from the API in some
chat on this machine, so the band can trail Claude Code's own usage bar by a
point for a moment. The Desktop band checks every 5 seconds for a new reading,
from its own chat or another one, and redraws at once (D-31). Work elsewhere
(claude.ai chats, Cowork, cloud sessions, another computer) reaches it only
through `/usage`, below.

### Usage from everywhere

`ctempo fetch --every 10` lets ctempo ask Claude Code itself: at most every
10 minutes the macOS menu bar, or a Desktop band on any system, runs
`claude -p "/usage"` in the background (about 6 s) and records the account's
levels it prints, as readings from a session of its own. ctempo never sees a
credential: Claude Code makes the request with its own login. `ctempo fetch`
runs it once now; `ctempo fetch --every 0` turns it off (the default);
`ctempo doctor` says when it last answered, or why not. It needs Python 3.9
or later (time zones) and the `claude` command (on Windows in
`%USERPROFILE%\.local\bin` or npm's folder).

## The menu bar (macOS)

The band shows only in Claude Code (the Desktop Code tab, the terminal). To
see the pace anywhere, in a regular chat or another app, ctempo can put it in
the macOS menu bar through [SwiftBar](https://swiftbar.app), a free menu bar
app:

```sh
brew install --cask swiftbar      # or download it from swiftbar.app
open -a SwiftBar                  # once: it asks for a plugin folder
ctempo menubar --install          # writes ctempo.1m.sh into that folder
```

The menu bar then shows two small bars, the 5 hours on top and the week below,
with `22% · 18%` beside them, in red, yellow or green when a window needs you
(`70% Out 15:40 · 48%`). A click shows each window's bar as the Desktop band
draws it, its words and reset, and how old the readings are; the full sentence
is the bar's tooltip. `"menubar": {"style": "text"}` brings back plain text. It refreshes every minute and
records nothing itself: the readings come from Claude Code sessions on this
Mac (the status line and the Desktop mod), so while none runs the numbers
grow old, the title turns grey after 30 minutes and the menu says so.
`ctempo menubar --uninstall` (or `ctempo uninstall --apply`) removes it.

To keep the numbers fresh while you work in chats, Cowork or cloud sessions,
turn on [Usage from everywhere](#usage-from-everywhere) (`ctempo fetch --every
10`; `ctempo menubar --fetch 10` does the same). The menu then also says when
`/usage` last answered.

The menu bar also posts a macOS notification, once per window, when a limit
will run out before its reset, when it is reached, or when quota will go
unused at the end of a window ("5-hour limit runs out at 15:40"), and only
from readings under 30 minutes old. `"menubar": {"notify": false}` in
`config.json` turns them off.

## The classic line

`display.style: "classic"` brings back the denser line of earlier versions:

```
5h 62% ×1.4▲ →88% @13:20 · wk 48% +3 ×1.1 →95% @Thu 09:00
```

| Part | Meaning |
|---|---|
| `5h 62%` | 5-hour window: 62% used |
| `×1.4▲` | multiplier: you can go 1.4 times faster and still land on the target at the reset. `▲` speed up (≥ 1.2), no glyph on pace, `▼` slow down (< 0.9) |
| `~×1.4` | same, but the rate leans on interpolation (fewer than 3 fresh readings in the last 20 minutes) |
| `→88%` | projected level at the reset at the current burn rate |
| `dry 12:40` | instead of the projection: you hit 100% at 12:40, before the reset |
| `idle ≤9%/h` | nothing spent in the last 20 minutes; you can afford up to 9 points an hour |
| `≤24%/h` | no rate yet (a window's first minutes, or the first 5 minutes after work starts); you can afford up to 24 points an hour; the burn is unknown, so this part is never green and `check` says no go |
| `@13:20` | reset time |
| `wk 48%` | weekly window: 48% used |
| `+3` | points versus your on-pace level (plus: ahead of your usual rhythm, minus: surplus) |
| `×1.1` | weekly multiplier (same reading as above; no glyph: on pace) |
| `→95%` | projected weekly level at the reset; 100 minus this will expire |
| `dry~Wed 14:00` | the week runs out on Wednesday around 14:00 |
| `@Thu 09:00` | weekly reset |
| `5h 0%` / `wk 0%` | no open window (the next message starts one) |
| `5h 12% @15:00` (level and reset only) | the database could not be written when a new window opened (locked, read-only or full); this is the payload's level, without pace math, until a write succeeds |

Colors: dim when fine; **yellow** when a window runs dry before its reset but
the multiplier is still on pace (0.9 to 1, no glyph), or when you are at or
above a target set below 100 (idle included); **red** when a window runs dry
before its reset and the verdict is slow down (`▼`), or it is maxed; **green**
when a window is in its last stretch (1 hour for 5h, 2 days for the week) with
at least 10 points about to expire below its target (the reserve that a target
below 100 keeps never counts; never while the 5h burn rate is not known yet).
`NO_COLOR` turns colors off; the ▲/▼ glyphs keep the verdict.

`ctempo now` prints the line with the reasoning behind every number.

## The math

**5-hour window**

- current rate = least-squares slope, over the trailing 20 minutes, of the
  level path drawn through fresh readings only (0 at the window start, linear
  between readings, one whole-percent step past the last one at its slope,
  then flat): steady work keeps its rate between steps, a burst that ended
  fades out, idle is 0, and spend in a gap you did not observe (claude.ai
  while Claude Code sat idle) is spread over the gap rather than shown as a
  sudden burst;
- allowed rate = max(0, target − used) / hours to reset (target defaults to
  100);
- multiplier = allowed / current;
- projection = used + rate × hours left, capped at 100, with the time it hits
  100 if that comes first.

**Weekly window**

- profile `w(h)`: 168 hour-of-week bins, the expected share of a week's spend
  in each hour. Prior from your transcripts (last 45 days, cost-weighted
  tokens), updated from the observed weekly-% spend of each hour with a 14-day
  half-life, off-hours floored at 0.15 of a typical awake hour. The prior
  itself fades toward uniform with the same half-life from the day it was
  built, so a schedule that changed since then takes over. The hours from a
  week's last reading to its reset are learned as idle, at low confidence,
  unless that week was maxed (by any reading, a new session's first one
  included: blocked hours say nothing about your habits);
- `E` = Σ w(h) from now to the reset: the share of a week's spend still ahead;
- on-pace level = target × (1 − E); after an early reset that keeps the same
  reset time, it is paced from that reset instead: target × the share of the
  span from it to the reset that has passed; delta = used − on-pace;
- intensity = what you spent over your last 24 awake hours / what the profile
  expected for those hours (at target pace). Spend in a long gap you did not
  observe is spread over the gap by the profile, so only its expected part
  counts. When the 24 hours reach back into the previous week, its hours after
  it hit 100% (blocked until the reset) are left out of the expected part; its
  other hours after its last reading count as no spend, since a last day spent
  on claude.ai only and an idle one leave the same readings. Right after
  install the count starts at the first timed reading, and until the level
  moves the whole window so far stands in (the typical week while the window
  is still at 0, or within the first 2% of a week's expected spend after an
  early reset);
- projection = used + intensity × target × E; "dry" is where an hour-by-hour
  walk at that intensity crosses 100%;
- multiplier = max(0, target − used) / (intensity × target × E): 0 once you
  are past a target below 100, as for the 5-hour window.

Readings from idle sessions, the 5-minute wobble of `resets_at`, resets that
move later, the `used_percentage` epoch leak (#52326), resets that keep the
same `resets_at`, and DST weeks are all handled; DECISIONS.md explains each
rule.

## Install (macOS and Linux)

```sh
git clone <this repo> ~/Projects/claude-tempo && cd ~/Projects/claude-tempo
python3 bin/ctempo install                          # dry run: shows the diff, changes nothing
python3 bin/ctempo install --apply --bin-dir ~/.local/bin
```

`install --apply`:

1. picks and verifies an absolute interpreter (`/usr/bin/python3` first, then
   Homebrew, then the one you ran it with; `--python /abs/path` to choose;
   Windows: see below) and pins it in the command, run as `python -I -S` so
   conda or PYTHONPATH cannot interfere;
2. backs up `~/.claude/settings.json` to
   `settings.json.ctempo-backup-<UTC time>` and sets only `statusLine`
   (`refreshInterval: 60`). If you already have a status line, ctempo
   **composes**: your command still runs, with the same input, and ctempo's
   segment is appended to its last line, or put on a line of its own when the
   joined line would not fit Claude Code's footer (`--replace` to replace it
   instead); a background job your command starts keeps running, and a command
   still running after `compose.timeout_ms` is stopped;
3. installs the Desktop mod from the local marketplace in this repo:
   `claude plugin marketplace add <repo>` then
   `claude plugin install ctempo@claude-tempo` (`--no-mod` to skip). If
   `claude` is not on PATH, it prints the two commands for you to run, and
   uninstall still undoes them;
4. writes `~/.claude-tempo/runtime.json` (the mod reads it: the interpreter,
   its flags and the entry point), a default `config.json`, and a `ctempo`
   launcher in `--bin-dir` (a later install repins it without `--bin-dir`,
   after `--python` or a moved clone too; a new `--bin-dir` moves it there);
5. builds the profile prior from `~/.claude/projects` transcripts
   (`--no-bootstrap` to skip).

The terminal status line applies at once. For the mod, restart Claude Code or
run `/reload-plugins` (in Desktop: start a new session). Then `ctempo doctor`.

## Install on Windows

You need Python 3.8+, and 3.12+ renders about 30 ms faster on Windows
(Performance): from python.org, `winget install Python.Python.3.12`, or the
Python install manager. Git for Windows is optional (`git clone` needs it,
or download the repo as a ZIP): Claude Code runs the status line in Git Bash
when it finds one, else in PowerShell, and install prints which. In
PowerShell, each command on its own line (Windows PowerShell 5.1 has no `&&`):

```powershell
git clone <this repo> $HOME\Projects\claude-tempo
cd $HOME\Projects\claude-tempo
py -3 bin\ctempo install                                    # dry run
py -3 bin\ctempo install --apply --bin-dir $HOME\.local\bin
```

From the clone, `py -3 ctempo ...` and `py -3 -m ctempo ...` work the same as
`py -3 bin\ctempo ...`; after install, the `ctempo` launcher works from anywhere.

In cmd.exe the same lines work with `%USERPROFILE%` for `$HOME` (and `cd /d`),
each such path in double quotes (`"%USERPROFILE%\.local\bin"`), since a profile
folder may hold a space, and without the comment. `~` in this README is
`%USERPROFILE%` on Windows: settings in `%USERPROFILE%\.claude\settings.json`,
data in `%USERPROFILE%\.claude-tempo`. `install --apply` takes the steps above,
with these differences:

- **Python.** `python3` is often only the Microsoft Store stub ("Python was
  not found"), so start install with `py -3`. It pins, in this order, the
  first interpreter that passes: the registry (PEP 514, newest first),
  `py -0p`, the default install folders, the one you ran it with, then
  `python` and `python3` on PATH. Behind a Store alias or `py.exe` it pins the
  real `python.exe`, and for a venv's `python.exe` the base one it starts
  (ctempo runs `-I -S`, so a venv adds only a process); install's list says
  `via <path>` for these. A Store Python is used only when nothing else
  passes. `--python C:\path\to\python.exe` chooses.
- **The shell.** Claude Code looks for Git Bash in `CLAUDE_CODE_GIT_BASH_PATH`,
  `C:\Program Files\Git`, `C:\Program Files (x86)\Git` and beside the `git`
  on PATH; without one it uses PowerShell (pwsh 7, else Windows PowerShell
  5.1). install repeats that search ("Claude Code runs it with Git Bash
  here"), and your previous status line runs in the shell that runs ctempo's.
- **The command.** Forward slashes and `-X utf8` (a pipe gets the ANSI code
  page, and `-I` ignores `PYTHONUTF8`), in a form Git Bash and PowerShell read
  the same, so it keeps working when the shell changes:
  `C:/Users/ana/AppData/Local/Programs/Python/Python312/python.exe -I -S -X utf8 C:/Users/ana/Projects/claude-tempo/bin/ctempo statusline`.
  A path with a space is written with its folders' 8.3 short names
  (`C:/PROGRA~1/PYTHON~1/python.exe`); on a drive without them the command is
  quoted for the shell Claude Code uses now, and install warns that it runs
  only there. A previous status line that a shell would hand to Python
  changed (one with a `"`, which Windows PowerShell 5.1 alters, or one that
  starts with a POSIX path such as `/usr/bin/env`, which Git Bash rewrites) is
  stored as `--compose-b64 b<base64>` instead of `--compose '<command>'`.
  install says so, and `ctempo doctor` shows it decoded; to change it by hand,
  put `--compose '<command>'` in its place, then run `ctempo install --apply`,
  which encodes it again when a shell would change it (until then
  `ctempo doctor` warns).
- **Launchers.** `--bin-dir` writes `ctempo.cmd`, for PowerShell and cmd.exe,
  and an extensionless `ctempo`, for Git Bash and Claude Code's Bash tool
  (which runs in Git Bash). A later install repins each one you keep, and
  one you delete stays deleted until you pass `--bin-dir` again. The status
  line and the mod run `python.exe` itself, never a launcher. If `ctempo` is
  not found, add the folder to your user PATH, then open a new terminal and
  restart Claude Code:

  ```powershell
  $p = [Environment]::GetEnvironmentVariable('Path', 'User')
  [Environment]::SetEnvironmentVariable('Path', "$p;$HOME\.local\bin", 'User')
  ```

- **Quoting.** PowerShell reads a comma as a list: write
  `ctempo check --tail '5h=60m,week=2d'`.

After you install or remove Git for Windows, or set
`CLAUDE_CODE_GIT_BASH_PATH`, run `ctempo doctor`. If it warns about the
statusLine shell or your previous status line, run `ctempo install --apply`
again. A command that install said reads the same in Git Bash and PowerShell
needs nothing.

### Windows notes

- Only a command quoted for a single shell (above) depends on which shell
  Claude Code picks; `ctempo doctor` checks it.
- Claude Code started from a Git Bash terminal can pick another shell than
  when started from the Desktop app or PowerShell (Git Bash puts its own `git`
  first on PATH). install and doctor say so when run there; run
  `ctempo doctor` from PowerShell to check.
- Your previous status line runs in a nested Git Bash or PowerShell, which
  can take about 1 s to start cold, so `compose.timeout_ms` is 3000 on Windows.
  If your part of the line still drops out (a slow script), set it to 5000 in
  `config.json`.
- Claude Code inside WSL is a separate Linux install: follow the macOS and
  Linux steps there. The Desktop app draws no band in WSL sessions, where
  plugins are not available.
- `CTEMPO_HOME`: `~` is `%USERPROFILE%` (never `HOME`); `~\x`, `C:\x`, `C:/x`
  and Git Bash's `/c/x` work. `%USERPROFILE%` in settings.json `env` is not
  expanded (it names a folder called `%USERPROFILE%`). Any other path that
  starts with a single `/` (`/srv/ctempo`) is refused, since Git Bash would
  rewrite it to a folder under its own install (the message names the same
  path on your home's drive, where an older version kept it), and so is `C:x`
  (no slash after the colon, so relative to that drive's current folder).
  Keep it on a local drive: the band usually cannot read a `\\server\share`
  folder.
- `config.json` may be UTF-8 or UTF-16 (what Windows PowerShell 5.1's `>`
  writes); `ctempo config show` says when the file is not used.

## Uninstall

```sh
ctempo uninstall            # dry run
ctempo uninstall --apply
```

It uninstalls the mod and the marketplace, puts your previous `statusLine` back
(or the command inside ctempo's `--compose` if you put or changed one there),
or removes ctempo's, removes the launcher (on Windows both) and `runtime.json`.
`--no-mod` keeps the mod, and `runtime.json` and the launchers with it, until
an uninstall without it. If `~/.claude-tempo/install.json` is gone (you deleted
the folder, or use another `CTEMPO_HOME`) while your settings still list the
mod, uninstall says so and removes it all the same. A launcher made before the
folder was deleted stays: remove it yourself. If you did not change
`settings.json` in between, the file comes back byte for byte (it is restored
from the backup). If `claude` is not on PATH, uninstall prints the two plugin
commands and exits 1; once your settings list neither the plugin nor its
marketplace (you ran them yourself), it completes. Your history stays in
`~/.claude-tempo/`; delete that folder yourself if you want it gone.

## Commands

| Command | What it does |
|---|---|
| `ctempo statusline` | reads the status-line JSON on stdin, records a sample, prints the line (`--format json` for the mod) |
| `ctempo now` | the line plus the reasoning: rates, allowed rates, on-pace level, intensity, projections, exchange rate, and the age of the last reading (heartbeats shown apart) |
| `ctempo report [--days N]` | waste ledger: % unused at each closed weekly and 5-hour window, totals, in 5h-window equivalents |
| `ctempo profile [--rebuild]` | hour-of-week heatmap, the prior's age and weight now, lightest awake blocks, points vs pace by day before reset |
| `ctempo check [--min-free X] [--tail 5h=60m,week=2d] [--max-age 30m]` | for a future scavenger: exit 0 and JSON when there is quota to harvest now, else exit 1; data older than `--max-age` is a no-go, and heartbeats do not count as data; so is a 5h window whose burn rate is not known yet (its leftover is unknown); spare quota is measured against each window's target (`spare` in the JSON), so a window at or above a target below 100, or one that should slow down, is never harvestable; `--min-free` is clamped to 0 to 100 like `min_free`, and a leftover counts only when it is at least that and more than 0 |
| `ctempo bootstrap [--days N] [--dry-run]` | rebuild the profile prior from transcripts |
| `ctempo install` / `uninstall` | see above; dry run unless `--apply` |
| `ctempo doctor` | interpreter, database, settings wiring, mod, last samples, last line and the raw rate limits it came from, rate-limit kinds seen; on Windows, the shell Claude Code runs the status line in and whether the command suits it |
| `ctempo fetch [--every MIN]` | ask Claude Code's `/usage` for the account's levels now, or from now on every MIN minutes (0: never) |
| `ctempo menubar [--install|--uninstall]` | the band in the macOS menu bar (SwiftBar) |
| `ctempo config [show|init|path]` | effective configuration |

`ctempo` with no command is `ctempo now`.

## Configuration

`~/.claude-tempo/config.json` (written with defaults by the first install, and
kept as it is after that: a key you leave out takes the default of the version
you run, one in the file keeps its value, so delete a key to get a newer
default):

| Key | Default | Meaning |
|---|---|---|
| `target_5h`, `target_week` | 100 | level to pace against; 90 keeps a 10-point reserve, which green and `check` never count as spare |
| `rate_window_min` | 20 | 5h least-squares window, minutes |
| `verdict.speed_up`, `verdict.slow_down` | 1.2, 0.9 | multiplier thresholds |
| `tail.five_hour_min`, `tail.seven_day_hours` | 60, 48 | "last stretch" of each window |
| `min_free` | 10 | points about to expire below the target that count as worth using (green, `check`); 0 to 100, and 0 still needs more than 0 points |
| `profile.half_life_days` | 14 | how fast the profile forgets |
| `profile.floor` | 0.15 | off-hour weight relative to a typical awake hour |
| `profile.override` | null | `{"work_days": [0,1,2,3,4], "wake_hours": [8, 23], "off_day_factor": 0.3, "mode": "prior"}`; days are 0 = Monday; mode `replace` ignores learning |
| `reset.drop_points`, `reset.drop_samples` | 15, 2 | a sustained drop that counts as a reset |
| `display.style` | `bars` | the terminal line: `bars`, or `classic` for the dense line of earlier versions |
| `display.symbols` | `unicode` | `ascii` for `# = .` bars (and `x ^ v ->` in the classic line) |
| `display.max_width` | 80 | the line drops optional parts to fit, then is cut with `…` (`...` in ascii mode); each part keeps its color |
| `mod.surfaces` | `["desktop"]` | where the mod draws its band (add `terminal` if you do not use the status line) |
| `mod.refresh_s` | 60 | how often the mod refreshes the band when no reading moved (15 to 3600) |
| `compose.timeout_ms` | 1500 (3000 on Windows) | how long your previous status line may take; once it exits, ctempo reads its output for up to 500 ms more, and only a command still running at the timeout is stopped; on Windows, 5000 for a slow PowerShell or Git Bash script |
| `fetch_usage_min` | 0 | every this many minutes the menu bar or a Desktop band runs `claude -p "/usage"` for the account's levels; 0 never (`ctempo fetch --every N`) |
| `menubar.notify`, `menubar.style` | true, `bars` | macOS notifications; `text` for the plain text menu |
| `retention_days` | 365 | history kept |

`CTEMPO_HOME` moves everything (default `~/.claude-tempo`). The Desktop app
does not inherit your shell's environment, so if you move it, set it in
`~/.claude/settings.json` under `env` too. Claude Code passes that value as
written, so ctempo expands a leading `~` itself and takes a relative path from
your home directory, never from the session's folder. The mod does not expand
`~otheruser/...`: use an absolute path for that. On Windows the home directory
is `%USERPROFILE%`, and some forms are refused: see Windows notes. Values of
the wrong type or out of range fall back to the default or are clamped; a
`config.json` that is not valid JSON is ignored as a whole (`ctempo config
show` says so).

## Data and privacy

- `~/.claude-tempo/tempo.db` (SQLite, WAL): `samples` (time, window, used %,
  resets_at, source, session id), `windows` (one row per window: start, reset,
  peak, % unused at close), the profile, counters. Kept 365 days.
- The status-line payload carries no message content; only the rate limits and
  the session id are stored.
- `bootstrap` reads transcript lines to get each assistant message's timestamp,
  id and token counts. Message content is parsed with the line and discarded; it
  is never stored, printed or logged (a test checks the database for a planted
  secret).
- No hooks inject anything into Claude's context.
- Errors from the status line go to `~/.claude-tempo/errors.log` (64 KB, one
  rotation), never to the line.

## Performance

`python3 scripts/bench_statusline.py` seeds 45 days of history (38,000 rows)
and times 50 renders the way Claude Code runs them (fresh process, payload on
stdin, a new sample written each time). In the build container: p50 35 ms,
p95 47 ms on Python 3.13; p50 34 ms, p95 44 ms on Python 3.9. CI runs it on
GitHub's runners with Python 3.12: macOS p50 24 ms, p95 27 ms; Windows p50 51
ms, p95 55 ms (with Python 3.8 to 3.11 on Windows, about 30 ms more: D-05).
The budget is 100 ms. It also prints where the time goes: the interpreter's
start, the imports, and each phase of a render.

## Tests

```sh
python3 -m unittest discover -s tests -t .     # 490 tests, Python 3.8 to 3.13
claude plugin validate mods/ctempo
claude plugin test mods/ctempo                 # 65 tests
python3 scripts/ci_integration.py              # end to end, throwaway folders
```

(On Windows, `py -3` for `python3`, and in cmd.exe drop the `#` comments.)

The suite feeds synthetic time series (steady burn, bursts, idle, a reset
mid-series, two parallel sessions, missing windows, bogus `resets_at`, the
epoch leak, a week with nights and weekends, DST) and checks the math against
values computed by hand in the test comments. Install and uninstall run on
throwaway settings files, including the byte-identical round trip (CRLF, mode,
several settings files, a crash mid-install, a lost install record). A
model-based fuzzer runs 400 seeded random sequences of installs and uninstalls
(some with `--no-mod`) on two settings files, with claude working, missing or
hanging, hand edits (the command inside ctempo's `--compose` included), the
plugin commands run by hand and the data folder deleted in between. It checks
after every step that `runtime.json` exists while a mod record does, and at
the end that nothing of ctempo is left, each file holds the user's JSON, and a
file the user never edited comes back byte for byte.

Windows rules run on every OS with the one platform flag (`config.IS_WINDOWS`)
patched; the 12 tests that need Windows itself (cmd.exe, Git Bash, PowerShell,
taskkill, 8.3 names, the read-only attribute, the C runtime's `TZ`, long paths)
skip elsewhere. CI (`.github/workflows/tests.yml`) runs the suite on Ubuntu,
macOS and Windows, the mod's tests on all 3, and `scripts/ci_integration.py` on
Windows and macOS: install into a throwaway settings.json, a payload piped into
the command it wrote through each shell Claude Code may use (Git Bash, pwsh and
powershell.exe; `/bin/sh` on macOS), composed commands in plain and encoded
form, and uninstall back to the same bytes; a read-only settings.json; and on
Windows the launchers from cmd.exe, PowerShell and Git Bash, a drive-less
`CTEMPO_HOME`, and a venv's `python.exe` pinned as its base interpreter.
`.gitattributes` keeps `bin/ctempo`, `*.py` and `*.sh` LF on a Windows
checkout, where a CR breaks the `#!` line in Git Bash.

The 18 review passes (2 of them model-based fuzzers, for install and for reset
detection, 3 on Windows, the last on the band) found 181 defects; 178 are fixed and 3, which
need 2 Claude Code processes on 1 conversation, are documented limits (open a
conversation in a second terminal with `--fork-session`); each fixed one has a
regression test that fails on the code before its fix (for the 3 in these docs,
a check that the Windows section says what the code does).

## Layout

```
bin/ctempo                 launcher (python -I -S safe)
ctempo/                    the core: store, ingest, pace, weekly, profile, band
                           (what the band and the line say), render,
                           statusline, commands, install, transcripts, clock
                           (local time), winshell (Windows shells and commands)
mods/ctempo/               the Desktop mod (TypeScript, thin)
.claude-plugin/            local marketplace: claude-tempo
tests/                     unittest suite
scripts/bench_statusline.py
scripts/ci_integration.py  end to end through each shell (CI on Windows, macOS)
.github/workflows/         CI: tests on 3 OSes, the mod, the end-to-end runs
notes/                     docs check and reference study
PLAN.md, DECISIONS.md
```

## Credits

ctempo reimplements ideas from these MIT-licensed projects (read, not run or
copied):

- [SeanLF/ccpool](https://github.com/SeanLF/ccpool): least-squares burn on a
  running-max series, the 300 s `resets_at` jitter bucket, the #52326 epoch-leak
  guard, the 0.15 off-hour pace floor with work days and wake hours, the
  dim-until-it-matters statusline, the dry-run/backup install.
- [thevibeworks/claude-code-statusline](https://github.com/thevibeworks/claude-code-statusline):
  the decay-weighted profile with a 14-day half-life, the hour-by-hour walk
  behind "dry ~Thu 09:00" and "~N% will expire", the 0.25 rest-hour threshold,
  and `pct_per_window` (the 5h-to-weekly exchange rate).
- [thevibeworks/ccpace](https://github.com/thevibeworks/ccpace): the sustained
  drop rule for resets (2 consecutive readings, 15 points).
- [fabioconcina/claumon](https://github.com/fabioconcina/claumon): the
  empirical-Bayes forecast with an 80% interval, noted for v2.
- [Astro-Han/claude-pace](https://github.com/Astro-Han/claude-pace): the
  network-free baseline.
- [NathanAB/statusline-anywhere](https://github.com/NathanAB/statusline-anywhere):
  how a mod runs a status-line command and draws it above the prompt.
- [anthropics/claude-code-playground](https://github.com/anthropics/claude-code-playground)
  (Apache-2.0): the sample mods' conventions.
