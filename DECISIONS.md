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
`CTEMPO_HOME`; a leading `~` is expanded and a relative path is taken from the
home directory, because Claude Code passes settings.json `env` values as
written and the statusLine runs in each session's folder), WAL,
`synchronous=NORMAL`, busy timeout 250 ms, schema version in `PRAGMA
user_version`. The first open of a new database waits up to the busy timeout
for another opener's switch to WAL. SQLite answers a concurrent switch with
"locked" at once, without the timeout, so when every open session re-rendered
at install, some fell back to the bare line and lost their reading (found in
review). Tables: `samples(ts, window, used_pct, resets_at, source, session_id,
flag, window_id)` (the brief's columns plus `flag` and `window_id`), `windows`
(one row per window instance: start, reset, first/last seen, last and max
used, closed_at, unused_at_close, close_reason), `readers` (each session's
last reading, for freshness), `kinds` (every rate-limit kind seen, with source
and count), `meta` (profile, counters, rejections). One `BEGIN IMMEDIATE`
transaction per ingest. Any `sqlite3.OperationalError` (locked) skips the
write and still renders; when the skipped write would have opened a new window
(no open instance whose reset is still ahead), that window shows the payload's
level and reset only, with no pace math, as the bare line does; a broken
database falls back to a line built from the payload alone.

**D-06. Freshness.** The statusLine payload carries the rate limits from the
session's *last API response*. An idle session re-renders every 60 s with an
old value; with parallel sessions, that stale value interleaves with a busy
session's fresh ones (ccpool saw 76% of rows sharing timestamps). A reading is
FRESH when it differs from what the same session reported before; otherwise it
is a HEARTBEAT: it proves time passed, never moves the series, never raises a
window's max, never rolls a window over, does not update `last_seen`, and
cannot trigger a reset. A session id can be shared by 2 processes
(`claude -c`, or `--resume` without `--fork-session`, in a second terminal
while the first is open). The reader row then holds whichever wrote last, so
the idle process's re-render of its last API response differs from it and
looked fresh. A single process only moves forward: within a window its level
only rises, a moved reset keeps the level, and it returns to a `resets_at` it
left only to undo its own bogus later one (D-10). So a changed reading that
goes back to one the session already gave is the idle process's replay, and it
is a heartbeat: its reading before last (each session keeps it in a second
reader row), or a reading it gave a window that is no longer open (superseded
by a moved or an early reset). At the open window's reset only the first test
applies, and only when the session's last reading had that reset too (A, B, A
within the window): a render there whose session read another reset in between
is the busy process's, and it breaks the stale one's pending run (D-10). A
return to a later reset at the very level the session left stays fresh (a
reset moved earlier and back, read by a maxed session), and a return to an
earlier reset from a session that last read a later one is left to D-10. A
replay of a reading the session gave a window that is no longer open also
withdraws that reading's earlier render from a pending reset, so a later real
reset cannot sweep it in. At the open window's reset it does not: there A, B,
A is symmetric (the busy process's re-render of its own new reading matches it
too), and withdrawing there deleted the busy process's real pending drop, so a
real early reset was never taken; the statusline and the Desktop mod of one
session, which share its id, did the same when their writes crossed (found in
review). Instead the reading that completes a drop or a rise run must have
moved forward from its own session's last reading, as a single process does.
Idle twins faked drop resets below another session's max, reopened windows
that a moved or an early reset had superseded, and rolled a window whose reset
moved earlier back to the old reset on every render pair (found in review).
Limits: the reading before last does not catch 3 or more processes cycling on
1 session id; a reading that another session's identical row deduplicated
(D-07) leaves no sample to match; and a real reset whose `resets_at` lands on
that of a window ctempo closed, read at levels the session already gave that
window, waits for a level it did not give. Three more cases are known and left
as they are, since 2 processes on 1 session id are rare and ctempo cannot tell
them apart without a process identity (Claude Code runs each statusLine render
through a new `sh` in a new process group, so neither the parent nor the group
says which process it is): an idle process's stale reading can complete a real
drop run and set the new window's level until the real level passes it; with a
light week near 0, idle processes of 2 sessions can reopen a week that was
reset early; and a process's replay of a later reset it left can roll a window
whose reset moved earlier back (found in review). Opening one conversation in
a second terminal with `--fork-session` avoids all of them. A session's very
first reading is stored as FIRST: when the statusLine is installed, every open
session re-renders at once with whatever it last saw, so that reading is a
real level but may be hours old. It counts for the window's max and for the
weekly peak, cannot be an anomaly (two idle sessions would otherwise fake a
drop reset), is not used for profile learning (except that one at 100 marks
its window as capped, D-13), enters the 5h level path only as a lower bound
(D-11), and never fixes the start of the weekly lookback (D-15). These rules
were found in review. The line shows the level as judged and stored; the raw
reading is used only when the write failed (an idle session's stale pre-reset
value once showed "maxed"). Analysis uses the running max of fresh readings
per window instance (usage is monotone within a window).

