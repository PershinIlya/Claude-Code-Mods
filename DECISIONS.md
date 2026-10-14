# Decisions

Every choice that was not dictated by the brief, with the reason. Numbered so
code comments can point here.

## Environment and packaging

**D-01. Built in a cloud container, not on the Mac.** The session ran in a
Linux container (Ubuntu 24.04, Claude Code 2.1.299). There is no access to the
MacBook, its `~/.claude`, its transcripts or its Keychain. So "install for
real", "live samples" and "bootstrap from my transcripts" were done against
the container's own `~/.claude` (one live session, this one), and the Mac
install is a single command. The code has no Linux-only paths: it uses
`time.localtime`, POSIX `sh` for composition, and probes `/usr/bin/python3`,
`/opt/homebrew/bin/python3` and `/usr/local/bin/python3` (macOS locations)
first.

**D-02. Python 3.8+, standard library only, package plus launcher.** The core
is `ctempo/` (importable) and `bin/ctempo` (a launcher that puts the repo on
`sys.path`, so it runs under `python -I -S`). macOS Command Line Tools ship
Python 3.9, so nothing newer than 3.8 syntax is used; the suite runs on 3.8,
3.9, 3.11, 3.12 and 3.13. The statusline path does not import argparse.

**D-03. Interpreter pinning.** `ctempo install` probes candidates with
`python -I -S -c "import sys, sqlite3, json"` (three runs, best time), in this
order: `/usr/bin/python3` (skipped on macOS when the Command Line Tools are
missing, because the shim would pop an install dialog), Homebrew, the
interpreter running install, `which python3`. The first that passes wins;
`--python` overrides. System and Homebrew paths come first because a conda env
can be deactivated, updated or deleted, and the Desktop app does not inherit
the shell's PATH. The chosen absolute path goes into the statusLine command
and into `~/.claude-tempo/runtime.json` (read by the mod). `-I` ignores
PYTHON* variables and the user site; `-S` skips `site` (faster, and stdlib
needs nothing from it).

## Data

**D-04. Sources.** Primary: the statusLine stdin payload, written by
`ctempo statusline` (no network, no token). Desktop: the mod reads
`$.session.usage().rateLimits` (and the same list from `session.measure`),
converts it to the statusLine shape (ISO `resetsAt` to epoch seconds) and
calls the same CLI. One code path, two collectors.

Left out of v1, as asked: polling the undocumented `/api/oauth/usage`. It would
add (a) per-model weekly caps (`seven_day_opus`, `seven_day_sonnet`, the
`limits[]` "weekly_scoped" entries), (b) usage from other devices and from
claude.ai while no Claude Code session is open (the statusline only learns
about it at the next API response), (c) `extra_usage`. Cost: it needs the
OAuth token (Keychain item "Claude Code-credentials"), it can be rate limited
(429), and the endpoint is undocumented.

**D-05. Store.** SQLite at `~/.claude-tempo/tempo.db` (override with
`CTEMPO_HOME`), WAL, `synchronous=NORMAL`, busy timeout 250 ms, schema version
in `PRAGMA user_version`. Tables: `samples(ts, window, used_pct, resets_at,
source, session_id, flag, window_id)` (the brief's columns plus `flag` and
`window_id`), `windows` (one row per window instance: start, reset, first/last
seen, last and max used, closed_at, unused_at_close, close_reason), `readers`
(each session's last reading, for freshness), `kinds` (every rate-limit kind
seen, with source and count), `meta` (profile, counters, rejections). One
`BEGIN IMMEDIATE` transaction per ingest. Any `sqlite3.OperationalError`
(locked) skips the write and still renders; a broken database falls back to a
line built from the payload alone.

**D-06. Freshness.** The statusLine payload carries the rate limits from the
session's *last API response*. An idle session re-renders every 60 s with an
old value; with parallel sessions, that stale value interleaves with a busy
session's fresh ones (ccpool saw 76% of rows sharing timestamps). A reading is
FRESH when it differs from what the same session reported before; otherwise it
is a HEARTBEAT: it proves time passed, never moves the series, never raises a
window's max, and cannot trigger a reset. Analysis uses the running max of
fresh readings per window instance (usage is monotone within a window).

