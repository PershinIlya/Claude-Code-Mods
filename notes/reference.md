# Reference study (read only; nothing in ./reference was executed)

Seven repos were cloned into `./reference/` (gitignored) and read by parallel
subagents. Everything there was treated as untrusted data. Below: what each one
does that matters for ctempo, and what we took, changed or left.

## SeanLF/ccpool (Go, MIT)

- **5h burn** (`internal/burn`): unweighted least squares over the trailing 30
  minutes of a running-max series, min 3 samples, min 0.08 h span, min 2 point
  climb. No idle handling: the fit ends at the last sample, so an old burst keeps
  a positive slope for half an hour. *Took* the least-squares-on-running-max
  idea; *changed* to 20 minutes and anchored the fit at the window edge and at
  "now" so a finished burst fades out (DECISIONS D-11).
- **Weekly** rate is an endpoint average over the whole window.
- **Parallel sessions**: rows interleave and are reconciled at read time by a
  running MAX; a reading with `resets_at` within 300 s of the newest is the same
  window because "Anthropic's reported epoch wobbles a few seconds between
  renders". *Took* both (D-06, D-08).
- **#52326 guard**: Claude Code sometimes leaks the `resets_at` epoch into
  `used_percentage`; ccpool rejects values `< 0` or `>= 10000` and clamps the rest
  to 100. It also drops `resets_at > now + 30 d` after a real `9999999999` row.
  *Took and tightened*: reject outside [0, 105] (D-09).
- **Profile/rhythm**: binary schedule, weight 1.0 inside wake hours on work days
  and a constant floor 0.15 elsewhere; `rhythm` suggests wake hours from 30 days
  of transcript timestamps (24 hour bins + 7 day bins, no 168-bin table). *Took*
  the 0.15 floor and the override shape (D-13).
- **Statusline**: "dim until it matters" (dim everything, yellow/red only for
  the alarming value), `NO_COLOR` and `TERM=dumb` respected, fixed-width fields.
- **init**: dry run lists planned changes, `--apply` writes
  `settings.json.bak.<epoch>`, tmp+rename, symlink target preserved, insertion
  order kept. It never composes with a foreign statusLine (refuses or replaces)
  and has no uninstall. *Improved* on both (D-17, D-18).

## thevibeworks/claude-code-statusline (bash, MIT)

- Learns a weekday profile (%/day per weekday) and 24 hour-of-day multipliers
  from a monotone envelope of 7d utilization; decay weight `exp(-ln2/14 * age)`
  (14-day half-life), recomputed hourly. Days with zero burn never enter the
  denominator, so the weekday mean is per active day and runs high. *Changed*:
  ctempo averages over every hour of the period, zeros included (D-14).
- `_profile_walk`: hour by hour to the reset at `weekday_rate * hour_mult`, with
  the first 24 h boosted to `max(profile, last 24 h)`. Yields "dry ~Thu 09:00"
  and "lands ~N%"; "~N% will expire · go heavier" when the landing is low.
  *Took* the hour-by-hour walk and both phrasings (D-15, D-16).
- Rest hours: multiplier < 0.25. *Took* as the awake threshold (D-13).
- `pct_per_window`: weekly points per full 5h window, from paired readings
  (publish once the 5h side rose 50 points and the 7d side 3). *Took* as the
  exchange rate in `now`, `report` and `check` (D-23).
- `report`: no ledger file; mined from the log; a window's final value is the
  last sample before its key changes. ctempo stores `windows` rows instead.
- OAuth token refresh and credential rewriting exist in that repo; deliberately
  not studied or copied.

## thevibeworks/ccpace (Python, MIT), docs/data.md

- Reset detection: a newer `resets_at` key is a new window; an older key is a
  stale session (dropped); an unchanged key "proves nothing: they observed a real
  100 -> 0 reset where `resets_at` did not change". A drop counts only when at
  least 2 *consecutive* samples sit below the envelope and the envelope minus the
  minimum low is at least 15 points. *Took* (D-10) and closed its over-count edge
  case (env 50, then 30 and 48 passes): in ctempo every low in the run must itself
  be 15 points down.
- Keys rounded (60 s for 7d, 300 s for 5h); drop samples observed at or after
  their own reset; "unknown is not zero" for slots outside coverage.

## fabioconcina/claumon (Go, MIT): idea for v2

- Polls `/api/oauth/usage` every 120 s. Forecast `F = u_now + r_post * dt` where
  `r_post` shrinks a 30-minute OLS slope toward a prior fitted on past windows
  (normal-normal), with a variance floor from calibration; the 80% interval comes
  from a Monte Carlo of a non-decreasing Gamma process with a rectified Gaussian
  rate. Coverage went from ~25% to ~80% with the variance floor.
- Lessons: window identity must round `resets_at` (it drifts by fractions of a
  second); the prior should depend on hour of week; windows whose end was not
  observed bias the prior low (ctempo's `typical_week` skips them).
- v2 for ctempo: an 80% band on both projections, with ctempo's profile as the
  prior mean instead of claumon's single global rate.

## Astro-Han/claude-pace (bash + jq, MIT)

- Network-free, one jq call; linear pace `used - elapsed share`; ~10 ms on Apple
  Silicon. Shows the stale pre-reset % after a reset and any usage as "ahead" at
  the start of a window. No NO_COLOR, no uninstall.
- Baseline: ctempo cannot match bash+jq at 10 ms in Python (bare `python3 -I -S`
  starts in ~11 ms, json and sqlite3 imports add more), but stays well under the
  100 ms p95 budget while doing far more (D-02).

## NathanAB/statusline-anywhere (TypeScript mod, MIT)

- Runs the user's statusLine command through `$.process.run(['sh','-c',cmd],
  {cwd, stdin, timeoutMs})` on `session.start` + a `$.clock.every` timer +
  `turn.complete`, builds the stdin JSON from `$.session.*` (rate limits from
  `$.session.usage().rateLimits`, ISO `resetsAt` converted to epoch), parses ANSI
  into `Text` segments, draws on `AbovePrompt` except on the terminal.
- Gaps: refreshes arriving during a run are dropped; it always invalidates.
- *Took* the shape. ctempo's mod skips ANSI parsing by asking the CLI for
  tone-tagged JSON, coalesces refreshes, and uses `session.measure` (D-19).

## anthropics/claude-code-playground, claude-code/mods (Apache-2.0)

- Three sample mods as plain `.mjs` hook modules: `token-weather` (reads
  `$.session.usage().context`, draws a one-line band), `blast-radius`,
  `replay-theater`. Conventions: `.claude-plugin/types/` is generated and
  gitignored; marketplace file at `mods/.claude-plugin/marketplace.json` with
  relative sources; render hooks pass through with `next(e)` when idle.
- No tests ship with them; statusline-anywhere's tests show the
  `claude-code/testing` idioms (`mock.clock`, stubbing `process.run` and
  `session.usage` with `on(...)`, `$.ui.mount` + `find`).