**D-07. Write throttle and dedupe.** A fresh reading identical to the last
stored row of that window within 60 s is not stored again (two sessions
reporting the same value). A heartbeat is stored only if the window's last row
is at least 60 s old, across all sessions. The per-session reader row is
written only on change or once a day.

**D-08. Window instances.** `resets_at` within 300 s of the open instance is
the same window: the epoch wobbles by seconds between renders (ccpool,
claumon). A later `resets_at` rolls over (the old instance closes with
`unused_at_close = 100 - max_used`). When the new window began before the old
one's last fresh reading, at about the same level (less than
min(`drop_points`, max(1, half the old max)) below it), the reset moved: that
window never ended, and it closes as `shift` (D-10), so `report`, the
profile's points-vs-pace table, the typical week and the weekly lookback leave
it out. The 2 conditions are the continuation test learning uses (D-13). A
weekly reset that moved 1 h later mid-week listed the running week as
finished, with 55 phantom unused points (found in review). A new window that
began after the last reading is a real (early) reset, whatever its level.
Judged on the level alone, a light week that peaked at 10%, was reset early on
Wednesday and was first read again on Thursday at 6% (spent on claude.ai
first) closed as `shift`, and `report` left out a week that had ended (found
in review). Instances whose reset time passed close as `expired` at their
reset time; a reading whose `resets_at` is not in the future is rejected (no
grace, so the two rules agree). Window start is `resets_at - 5 h` or `- 7 d`.

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
the new instance starts at the first one, and the weekly math paces it from
there (D-15). The instance the drop closed (reason `drop`) never reached its
reset, so the weekly lookback and the profile's points-vs-pace table leave it
out. The typical week also leaves out the instance the drop opened, which
holds only part of a week (D-13). This is ccpace's rule; ccpace observed a
real 100 to 0 reset with an unchanged `resets_at`. Every low in the run must
itself be 15 points down, which closes ccpace's over-count case (50 then 30,
48). The anomaly that completes the run must also agree with its own session's
previous reading (a low one, for a drop): an idle twin's re-render goes
straight from the busy twin's reading to the anomaly, so it joins a run but
cannot complete it. Sessions that each had an idle twin with an old, lower
reading made a drop reset when the statusLine was installed, and the idle
twins of 2 sessions made one whenever their busy twins read something in
between (found in review). A drop that several sessions read once each
therefore waits for a second low reading from one of them. The window a drop
opens is exposed to the old level too: an idle twin's pre-reset reading is
fresh whenever its busy twin read twice since its last render, and it raised a
new window at 2% back to 85%, after which the real readings faked another drop
every minute (found in review). A fresh reading 15 points or more above the
max of a window a drop opened is an anomaly too (\"rise\"), taken by the same
rule, so a real jump (claude.ai use while Claude Code was idle) shows 1
reading later, and one that a maxed or idle session then only repeats waits
for another reading. A twin's pre-reset reading less than 15 points above the
new window's max still raises it; only that margin guards it, as on the undo
below. A fresh reading with an *earlier* `resets_at` is an anomaly too
("shift"): if it is sustained, the open instance came from a bogus later
reset, and the real window, closed by it, is reopened instead of being split
in two. A reset moved earlier at about the same level (D-08's test, less than
`drop_points` above the max, and not below the session's own last reading of
the open window), to a `resets_at` that no window instance ever had, is taken
on 1 reading, as a move later is. A maxed session whose 5-hour reset moved 1
hour earlier repeated its reading only as heartbeats, so the line kept the old
reset 1 hour past the real one, and a bogus later reset that was the first
reading of a new window was never undone (found in review). A stale reading (a
lagging session's or an idle twin's) carries a reset some instance had, so it
still needs `drop_samples`. One fresh reading undoes it, because one reading
did the damage. A maxed or idle session repeats its real reading only as
heartbeats, so the second fresh one never came: the line kept the bogus reset
time past the real reset, and `report` counted the window twice (found in
review). The undo by one reading takes 4 conditions. First, the bogus
instance's rollover closed the window that this reading's `resets_at` belongs
to, either as `shift` (D-08: the reset moved and the level held) or as
`rollover` with the bogus reading at or above that window's max (a maxed
session reads no fresh value after its 100, so its bogus reset can begin after
its last reading; matching `shift` only left it stuck again, found in review).
The undo follows the session's own chain of bogus rollovers back to the window
this reading belongs to, and every bogus instance in between closes as
`shift`: a maxed session that read two bogus later resets in a row kept a
bogus reset time, and `report` counted 3 windows (found in review). Each link
of that chain must pass the first condition, not only the last: a rollover
below its window's max is a real early reset, and the chain stops there
(walking across one let an idle twin reopen a week that had been reset early,
found in review). Second, the reading comes from the session whose reading
opened the bogus instance. Third, it is not below that opening reading, nor
below the session's own last reading of the moved reset (a session's real
reading never is; once the busy twin read a higher level at the moved reset,
an idle twin's stale reading equal to the opening one still undid the move,
found in review). Fourth, it sits less than `drop_points` above the bogus
instance's max. Two processes can share a session id (`claude -c` or
`--resume` without `--fork-session`), and the idle one keeps re-rendering the
old window. When the undo also matched a window closed as `rollover` (the
level dropped, or the new window began after the last reading, which is what a
real early reset looks like), only the `drop_points` margin guarded it. That
stale reading then flipped the line and `check` back to the dead window on
every render pair: from the first render after a weekly window at 10% was
reset early, and once a new 5-hour window climbed within 15 points of the old
90%. After a move to a later reset at 42%, a twin's stale 41% did the same
(found in review, twice). A twin whose stale reading equals the session's
reading of the moved reset still undoes it on each render pair until the level
moves on, and a twin's reading at the old later reset that equals the
session's reading of a reset moved earlier rolls it back the same way; the
readings cannot tell it from the session's own real reading. The undo still
moves the real window's pending readings from other sessions (up to
`drop_samples - 1`) into it. One was left behind in the superseded instance,
and the reopened window showed a level too low until the next fresh reading
(found in review). From any other session the undo still takes `drop_samples`,
since that session may lag a real early reset, and so does a window closed as
`rollover` above the bogus reading (a real early reset shows a lower level). A
session whose last reading, or its reading before last, had a later reset
completes such a run only with what a reset moved earlier looks like (about
the same level, not below its own last reading of the open window) when its
reading may be a replay: one it already gave (D-06), or its first change,
whose reading before last is unknown. The idle twins of 2 sessions, each at
the old reset while its busy twin read the reset moved later, made the run
that reopened the old window, and at install 2 sessions' idle twins put the
line on a week that had ended before ctempo saw it (found in review). The
superseded instance stays closed with reason `shift`. It never ended, so
`report`, the profile's points-vs-pace table, the typical week and the weekly
lookback all leave it out.

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
  a light user's early reset as 0 %/h (found in review). At a real reset, the
  old window's tail from its last fresh reading to the new window's start,
  when longer than `max_gap_hours`, is learned as no spend at confidence 0.25
  (no reading, no news, no spend, D-11). Unlearned, those hours kept `M p(h)`
  for good. With a Monday 09:00 reset and a uniform prior, a weekday user's
  weekend stayed awake: 107 awake hours instead of 45, and a typical week of
  90% for a true 56% (found in review). A window that reached 100% is the
  exception. The account was blocked in its tail, which says nothing about the
  habit, and learning it as idle made a user who caps every Tuesday read "on
  pace" on Monday. Reaching 100% counts by any FRESH or FIRST reading, as in
  the lookback (D-15). When the last points go on claude.ai, a new session's
  first reading can be the only one at 100, and learning, which looked at
  FRESH readings only, learned that blocked tail as idle (found in review).
  Per bin, a decay-weighted mean with a 14-day half-life (continuous-time
  EWMA: weight `0.5 ** (age / 14 d)`).
