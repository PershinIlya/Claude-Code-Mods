# Decisions

Every choice that was not dictated by the brief, with the reason. Numbered so
code comments can point here.

## Environment and packaging

**D-01. Built in a cloud container, not on the Mac.** The session ran in a
Linux container (Ubuntu 24.04, Claude Code 2.1.299). There is no access to the
Mac, its `~/.claude`, its transcripts or its Keychain. So "install for
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
`CTEMPO_HOME`; a leading `~` is expanded and a relative path is taken from the
home directory, because Claude Code passes settings.json `env` values as
written and the statusLine runs in each session's folder), WAL,
`synchronous=NORMAL`, busy timeout 250 ms, schema version in `PRAGMA
user_version`. Tables: `samples(ts, window, used_pct, resets_at, source,
session_id, flag, window_id)` (the brief's columns plus `flag` and
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
window's max, never rolls a window over, does not update `last_seen`, and
cannot trigger a reset. A session's very first reading is stored as FIRST:
when the statusLine is installed, every open session re-renders at once with
whatever it last saw, so that reading is a real level but may be hours old. It
counts for the window's max and for the weekly peak, cannot be an anomaly (two
idle sessions would otherwise fake a drop reset), is not used for profile
learning, enters the 5h level path only as a lower bound (D-11), and never
fixes the start of the weekly lookback (D-15). These rules were found in
review. The line shows the level as judged and stored; the raw reading is used
only when the write failed (an idle session's stale pre-reset value once
showed "maxed"). Analysis uses the running max of fresh readings per window
instance (usage is monotone within a window).

**D-07. Write throttle and dedupe.** A fresh reading identical to the last
stored row of that window within 60 s is not stored again (two sessions
reporting the same value). A heartbeat is stored only if the window's last row
is at least 60 s old, across all sessions. The per-session reader row is
written only on change or once a day.

**D-08. Window instances.** `resets_at` within 300 s of the open instance is
the same window: the epoch wobbles by seconds between renders (ccpool,
claumon). A later `resets_at` rolls over (the old instance closes with
`unused_at_close = 100 - max_used`). Instances whose reset time passed close
as `expired` at their reset time; a reading whose `resets_at` is not in the
future is rejected (no grace, so the two rules agree). Window start is `resets_at - 5 h` or
`- 7 d`.

**D-09. Guards.** `used_percentage` must be a finite number in [0, 105]:
Claude Code bug #52326 sometimes puts the `resets_at` epoch there (ccpool,
Claude-Code-Usage-Monitor); values in (100, 105] clamp to 100. `resets_at`
may be seconds, milliseconds, a numeric string or ISO 8601; it must be after
2017, not already passed (5 s grace), and no further ahead than the window
length plus 15 min (5h) or 1 h (7d). Each rejection is counted in `meta`
(`ctempo doctor` shows them). Booleans are not numbers.

**D-10. Reset detection.** Besides a later `resets_at`: a fresh reading at
least 15 points below the window's max (15 included) is an ANOMALY; two
consecutive fresh anomalies (default `reset.drop_samples = 2`) are a reset,
the new instance starts at the first one. This is ccpace's rule; ccpace
observed a real 100 to 0 reset with an unchanged `resets_at`. Every low in the
run must itself be 15 points down, which closes ccpace's over-count case (50
then 30, 48). A fresh reading with an *earlier* `resets_at` is an anomaly too
("shift"): if it is sustained, the open instance came from a bogus later
reset, and the real window, closed by it, is reopened instead of being split
in two. The superseded instance stays closed with reason `shift`. It never
ended, so `report`, the profile's points-vs-pace table, the typical week and
the weekly lookback all leave it out.

## 5-hour math

