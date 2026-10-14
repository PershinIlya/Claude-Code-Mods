# ctempo (claude-tempo)

A local pace monitor for Claude's two usage limits: the rolling 5-hour window
and the 7-day weekly window, shared by claude.ai, the Desktop app and Claude
Code. It sits in your main working view (the Claude Code status line in the
terminal, a band above the prompt in the Desktop Code tab) and answers, for
both windows: how fast am I spending, how fast can I afford to, and where will
I land at the reset.

```
5h 62% ×1.4▲ →88% @13:20 · wk 48% +3 ×0.8▼ →96% @Thu 09:00
```

No network, no token: it reads the rate limits Claude Code already hands to the
status line, keeps a history in SQLite, and learns your hour-of-week rhythm.
Python 3.8+ standard library only.

## Reading the line

| Part | Meaning |
|---|---|
| `5h 62%` | 5-hour window: 62% used |
| `×1.4▲` | multiplier: you can go 1.4 times faster and still land on the target at the reset. `▲` speed up (≥ 1.2), no glyph on pace, `▼` slow down (< 0.9) |
| `~×1.4` | same, but the rate leans on interpolation (fewer than 3 fresh readings in the last 20 minutes) |
| `→88%` | projected level at the reset at the current burn rate |
| `dry 12:40` | instead of the projection: you hit 100% at 12:40, before the reset |
| `idle ≤9%/h` | nothing spent in the last 20 minutes; you can afford up to 9 points an hour |
| `@13:20` | reset time |
| `wk 48%` | weekly window: 48% used |
| `+3` | points versus your on-pace level (plus: ahead of your usual rhythm, minus: surplus) |
| `×0.8▼` | weekly multiplier (same reading as above) |
| `→96%` | projected weekly level at the reset; 100 minus this will expire |
| `dry~Wed 14:00` | the week runs out on Wednesday around 14:00 |
| `@Thu 09:00` | weekly reset |
| `5h 0%` / `wk 0%` | no open window (the next message starts one) |

Colors: dim when fine; **yellow** when you are above target pace; **red** when
a window runs dry before its reset or is maxed; **green** when a window is in
its last stretch (1 hour for 5h, 2 days for the week) with at least 10 points
about to expire. `NO_COLOR` turns colors off; the ▲/▼ glyphs keep the verdict.

`ctempo now` prints the same with the reasoning behind every number.

## The math

**5-hour window**

- current rate = least-squares slope, over the trailing 20 minutes, of the
  level path drawn through fresh readings only (0 at the window start, linear
  between readings, one whole-percent step past the last one at its slope,
  then flat): steady work keeps its rate between steps, a burst that ended
  fades out, idle is 0, and spend in a gap you did not observe (claude.ai
  while Claude Code sat idle) is spread over the gap rather than shown as a
  sudden burst;
- allowed rate = (target − used) / hours to reset (target defaults to 100);
- multiplier = allowed / current;
- projection = used + rate × hours left, capped at 100, with the time it hits
  100 if that comes first.

**Weekly window**

- profile `w(h)`: 168 hour-of-week bins, the expected share of a week's spend in
  each hour. Prior from your transcripts (last 45 days, cost-weighted tokens),
  updated from the observed weekly-% spend of each hour with a 14-day half-life,
  off-hours floored at 0.15 of a typical awake hour;
- `E` = Σ w(h) from now to the reset: the share of a week's spend still ahead;
- on-pace level = target × (1 − E); delta = used − on-pace;
- intensity = what you spent over your last 24 awake hours / what the profile
  expected for those hours (at target pace);
- projection = used + intensity × target × E; "dry" is where an hour-by-hour
  walk at that intensity crosses 100%;
- multiplier = (target − used) / (intensity × target × E).

Readings from idle sessions, the 5-minute wobble of `resets_at`, the
`used_percentage` epoch leak (#52326), resets that keep the same `resets_at`,
and DST weeks are all handled; DECISIONS.md explains each rule.

## Install (on your Mac)

```sh
git clone <this repo> ~/Projects/claude-tempo && cd ~/Projects/claude-tempo
python3 bin/ctempo install                          # dry run: shows the diff, changes nothing
python3 bin/ctempo install --apply --bin-dir ~/.local/bin
```

`install --apply`:

1. picks and verifies an absolute interpreter (`/usr/bin/python3` first, then
   Homebrew, then the one you ran it with; `--python /abs/path` to choose) and
   pins it in the command, run as `python -I -S` so conda or PYTHONPATH cannot
   interfere;
2. backs up `~/.claude/settings.json` to `settings.json.ctempo-backup-<time>`
   and sets only `statusLine` (`refreshInterval: 60`). If you already have a
   status line, ctempo **composes**: your command still runs, with the same
   input, and ctempo's segment is appended to its output (`--replace` to replace
   it instead);
3. installs the Desktop mod from the local marketplace in this repo:
   `claude plugin marketplace add <repo>` then
   `claude plugin install ctempo@claude-tempo` (`--no-mod` to skip);