- shrinkage: `r(h) = (n m + k M p) / (n + k)`, `n` the decayed confidence
  weight, `m` the observed mean, `M` the weekly total. `k = 1` for an
  informative prior (transcripts, override), `0.25` for the uniform one so a
  few observations can outweigh "I know nothing". `M` is the decay-weighted
  peak of recent closed weekly windows that were observed in their last day
  (by the last fresh reading, not by heartbeats; an unobserved tail
  understates the peak, claumon's weak spot) and lasted a whole week (the
  window a drop reset opens holds only part of one: with one among 3 weeks at
  90%, the typical week read 77%, found in review; nor does one that an early
  reset cut short more than 1 hour before its reset: read in its last day, a
  window closed 23 h early at 74% pulled the typical week to 86%, found in
  review); without those, the unweighted ratio `sum(m) / sum(p)` over observed
  bins. Unweighted because busy hours are observed with more confidence (short
  gaps), which inflated the weighted ratio from 40 to 106 in a test. The line,
  `ctempo profile`, `profile --rebuild` and learning all build the weights
  this way (`profile.current_weights`); `profile` used the ratio and printed a
  typical week of 168% (found in review). `ctempo profile` names the three
  lightest 3-hour blocks whose hours are all awake by that rule, and none on a
  flat profile. It listed "Mon 00-03, Mon 03-06, Mon 06-09" on a flat profile,
  and wake-up or bedtime blocks that were two-thirds asleep (found in review).
  Its points-vs-pace table is the level minus the on-pace level for
  `target_week`, each window paced from its own start as on the line (D-15),
  so the reset column is what expired (100 - peak) only at target 100. It
  leaves out a window that an early reset cut short (a drop, D-10, or a
  rollover more than 1 hour before its reset). The window that replaced it
  holds the rest of that week, so each week counts once. The table paced a
  drop's window from `resets_at - 7 d`, set the closed window's frozen level
  against a rising pace, and counted the drop week twice: up to 18 points of
  "surplus" for a user on pace (found in review). Below 100 the note under the
  table says it is the peak versus the target and points to `report` (found in
  review).
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
one id; the largest usage wins. Weight =
`input + 1.25 cache_creation + 0.1 cache_read + 5 output` (API price ratios
relative to fresh input); only the distribution over the week matters. Each
bin's mean is over *every* hour of the period, zeros included (thevibeworks
averages per active day, which runs high), then lightly smoothed along the
hour axis. A short history is blended toward uniform:
`prior = mix x shape + (1 - mix) x uniform`, with
`mix = min(1, covered_days / 14, active_days / 4)`; `covered_days` starts
where 5% of the cost is behind, so one stray old message does not buy trust
(found in review). The prior strength `k` moves from 0.25 to 1 with `mix`.
Found on live data: one hour of transcripts otherwise made a three-hour spike.
It misses claude.ai and Desktop chat; the percent observations correct that
over time. For that, the prior fades toward uniform with the profile half-life
from its `built_at`, in shape (`a x prior + (1 - a) x uniform`,
`a = 0.5 ** (age / 14 d)`) and in strength (trust times `a`). Hours that went
idle since then are only seen through overnight gaps, learned at low
confidence and spread by the prior's own shape. So a prior of constant
strength kept Mon-Fri 9-13 at 20% of the week 8 weeks after a move to 14-23
(found in review). `ctempo profile` prints the prior's age and its weight now
(trust times `a`) beside the trust it was built with. It printed the
build-time trust for good: "trusted 100%" 8 weeks on (found in review). Under
`profile.override` the transcript prior is unused, and `ctempo profile` says
so; it printed the prior's faded weight there too (found in review). With an
override and no transcript prior, `ctempo profile` names the override as the
prior and `doctor` no longer warns "no transcript prior, run
`ctempo bootstrap`" (found in review). Under mode `replace` the learned hours
are unused as well, and `ctempo profile` and `doctor` say so instead of
"folded in" (found in review). `ctempo bootstrap` builds a fresh one.
`install --apply` runs the bootstrap too (`--no-bootstrap` skips it).