**D-07. Write throttle and dedupe.** A fresh reading identical to the last
stored row of that window within 60 s is not stored again (two sessions
reporting the same value). A heartbeat is stored only if the window's last row
is at least 60 s old, across all sessions. The per-session reader row is
written only on change or once a day.

**D-08. Window instances.** `resets_at` within 300 s of the open instance is
the same window: the epoch wobbles by seconds between renders (ccpool,
claumon). A later `resets_at` rolls over (the old instance closes with
`unused_at_close = 100 - max_used`). Instances whose reset time passed close
as `expired` at their reset time. Window start is `resets_at - 5 h` or
`- 7 d`.

**D-09. Guards.** `used_percentage` must be a finite number in [0, 105]:
Claude Code bug #52326 sometimes puts the `resets_at` epoch there (ccpool,
Claude-Code-Usage-Monitor); values in (100, 105] clamp to 100. `resets_at`
may be seconds, milliseconds, a numeric string or ISO 8601; it must be after
2017, not already passed (5 s grace), and no further ahead than the window
length plus 15 min (5h) or 1 h (7d). Each rejection is counted in `meta`
(`ctempo doctor` shows them). Booleans are not numbers.

**D-10. Reset detection.** Besides a later `resets_at`: a fresh reading at
least 15 points below the window's max is an ANOMALY; two consecutive fresh
anomalies (default `reset.drop_samples = 2`) are a reset, the new instance
starts at the first one. This is ccpace's rule; ccpace observed a real 100 to 0
reset with an unchanged `resets_at`. Every low in the run must itself be 15
points down, which closes ccpace's over-count case (50 then 30, 48). A fresh
reading with an *earlier* `resets_at` is an anomaly too ("shift"): if it is
sustained, the open instance came from a bogus later reset, and the real
window, closed by it, is reopened instead of being split in two.

## 5-hour math

**D-11. Burn rate.** Least-squares slope of the running max over the trailing
20 minutes (brief), with two anchors: the window's value at the start of the
fit window (if known) and the current reading at "now" (if not already a
stored row). Heartbeats are points too. Effect: a burst that stopped 15
minutes ago keeps lowering the slope as flat points accumulate, and drops out
after 20 minutes; idle gives exactly 0. ccpool fits only the samples and ends
at the last one, so an old burst keeps a positive slope. Needs 3 points
spanning 5 minutes; otherwise the window average (`used / elapsed`) is used and
marked `~` on the line. Slopes below 0.05 %/h count as idle.

**D-12. Allowed rate, multiplier, projection.** As in the brief: allowed =
(target - used) / hours to reset; multiplier = allowed / current; projection
= used + rate x remaining, capped at 100, with the cap time when it would hit
100 before the reset. Verdict: multiplier >= 1.2 is "speed up", < 0.9 is
"slow down", else "on pace" (configurable). When idle the line shows the
allowed rate ("idle <=9%/h").

## Weekly math

**D-13. Profile shape.** 168 bins in local time (Monday 00:00 = bin 0); time is
cut at local hour boundaries, so DST days with 23 or 25 hours are handled.
`w(h)` combines:

- prior `p(h)`: from transcripts (D-14), from the manual override
  (`work_days`, `wake_hours`, `off_day_factor`, mode `prior` or `replace`, like
  ccpool's work days and wake hours), else uniform;
- observations: weekly-percent spend per completed local hour. Spend between
  consecutive fresh readings is spread evenly in time when the gap is at most
  2 h (well localized, confidence 1), by the current profile when longer (only
  the total is known, confidence 0.25). An hour is final once a fresh reading
  after it exists. Per bin, a decay-weighted mean with a 14-day half-life
  (continuous-time EWMA: weight `0.5 ** (age / 14 d)`).
- shrinkage: `r(h) = (n m + k M p) / (n + k)`, `n` the decayed confidence
  weight, `m` the observed mean, `M` the weekly total. `k = 1` for an
  informative prior (transcripts, override), `0.25` for the uniform one so a
  few observations can outweigh "I know nothing". `M` is the decay-weighted
  peak of recent closed weekly windows that were observed in their last day
  (an unobserved tail understates the peak, claumon's weak spot); without
  those, the unweighted ratio `sum(m) / sum(p)` over observed bins.
  Unweighted because busy hours are observed with more confidence (short gaps),
  which inflated the weighted ratio from 40 to 106 in a test.
- floor: every bin is raised to `0.15 x typical`, typical = mean of the busiest
  third of the bins (ccpool's 0.15). An hour is "awake" when its rate is at
  least 0.25 x typical (thevibeworks' rest-hour threshold).

Why not thevibeworks' factorized weekday x hour-of-day model: it has more data
per parameter, but cannot represent "Friday afternoons are light" and needs 14
days before it speaks. The transcript prior removes the cold start, and the
shrinkage handles the noise of 168 bins.

Learning runs inside the statusline at most every 10 minutes (incremental, a
few ms); `ctempo profile --rebuild` relearns from all samples.

**D-14. Transcript prior.** Assistant messages under `~/.claude/projects/**`
(subagents included) from the last 45 days (`profile.bootstrap_days`); files
not modified in that period are skipped unopened. Per message only the
timestamp, message id and token counts are kept; content is parsed (the line
is one JSON object) and dropped. A message streamed over several lines shares
one id; the largest usage wins. Weight = `input + 1.25 cache_creation + 0.1
cache_read + 5 output` (API price ratios relative to fresh input); only the
distribution over the week matters. Each bin's mean is over *every* hour of
the period, zeros included (thevibeworks averages per active day, which runs
high), then lightly smoothed along the hour axis. A short history is blended
toward uniform: `prior = mix x shape + (1 - mix) x uniform`, `mix = min(1,
days / 14)`, and the prior strength `k` moves from 0.25 to 1 with it. Found
on live data: one hour of transcripts otherwise made a three-hour spike. It
misses claude.ai and Desktop chat; the percent observations correct that over
time. `install --apply` runs the bootstrap too (`--no-bootstrap` skips it).

**D-15. Weekly numbers.** As in the brief, with these details:
`E` is normalized by the week's total share (DST weeks have 167 or 169 local
hours). Intensity uses the last 24 *awake* hours, walking back through nights
and weekends, and crosses into the previous weekly window when needed (spend =
previous window's peak minus its value at the lookback start, plus the current
window from 0). Fallbacks when the lookback holds less than 1% of a week's
expected spend: the whole window so far, then the typical week `M / target`,
then 1.0, each labelled in `ctempo now`. "Dry" is the hour-by-hour walk at
that intensity (thevibeworks' `_profile_walk` shape). Verdict thresholds are
the same as for 5h; "idle" when the last 24 awake hours spent nothing.

**D-23. Exchange rate.** Weekly points per full 5-hour window, from paired
readings (thevibeworks' `pct_per_window`: published once the 5h side rose 50
points and the weekly side 3, clamped to [1, 50]). `now` and `report` use it
to say how many 5-hour windows the expiring weekly surplus is worth; `check`
uses it to price a 5h leftover in weekly points.

## Display

**D-16. The line.**
`5h 62% ×1.4▲ →88% @13:20 · wk 48% +3 ×0.8▼ →96% @Thu 09:00`.
`×` is the multiplier with a verdict glyph (▲ speed up, ▼ slow down, none on
pace), so the verdict survives `NO_COLOR`. `+3` is points versus the on-pace
level (plus = spending faster than your profile). `dry 12:40` / `dry~Wed
14:00` replaces the projection when the limit would be hit before the reset.
`~` marks the 5h window-average fallback. Tones: dim when fine, yellow for
slow down, bold red when it runs dry or is maxed, and green (an addition to
the brief) when a window is in its tail with at least `min_free` points about
to expire. `NO_COLOR` and `TERM=dumb` disable color; `display.symbols:
"ascii"` swaps the glyphs. When the line would exceed 80 columns, optional
parts are dropped in order: 5h reset time, weekly delta, weekly reset time,
then projections. The statusline never prints a traceback: errors go to
`~/.claude-tempo/errors.log` (64 KB, one rotation) and the line degrades to
"ctempo: error" or a bare line from the payload.

**D-17. Composing with an existing statusLine.** `install` keeps the old
command inside ours (`--compose '<old command>'`, shell-quoted), so
`settings.json` shows exactly what runs. At render time the old command starts
first, in its own process group, with the same stdin, and runs in parallel
with ctempo's own work; its output comes first and our segment is appended to
its last line (two spaces) or put on a new line when that would exceed
`COLUMNS`. Timeout 1.5 s, then it is killed with its children and only our
segment shows. `CTEMPO_COMPOSED=1` stops recursion. `--replace` replaces
instead. `refreshInterval` becomes 60, or stays lower if the old one was lower.

**D-19. The mod.** Thin: it builds the statusLine-shaped payload from
`rateLimits`, runs `python -I -S bin/ctempo statusline --source mod --format
json` through `$.process.run` and draws the tone-tagged segments it gets back
(no ANSI parsing). It calls the CLI after each `session.measure` whose
`rateLimits` changed and every 60 s (time-dependent numbers), never on the
start path, and coalesces calls that arrive while one runs (statusline-anywhere
drops them). It draws on the surfaces in `mod.surfaces` (default `["desktop"]`;
VS Code and mobile draw no band per the docs) and passes the band on in the
terminal, where the statusLine already shows the line; it keeps what other
mods draw below its own row. Every `kind` it sees is logged by the CLI in the
`kinds` table.

**D-24. No hooks into Claude's context.** v1 adds no settings hooks and no
`prompt.*` hooks: the line is for the person, not the model.

## Install

**D-18. install / uninstall.** Dry run by default: prints the interpreter
probe table, a unified diff of `settings.json`, the plugin commands and the
files it would write. `--apply` copies `settings.json` to
`settings.json.ctempo-backup-YYYYmmdd-HHMMSS`, then writes atomically
(tmp + rename, symlinked settings keep the link). Only `statusLine` changes;
the file's indentation and trailing newline are detected and kept. What was
there before is recorded in `~/.claude-tempo/install.json`. `uninstall`
restores the original `statusLine` (or removes it), removes
`enabledPlugins` / `extraKnownMarketplaces` if they did not exist before and
the plugin CLI left them empty, and when the result equals the backup it
writes the backup's bytes, so an untouched file comes back byte for byte (the
test suite checks four formatting styles). The mod is installed with `claude
plugin marketplace add <repo>` and `claude plugin install
ctempo@claude-tempo` (user scope); `--no-mod` skips it. `--bin-dir
~/.local/bin` adds a `ctempo` launcher with the pinned interpreter.

## The scavenger hook

**D-20. `ctempo check`.** Exit 0 and `"ok": true` when spare quota can be
harvested now, 1 otherwise, JSON on stdout either way. A 5h window is
harvestable when it is in its tail (`--tail 5h=60m`), its projected unused is
at least `--min-free` (default 10) and at least that much is free now, and the
weekly window would not run dry or exceed its target if that leftover were
spent (priced with the exchange rate). The weekly window is harvestable when
it is in its tail (`week=2d`) with at least `min-free` projected to expire and
the 5h window has room, both now and at the current burn rate (projected
unused >= `min-free`): a scavenger must not starve interactive work. Data older than `--max-age` (30 min) is a no-go:
headless runs do not render a statusLine, so a scavenger has to refresh the
data itself (v2).

## Retention

**D-22.** Samples older than `retention_days` (365) and closed windows older
than that are deleted once a day by the statusline; per-session reader rows
after 7 days.

## Ideas left for v2

- An 80% band on both projections (claumon's empirical Bayes with a Gamma
  process), with ctempo's profile as the prior mean instead of one global
  rate.
- Optional `/api/oauth/usage` polling for per-model weekly caps and off-device
  usage (D-04), behind an explicit opt-in.
- The scavenger: a queue of jobs run with `claude -p` when `ctempo check` says
  go, which records rate limits from the SDK's usage reports itself.
- Mean reversion in the weekly projection (recent intensity decaying toward
  the typical week over the remaining days).
- A `report` breakdown by project (from transcript token counts).