4. writes `~/.claude-tempo/runtime.json` (the mod reads it), a default
   `config.json`, and a `ctempo` launcher in `--bin-dir`;
5. builds the profile prior from `~/.claude/projects` transcripts
   (`--no-bootstrap` to skip).

The terminal status line applies at once. For the mod, restart Claude Code or
run `/reload-plugins` (in Desktop: start a new session). Then `ctempo doctor`.

## Uninstall

```sh
ctempo uninstall            # dry run
ctempo uninstall --apply
```

It uninstalls the mod and the marketplace, puts your previous `statusLine`
back (or removes ctempo's), removes the launcher and `runtime.json`. If you did
not change `settings.json` in between, the file comes back byte for byte (it is
restored from the backup). Your history stays in `~/.claude-tempo/`; delete
that folder yourself if you want it gone.

## Commands

| Command | What it does |
|---|---|
| `ctempo statusline` | reads the status-line JSON on stdin, records a sample, prints the line (`--format json` for the mod) |
| `ctempo now` | the line plus the reasoning: rates, allowed rates, on-pace level, intensity, projections, exchange rate |
| `ctempo report [--days N]` | waste ledger: % unused at each closed weekly and 5-hour window, totals, in 5h-window equivalents |
| `ctempo profile [--rebuild]` | hour-of-week heatmap, lightest awake blocks, points vs pace by day before reset |
| `ctempo check [--min-free X] [--tail 5h=60m,week=2d] [--max-age 30m]` | for a future scavenger: exit 0 and JSON when there is quota to harvest now, else exit 1 |
| `ctempo bootstrap [--days N] [--dry-run]` | rebuild the profile prior from transcripts |
| `ctempo install` / `uninstall` | see above; dry run unless `--apply` |
| `ctempo doctor` | interpreter, database, settings wiring, mod, last samples, last line and the raw rate limits it came from, rate-limit kinds seen |
| `ctempo config [show|init|path]` | effective configuration |

`ctempo` with no command is `ctempo now`.

## Configuration

`~/.claude-tempo/config.json` (written with defaults by install; any key you
leave out keeps its default):

| Key | Default | Meaning |
|---|---|---|
| `target_5h`, `target_week` | 100 | level to pace against; 90 keeps a 10-point reserve |
| `rate_window_min` | 20 | 5h least-squares window, minutes |
| `verdict.speed_up`, `verdict.slow_down` | 1.2, 0.9 | multiplier thresholds |
| `tail.five_hour_min`, `tail.seven_day_hours` | 60, 48 | "last stretch" of each window |
| `min_free` | 10 | points about to expire that count as worth using (green, `check`) |
| `profile.half_life_days` | 14 | how fast the profile forgets |
| `profile.floor` | 0.15 | off-hour weight relative to a typical awake hour |
| `profile.override` | null | `{"work_days": [0,1,2,3,4], "wake_hours": [8, 23], "off_day_factor": 0.3, "mode": "prior"}`; days are 0 = Monday; mode `replace` ignores learning |
| `reset.drop_points`, `reset.drop_samples` | 15, 2 | a sustained drop that counts as a reset |
| `display.symbols` | `unicode` | `ascii` for `x ^ v ->` |
| `display.max_width` | 80 | the line drops optional parts to fit |
| `mod.surfaces` | `["desktop"]` | where the mod draws its band (add `terminal` if you do not use the status line) |
| `mod.refresh_s` | 60 | how often the mod refreshes the band (15 to 3600) |
| `compose.timeout_ms` | 1500 | how long your previous status line may take |
| `retention_days` | 365 | history kept |

`CTEMPO_HOME` moves everything (default `~/.claude-tempo`). The Desktop app
does not inherit your shell's environment, so if you move it, set it in
`~/.claude/settings.json` under `env` too. Values of the wrong type or out of
range fall back to the default or are clamped.

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
p95 47 ms on Python 3.13; p50 34 ms, p95 44 ms on Python 3.9. The budget is
100 ms.

## Tests

```sh
python3 -m unittest discover -s tests -t .     # 142 tests, Python 3.8 to 3.13
claude plugin validate mods/ctempo && claude plugin test mods/ctempo   # 9 tests
```

The suite feeds synthetic time series (steady burn, bursts, idle, a reset
mid-series, two parallel sessions, missing windows, bogus `resets_at`, the
epoch leak, a week with nights and weekends, DST) and checks the math against
values computed by hand in the test comments. Install and uninstall run on
throwaway settings files, including the byte-identical round trip (CRLF,
mode, several settings files, a crash mid-install, a lost install record).
Three adversarial review passes found 40 defects; each one has a regression
test that fails on the code before its fix.

## Layout

```
bin/ctempo                 launcher (python -I -S safe)
ctempo/                    the core: store, ingest, pace, weekly, profile, render,
                           statusline, commands, install, transcripts
mods/ctempo/               the Desktop mod (TypeScript, thin)
.claude-plugin/            local marketplace: claude-tempo
tests/                     unittest suite
scripts/bench_statusline.py
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