**D-15. Weekly numbers.** As in the brief, with these details: `E` is
normalized by the week's total share (DST weeks have 167 or 169 local hours).
After a drop reset (D-10) the instance starts at the drop. The elapsed share,
the on-pace level (`target x (1 - E / span)`, `span` the instance's own share
of the week) and the whole-window fallback count from there, while `E`,
intensity and the projection keep the full week's normalization. Measured from
`resets_at - 7 d`, a user exactly on pace for the new window read 38 points
behind for days. In its first hour, the whole-window fallback divided the new
level by the old week's elapsed share: `×9+▲ →2%` (found in review). Normal
instances are unchanged. Intensity uses the last 24 *awake* hours, walking
back through nights and weekends, and crosses into the previous weekly window
when needed (spend = previous window's peak minus its value at the lookback
start, plus the current window from 0). The previous window is the one whose
reset is nearest the current window's start, never one that did not end
(`shift`) or that a drop reset replaced (`drop`). Both drop windows end at the
same reset, give or take the seconds `resets_at` wobbles, so the nearer one
was picked by chance and could be the frozen one, which lost the spend since
the drop (found in review). The value at the lookback start is the running max
of readings up to it. A FIRST reading (D-06) never fixes that start: with no
FRESH reading at or before it, the count starts at the first FRESH one (a
stale level re-rendered at install showed a red "dry" for a day, depending on
which session rendered first). When the start falls in a gap between readings
longer than `max_gap_hours`, the gap's rise is spread over it by profile
share, as in learning (D-13), and only the part after the start counts (a
weekend of claude.ai use was charged to the last 24 hours). When the lookback
reaches into a previous window that reached 100%, the hours from its cap to
the new window's start are left out of the expected share, since nothing could
be spent there. The cap is that window's first FRESH or FIRST reading at 100,
an upper bound. Two hours after a Thursday reset, a user blocked since Tuesday
read "on pace" and capped again (found in review). The rest of the previous
window after its last reading still counts as no spend: a claude.ai-only last
day and an idle one leave the same readings, so spreading that tail by the
profile would trade one error for the other. Fallbacks when the lookback holds
less than 1% of a week's expected spend, or when it was cut short at the first
reading and has not seen a whole-percent step yet (that zero is no news, not
idle): the whole window so far (unless it is still at 0 after a cut lookback,
which is no news either; it showed `×? →0%`), then the typical week
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
`min_free` points about to expire below its target (max(0, target -
projected): a target below 100 keeps a reserve, and green below the target
invited spending it, found in review), never while the 5h burn rate is unknown
(D-11), whatever `min_free` (0 included), and never for 0 points. Times
(resets, dry times) are rounded to the nearest minute: a window keeps the
`resets_at` of its first reading (D-08), and when that one wobbled 2 s early
the line showed `@13:59` and `@Thu 08:59` for the whole window (found in
review); a wobble of 30 s or more still shows a minute early. At target 100
every slow down runs dry, so yellow was unreachable at the defaults until red
was made to need both (found in review). `NO_COLOR` and `TERM=dumb` disable
color; `display.symbols: "ascii"` swaps the glyphs. Output is written as UTF-8
bytes whatever the locale. Config values of the wrong type or out of range
fall back to the default or are clamped at load (`config.RANGES`), so a typo
never breaks the line. When the line would exceed `display.max_width` (80),
optional parts are dropped in order: 5h reset time, weekly delta, weekly reset
time, then projections. If it still does not fit, it is cut at the width with
`…` (`...` in ascii mode) and each part keeps its tone; the bare line and the
waiting message use the configured separator and are fitted the same way. The
statusline never prints a traceback: errors go to `~/.claude-tempo/errors.log`
(64 KB, one rotation) and the line degrades to "ctempo: error" or a bare line
from the payload.