**D-11. Burn rate.** A heartbeat repeats a session's last API response and
says nothing about the account's level (claude.ai keeps spending while a
Claude Code session idles), so the rate uses fresh readings only. They form a
*level path*: a point where each new level (running max) was first read (a
repeat of the same whole percent says nothing about when the next one comes),
through 0 at the window start (every window opens empty), linear between
readings (spend in an unobserved gap is spread over the gap instead of being
squeezed into the moment we looked). After the last step between two readings
the path keeps that step's slope for one reading quantum (the smallest step
seen, 0.1 to 1 point: steady work shows no new value between whole-percent
steps, and a flat tail used to read it as slowing down), then goes flat (no
news, no spend: a finished burst fades out and idle reads exactly 0). A
session's FIRST reading (D-06) only says the level was at least that by then,
so it becomes a point only where it lifts the path: a 3-hour-old 30%
re-rendered at install next to a fresh 60% is no 30-point burst. The rate is
the least-squares slope of that path over the trailing 20 minutes (brief),
sampled at its edges and at every point inside, but never reaching back past
the window's first real point once a second real point follows it (the
0-at-start ramp diluted a late start); until 5 minutes follow that first point
there is no rate, and the line shows only the allowed rate. The burn is then
unknown, not zero, and so is the leftover: the window is never green (D-16)
and `check` says no go (D-20); `ctempo now` names which wait it is (the window
opened less than 5 minutes ago, or work started in it less than 5 minutes ago,
at HH:MM). An unknown burn priced as zero turned a late start in the tail
green, with `check` saying go, while it ran dry (found in review). A single
real point keeps reaching back: the window average, which fades out over the
fit window. (Jumping to the first point on elapsed time alone turned a single
reading into idle after 5 minutes, and at a window's start flipped speed up to
dry with no new data.) With at least 3 readings spanning 5 minutes (or the
whole fit window, if that is shorter; a FIRST reading counts only where it is
a point of the path) the fit is plain least squares; otherwise it leans on
interpolation and the line marks it `~` (a single reading in a window gives
the window average). Slopes below 0.05 %/h count as idle. ccpool fits only raw
samples and ends at the last one, so an old burst keeps a positive slope. This
model came out of three review passes: anchoring on the last stored value let
an idle session's stale heartbeat fake a 36 %/h burst, an open session's
heartbeats squeezed two hours of claude.ai use into minutes, and the flat tail
made steady 9 %/h work flicker between "slow down" and "speed up".

**D-12. Allowed rate, multiplier, projection.** As in the brief: allowed =
max(0, target - used) / hours to reset; multiplier = allowed / current;
projection = used + rate x remaining, capped at 100, with the cap time when it
would hit 100 before the reset. Verdict: multiplier >= 1.2 is "speed up", <
0.9 is "slow down", else "on pace" (configurable). When idle the line shows
the allowed rate ("idle <=9%/h").

## Weekly math

**D-13. Profile shape.** 168 bins in local time (Monday 00:00 = bin 0); time is
cut at local hour boundaries, so DST days with 23 or 25 hours are handled.
`w(h)` combines:

- prior `p(h)`: from transcripts (D-14), from the manual override
  (`work_days`, `wake_hours`, `off_day_factor`, mode `prior` or `replace`,
  like ccpool's work days and wake hours), else uniform;
- observations: weekly-percent spend per completed local hour. Spend between
  consecutive fresh readings is spread evenly in time when the gap is at most
  2 h (well localized, confidence 1), by the current profile when longer (only
  the total is known, confidence 0.25). An hour is final once a fresh reading
  after it exists. At a window change, the new window continues the old one (a
  moved or bogus reset, the real window reopened) only when it started before
  the previous reading and its level is closer to the old level than to 0:
  less than min(`drop_points`, max(1, half the old level)) below it. Otherwise
  it counts from 0. With the 15-point drop alone, `profile --rebuild` learned
  a light user's early reset as 0 %/h (found in review). Per bin, a
  decay-weighted mean with a 14-day half-life (continuous-time EWMA: weight
  `0.5 ** (age / 14 d)`).
- shrinkage: `r(h) = (n m + k M p) / (n + k)`, `n` the decayed confidence
  weight, `m` the observed mean, `M` the weekly total. `k = 1` for an
  informative prior (transcripts, override), `0.25` for the uniform one so a
  few observations can outweigh "I know nothing". `M` is the decay-weighted
  peak of recent closed weekly windows that were observed in their last day
  (by the last fresh reading, not by heartbeats; an unobserved tail
  understates the peak, claumon's weak spot); without those, the unweighted
  ratio `sum(m) / sum(p)` over observed bins. Unweighted because busy hours
  are observed with more confidence (short gaps), which inflated the weighted
  ratio from 40 to 106 in a test. The line, `ctempo profile`, `profile
  --rebuild` and learning all build the weights this way
  (`profile.current_weights`); `profile` used the ratio and printed a typical
  week of 168% (found in review).
- floor: every bin is raised to `0.15 x typical`, typical = mean of the
  busiest third of the bins (ccpool's 0.15). An hour is "awake" when its rate
  is at least 0.25 x typical (thevibeworks' rest-hour threshold).

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
toward uniform: `prior = mix x shape + (1 - mix) x uniform`, with
`mix = min(1, covered_days / 14, active_days / 4)`; `covered_days` starts
where 5% of the cost is behind, so one stray old message does not buy trust
(found in review). The prior strength `k` moves from 0.25 to 1 with `mix`. Found
on live data: one hour of transcripts otherwise made a three-hour spike. It
misses claude.ai and Desktop chat; the percent observations correct that over
time. `install --apply` runs the bootstrap too (`--no-bootstrap` skips it).

**D-15. Weekly numbers.** As in the brief, with these details: `E` is
normalized by the week's total share (DST weeks have 167 or 169 local hours).
Intensity uses the last 24 *awake* hours, walking back through nights and
weekends, and crosses into the previous weekly window when needed (spend =
previous window's peak minus its value at the lookback start, plus the current
window from 0). The value at the lookback start is the running max of readings
up to it. A FIRST reading (D-06) never fixes that start: with no FRESH reading
at or before it, the count starts at the first FRESH one (a stale level
re-rendered at install showed a red "dry" for a day, depending on which
session rendered first). When the start falls in a gap between readings longer
than `max_gap_hours`, the gap's rise is spread over it by profile share, as in
learning (D-13), and only the part after the start counts (a weekend of
claude.ai use was charged to the last 24 hours). Fallbacks when the lookback
holds less than 1% of a week's expected spend, or when it was cut short at the
first reading and has not seen a whole-percent step yet (that zero is no news,
not idle): the whole window so far (unless it is still at 0 after a cut
lookback, which is no news either; it showed `×? →0%`), then the typical week
`M / target`, then 1.0, each labeled in `ctempo now` with its reason (recent
span too short; the level has not moved since the first timed reading; no
timed reading yet); the typical week and 1.0 also say why the whole window
could not stand in (it is still at 0%, or it is too new: under 2% of a week's
expected spend has passed since it opened). They said "no recent data" and "no
data yet" next to hours of readings. "Dry" is the hour-by-hour walk at that
intensity (thevibeworks' `_profile_walk` shape). Verdict thresholds are the
same as for 5h; "idle" when the last 24 awake hours spent nothing. The
multiplier is max(0, target - used) / remaining spend, so it is 0 past a
target below 100, as for 5h.

**D-23. Exchange rate.** Weekly points per full 5-hour window, from paired
readings (thevibeworks' `pct_per_window`: published once the 5h side rose 50
points and the weekly side 3, clamped to [1, 50]). `now` and `report` use it
to say how many 5-hour windows the expiring weekly surplus is worth; `check`
uses it to price a 5h leftover in weekly points, and uses the clamp maximum
(50) while it is unknown.

## Display

**D-16. The line.**
`5h 62% ×1.4▲ →88% @13:20 · wk 48% +3 ×1.1 →95% @Thu 09:00`.
`×` is the multiplier with a verdict glyph (▲ speed up, ▼ slow down, none on
pace), so the verdict survives `NO_COLOR`. `+3` is points versus the on-pace
level (plus = spending faster than your profile). `dry 12:40` / `dry~Wed
14:00` replaces the projection when the limit would be hit before the reset.
`~` marks the 5h window-average fallback. Tones: dim when fine; yellow for a
mild overshoot (it runs dry before the reset while the verdict is still on
pace), for slow down without a dry time, or at or above a target set below 100
whatever the verdict (an idle window above its target was green, found in
review); bold red when it runs dry with a slow down verdict, or is maxed; and
green (an addition to the brief) when a window is in its tail with at least
`min_free` points about to expire, never while the 5h burn rate is unknown
(D-11), whatever `min_free` (0 included), and never for 0 points. At target
100 every slow down runs dry, so yellow was unreachable at the defaults until
red was made to need both (found in review). `NO_COLOR` and `TERM=dumb`
disable color; `display.symbols: "ascii"` swaps the glyphs. Output is written
as UTF-8 bytes whatever the locale. Config values of the wrong type or out of
range fall back to the default or are clamped at load (`config.RANGES`), so a
typo never breaks the line. When the line would exceed `display.max_width`
(80), optional parts are dropped in order: 5h reset time, weekly delta, weekly
reset time, then projections. If it still does not fit, it is cut at the width
with `…` (`...` in ascii mode) and each part keeps its tone; the bare line and
the waiting message use the configured separator and are fitted the same way.
The statusline never prints a traceback: errors go to
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
start path, and coalesces calls that arrive while one runs
(statusline-anywhere drops them). It draws on the surfaces in `mod.surfaces`
(default `["desktop"]`; VS Code and mobile draw no band per the docs) and
passes the band on in the terminal, where the statusLine already shows the
line; it keeps what other mods draw below its own row. Every `kind` it sees is
logged by the CLI in the `kinds` table. It resolves `CTEMPO_HOME` like the CLI
(empty counts as unset, a leading `~` is `HOME`, a relative path is taken from
`HOME`; `~user/...` is an error that asks for an absolute path), passes the
home it resolved to the CLI, shows the CLI's stderr when it fails, and re-arms
its timer to `mod.refresh_s` (clamped to 15..3600 s). Desktop does not inherit
the shell environment: set `CTEMPO_HOME` in `settings.json` `env` if you move
the home (Claude Code passes the value as written, without expanding `~`).

**D-24. No hooks into Claude's context.** v1 adds no settings hooks and no
`prompt.*` hooks: the line is for the person, not the model.

## Install

**D-18. install / uninstall.** Dry run by default: prints the interpreter
probe table, a unified diff of `settings.json`, the plugin commands and the
files it would write. `--apply` copies `settings.json` to
`settings.json.ctempo-backup-YYYYmmdd-HHMMSS`, then writes atomically (tmp +
rename, symlinked settings keep the link). Only `statusLine` changes; the
file's indentation, line endings (CRLF too), trailing newline and mode are
kept. What was there before is recorded in `~/.claude-tempo/install.json`, one
record per absolute settings path, saved *before* the settings file is written
(a crash later still leaves uninstall what it needs). A statusLine changed
since the last install becomes the new original, and so does a command the
user put in ctempo's `--compose` argument, also when there was no statusLine
before ctempo (then without the `refreshInterval` ctempo added; uninstall
restores it, and a repin or `--replace` records it first, also into a record
rebuilt after install.json was lost, where it wins over a backup); ctempo's
own command is never recorded or restored as the original (a pre-ctempo
backup, where a blank one means there was no statusLine, or `--compose` is
used instead). Backups are created exclusively and never overwritten.
`uninstall` asks which file when several are installed and never creates a
missing file. Records are matched by real path, so a symlinked settings file
is one record; a version-1 record with a relative path is recovered from its
backup's name, and one that version 1 already uninstalled is dropped. If the
plugin commands fail during uninstall (claude missing or hanging), the mod
record and `runtime.json` are kept and uninstall exits 1 so a rerun retries
(the warning says the mod may still be installed). When claude is not on PATH
at install, install prints the plugin commands for the user to run and still
records the mod (as manual, with the user-file snapshot), so uninstall runs
the reverse commands. A mod stays manual until ctempo sees the plugin CLI
write to the user file: the mod listed there at any install or uninstall, or
the file changed by a plugin command ctempo ran (commands that time out leave
it manual). A later install that finds claude on PATH while a manual mod's
user file shows no trace of it takes the user-file snapshot again before
running them, and that snapshot replaces a record kept for the user file.
Whatever the mod record, if claude is missing at uninstall and the user
settings file lists neither the plugin nor its marketplace (the commands never
ran or failed, or the user undid them by hand), there is nothing to undo and
uninstall completes. A user file the plugin CLI never wrote to (a manual mod)
is never rewritten, also when an uninstall reaches it as its own target,
unless the reverse commands write there at uninstall (then it comes back from
its snapshot, or goes if it did not exist). A user file that is not valid JSON
is left alone (it is not "absent" for the snapshot either: its plugin keys
count as the user's). The plugin CLI edits the user settings file, so that
file's record outlives its own uninstall while the mod stays (installed
elsewhere, or its commands failed), and whoever removes the mod brings the
file back from its backup; a user file that only the plugin CLI touched gets a
backup of its own before the first plugin command (also when an older mod
record has none), and that snapshot is the file's baseline whenever it has no
record of its own, as when an uninstall reaches it without one. A later
install into it keeps two baselines: the file as it is then (its own backup),
and, as a fallback, the file before the mod (its kept record's baseline, else
that snapshot). Restore tries them in that order, and deletes the file because
there was none before the mod only when neither backup matches. Every record
of a file, under any name, goes when it is uninstalled, and the user file's
kept record goes with the mod (also when that file is gone). Backups are
ordered by their time and sequence number, never as strings. `uninstall`
restores the original `statusLine` (or removes it), removes `enabledPlugins` /
`extraKnownMarketplaces` if they were not the user's (absent before, or
holding only ctempo's own entry because the commands were run by hand; a key
the user file no longer holds of its own when ctempo next looks, as after the
user deleted or blanked the file, stops counting as theirs) and the plugin CLI
left them empty, and when the result equals the backup (a blank backup counts
as `{}`, as on install) it writes the backup's bytes, so an untouched file
comes back byte for byte (the test suite checks four formatting styles and
blank files). The mod is installed with `claude plugin marketplace add <repo>`
and `claude plugin install ctempo@claude-tempo` (user scope); `--no-mod` skips
it. `--bin-dir ~/.local/bin` adds a `ctempo` launcher with the pinned
interpreter.

## The scavenger hook

**D-20. `ctempo check`.** Exit 0 and `"ok": true` when spare quota can be
harvested now, 1 otherwise, JSON on stdout either way. A 5h window is
harvestable when it is in its tail (`--tail 5h=60m`), its projected unused is
at least `--min-free` (default 10) and at least that much is free now, and the
weekly window would not run dry or exceed its target if that leftover were
spent (priced with the exchange rate; while that is unknown, at its clamp
maximum of 50 weekly points per 5h window, and the JSON says
`"exchange_rate_assumed": true`, only when a 5h leftover was priced that way).
The weekly window is harvestable when it is in its tail (`week=2d`) with at
least `min-free` projected to expire and the 5h window has room, both now and
at the current burn rate (projected unused >= `min-free`): a scavenger must
not starve interactive work. While the 5h burn rate is not known yet (D-11),
the 5h leftover is unknown: `projected_unused` and the weekly cost are null,
nothing is priced (`exchange_rate_assumed` is false), the reason is "5h burn
rate not known yet" (beside "already tight" when the week is tight on its own,
without a price), and neither the 5h harvest nor the weekly harvest's 5h room
test passes, whatever `--min-free` (0 included: the unknown leftover was read
as 0, and 0 >= 0 passed it and crashed `check`), so the weekly harvest also
pauses in the first 5 minutes of each 5h window. A leftover or free room
counts only when it is at least `--min-free` and more than 0 (at 0, a maxed 5h
window had "room"). `--min-free` is clamped to 0 to 100, like `min_free` in
the config; nan, inf or a negative duration is an error (JSON with `"error"`,
exit 1). Data older than `--max-age` (30 min) is a no-go. Age is measured from
the newest reading that carries information (FRESH, FIRST or ANOMALY), never
from heartbeats: an open idle session re-renders an old reading every minute,
while headless runs and claude.ai spend without rendering a statusLine.
`ctempo now` reports the same age (`data_age_s` in `--json`) with the last
heartbeat beside it; `doctor` keeps its per-source liveness on all samples,
since a heartbeat proves the statusLine runs. A scavenger has to refresh the
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

## Review

An adversarial review (6 reviewers, each finding checked by a skeptic who
tried to refute it) confirmed 23 defects and rejected 11. A second pass over
the fixes confirmed 10 more (mostly install edge cases, and the 5h model
above) and rejected 1; a third confirmed 7 (the 5h model's quantization, ramp
and first readings; install retries and aliases) and rejected none; a fourth
confirmed 19 distinct defects (21 reports, one of them found three times: the
5h fit start, color tiers and line fitting; the weekly lookback, gaps and
multiplier; `CTEMPO_HOME`, `check` and `report`; install retries, baselines
and the `--compose` argument) and rejected 2; a fifth, over those fixes,
confirmed 11 distinct defects (14 reports: an unknown 5h burn taken as zero,
`now` reasons and data age, the profile scale, a weekly window at 0, idle
above a target, and five install edge cases) and rejected 1; a sixth confirmed
9 (10 reports: an unknown 5h leftover compared as a number at `min_free` 0,
two `check`/`now` texts, and six install sequences) and rejected 3, a
model-based fuzzer over install and uninstall (tests/test_install_fuzz.py)
found 3 more classes, and the fixers' own notes 5 more (a leftover of 0
counted as spare, a nan duration, a user file that is not valid JSON in two
places, backups sorted as strings). All 87 are fixed, each with a regression
test that was checked to fail on the code before its fix. Details in the
commit messages from "Fix review findings" on.