**D-17. Composing with an existing statusLine.** `install` keeps the old
command inside ours (`--compose '<old command>'`, shell-quoted), so
`settings.json` shows exactly what runs. At render time the old command starts
first, in its own process group, with the same stdin, and runs in parallel
with ctempo's own work; its output comes first and our segment is appended to
its last line (two spaces) or put on a new line when the joined line would not
fit where Claude Code draws it. Its prompt footer pads the line by 2 columns a
side, so the room is `COLUMNS - 4`, counted in display columns: CJK characters
and most emoji take 2, combining marks and joiners take 0, and VS16 makes a
narrow symbol 2. Measured as `COLUMNS` code points, a joined line in the last
4 columns, or one with emoji in it, lost its weekly reset to Claude Code's `…`
(found in review). A nonzero `statusLine.padding` takes 2 more columns per
unit; ctempo does not read it. Its output is complete once it has exited and
its stdout has closed or stayed quiet for 500 ms (Claude Code waits the same
for a hook's remaining output); a background job it started may keep stdout
open, and is left running. Timeout 1.5 s (`compose.timeout_ms`, counted from
ctempo's start): a command still running then is killed with its children and
only our segment shows; one that already exited keeps the output that arrived.
`CTEMPO_COMPOSED=1` stops recursion. `--replace` replaces instead.
`refreshInterval` becomes 60, or stays lower if the old one was lower.

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
`settings.json.ctempo-backup-YYYYmmdd-HHMMSSZ` (UTC), then writes atomically
(tmp + rename, symlinked settings keep the link). Only `statusLine` changes;
the file's indentation, line endings (CRLF too), trailing newline and mode are
kept. What was there before is recorded in `~/.claude-tempo/install.json`, one
record per absolute settings path, saved *before* the settings file is written
(a crash later still leaves uninstall what it needs). A statusLine changed
since the last install becomes the new original, and so does a command the
user put in ctempo's `--compose` argument, also when there was no statusLine
before ctempo (then without the `refreshInterval` ctempo added; uninstall
restores it, and a repin or `--replace` records it first, also into a record
rebuilt after install.json was lost; without a record it wins over a backup,
at a repin and at uninstall alike, and the `--compose` of a newer backup that
holds ctempo's command, as the one `--replace` takes, wins over an older
backup from before ctempo, or stands in for one, then with the statusLine's
other keys as ctempo ran it); ctempo's own command is never recorded or
restored as the original (a pre-ctempo backup, where a blank one means there
was no statusLine, or `--compose` is used instead). Backups are created
exclusively and never overwritten. `uninstall` asks which file when several
are installed and never creates a missing file. Records are matched by real
path, so a symlinked settings file is one record; a version-1 record with a
relative path is recovered from its backup's name, and one that version 1
already uninstalled is dropped. If the plugin commands fail during uninstall
(claude missing or hanging), the mod record and `runtime.json` are kept and
uninstall exits 1 so a rerun retries (the warning says the mod may still be
installed). `uninstall --no-mod` keeps them too, and the launcher (the kept
mod reads `runtime.json`); they go with the mod. When install.json has no mod
record (the data folder was deleted, or another `CTEMPO_HOME`) but the user
settings file lists ctempo's plugin or its marketplace, uninstall rebuilds the
record from that file and says so, then removes the mod as usual (`--no-mod`
keeps it); with no snapshot left, that file is tidied by keys (a key that
holds only ctempo's entry is not the user's). A version-1 mod record names no
user file: its commands edited the default one, so that file's record is kept
through a run that leaves the mod, and goes with the mod. When claude is not
on PATH at install, install prints the plugin commands for the user to run and
still records the mod (as manual, with the user-file snapshot), so uninstall
runs the reverse commands. A mod stays manual until ctempo sees the plugin CLI
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
ordered by their time and sequence number, never as strings: a name ending in
Z is UTC and sorts after every older one without it, which an older version
named in local time; local time, read in the zone in effect at the time,
orders only those older names among themselves (local time goes back after a
move west or at the fall-back hour, and a newer backup then sorted first,
found in review twice: the second time, an older version's local-time backup
sorted after a newer Z one). `uninstall` restores the original `statusLine`
(or removes it), removes `enabledPlugins` / `extraKnownMarketplaces` if they
were not the user's (absent before, or holding only ctempo's own entry because
the commands were run by hand; a key that ctempo next finds gone, as after the
user deleted or blanked the file, stops counting as theirs, and so does one
holding only ctempo's entry for a manual mod whose commands the user ran,
unless the key was empty at the mod's snapshot. A user's own empty key with
the mod in keeps counting, also after the user added and removed other
plugins, and also when the user ran the printed commands for a manual mod: the
plugin CLI leaves `{}` after removing its last plugin, so many files hold one,
and uninstall used to remove it, found in review 3 times. When the user
deletes or blanks the user file and then runs the printed commands by hand
(after ctempo ran them, or for a manual mod whose snapshot held the key
empty), ctempo cannot tell those keys from the user's own, and they may stay
empty) and the plugin CLI left them empty, and when the result equals the
backup (a blank backup counts as `{}`, as on install) it writes the backup's
bytes, so an untouched file comes back byte for byte (the test suite checks
four formatting styles and blank files). The mod is installed with
`claude plugin marketplace add <repo>` and
`claude plugin install ctempo@claude-tempo` (user scope); `--no-mod` skips it.
`--bin-dir ~/.local/bin` adds a `ctempo` launcher with the pinned interpreter.
A later install rewrites it with the new pin without `--bin-dir` (a `--python`
repin, a moved clone), unless the user deleted it. A launcher written in
another `--bin-dir` replaces it: the earlier one is removed while it is still
ctempo's, so uninstall removes the only one there is (found in review). A
`--bin-dir` that names the same directory another way (another case on a
case-insensitive macOS disk, a bind mount) holds the same file, which stays:
install used to remove the launcher it had just written (found in review).

## The scavenger hook

**D-20. `ctempo check`.** Exit 0 and `"ok": true` when spare quota can be
harvested now, 1 otherwise, JSON on stdout either way. A 5h window is
harvestable when it is in its tail (`--tail 5h=60m`), its projected unused
below its target (max(0, target - projected), `spare` in the JSON) is at least
`--min-free` (default 10) and at least that much is free below the target now
(target - used), and the weekly window would not run dry or exceed its target
if that spare were spent (priced with the exchange rate; while that is
unknown, at its clamp maximum of 50 weekly points per 5h window, and the JSON
says `"exchange_rate_assumed": true`, only when a positive 5h leftover was
priced that way. A leftover of 0 costs 0 at any rate, and the reason is then
"the weekly budget is already tight", without a price; it said "priced at 50"
next to nothing left, found in review). The weekly window is harvestable when
it is in its tail (`week=2d`) with at least `min-free` projected to expire
below its target and the 5h window has room below its own target, both now and
at the current burn rate (projected unused >= `min-free`): a scavenger must
not starve interactive work. What is spare is measured against each window's
target, not 100, as for green (D-16); at target 100 nothing changes. A window
at or above a target below 100, or with a slow down verdict, is never
harvestable, and the reasons say so ("5h window is at or above its 90%
target", "weekly window is spending faster than its 85% target allows (slow
down)"). check used to say go on the reserve that a target below 100 keeps
while the line was yellow, and one JSON both refused a 5h leftover as "already
tight" and said go on the week's surplus (found in review). `projected_unused`
and `free_now` stay out of 100; each window also reports its `target` and
`spare`, and a go reason adds "below its N% target" when the target is below
100; targets print as set (99.5 printed as "100%", which made "below its 100%
target", found in review). While the 5h burn rate is not known yet (D-11), the
5h leftover is unknown: `projected_unused` and the weekly cost are null,
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
places, backups sorted as strings); a seventh confirmed 9 (10 reports: spare
quota measured against 100 instead of the target, a bogus later reset that a
maxed or idle session never undid, concurrent first opens racing the switch to
WAL, the composer waiting for EOF behind a background job, 0% shown when a
write failed at a rollover, and four install cases) and rejected 1, the
fuzzer, extended with a `runtime.json` check after every step, a deleted data
folder, `--compose` edits and the plugin commands run by hand, found 2 more,
and a fixer's notes 1 more (the user's own empty plugin key removed by
uninstall); an eighth confirmed 15 and rejected none (a capped previous week
counted as expected spend in the weekly lookback, the old window's tail before
a reset never learned, a transcript prior that never faded, a drop reset paced
from `resets_at - 7 d`, a weekly reset moved later counted as a finished week,
the one-reading undo flipped by a shared session id and leaving another
session's pending reading behind, reset times shown a minute early, a 5h
leftover of 0 priced at the assumed exchange rate, fractional targets printed
as whole numbers, two `ctempo profile` texts, and three install cases: a
launcher that kept its first pin, backups named in local time, and the user's
own empty plugin key after other plugins came and went); a ninth confirmed 11
and rejected 1 (a cap first shown by a new session's first reading learned as
idle, the lookback after a drop week reading the window the drop replaced, a
drop week's window counted as a typical week, the points-vs-pace table pacing
a drop week from `resets_at - 7 d` and counting it twice, the prior's
build-time trust printed for good, a shared session id still flipping the line
to a dead window, a light user's early reset taken for a moved one, ctempo's
segment cut off in a composed line, and three install cases: a manual mod's
empty plugin key, an older version's local-time backup names, and a
`--bin-dir` alias); a tenth, over those fixes only, confirmed 3 and rejected 1
(a maxed session's bogus reset that began after its last reading no longer
undone, an early-rollover week counted as a typical week, and the prior's
weight printed under `profile.override`); an eleventh, over those 3, confirmed
2 that were older than them (two bogus later resets in a row from a maxed
session not undone, and `ctempo profile` and `doctor` asking for a transcript
prior that an override leaves unused) and rejected none; a twelfth, over those
2, confirmed 2 (the chained undo walking across a real early reset, introduced
by the eleventh pass's fix, and learned hours called "folded in" under mode
`replace`) and rejected none; a thirteenth, a model-based fuzzer over reset
detection (traces of true windows and Claude Code processes, shared session
ids included, replayed through the statusline path, 14,000 sequences) whose
findings verifiers checked, confirmed 5 and rejected none (2 idle processes on
1 session id faking a drop reset, raising the window a drop opened back to the
old level, rolling back a reset moved earlier, and undoing a reset moved later
or an early reset, and a reset moved earlier that a maxed or idle session
never got taken); a fourteenth, over those rules, confirmed 5 and rejected
none: the withdrawal deleting a real pending drop (for a busy process with an
idle twin, and for the statusline and mod of one session) is fixed, and 3 more
cases for processes sharing a session id are documented limits (D-06). 139 are
fixed, each with a regression test that was checked to fail on the code before
its fix. Details in the commit messages from "Fix review findings" on.