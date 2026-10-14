# ctempo: plan

Goal: in the main working view, for both Claude usage windows (5-hour and
weekly), show spend speed versus affordable speed, a verdict and a forecast
at reset; keep a local history for analytics and a future scavenger.

## Steps

1. Ground truth from the official docs and the local environment.
   Output: `notes/docs.md`. Done.
2. Read-only study of seven reference repos in parallel subagents.
   Output: `notes/reference.md`. Done.
3. Core in Python 3 standard library (`ctempo/`):
   store (SQLite WAL) and ingest with guards; 5-hour math; hour-of-week profile;
   weekly math; renderer; statusline runner with composition; CLI commands;
   transcript bootstrap; install/uninstall. Done.
4. Mod (`mods/ctempo`, TypeScript, thin) plus a local marketplace
   (`.claude-plugin/marketplace.json`). Done.
5. Tests: `python3 -m unittest discover -s tests -t .` (Python 3.8 to 3.13) and
   `claude plugin test mods/ctempo`. Synthetic series with hand-computed values.
   Done.
6. Benchmark: `scripts/bench_statusline.py` (50 runs, seeded history). Done:
   p95 about 47 ms.
7. Install for real (dry run, then `--apply`), install the mod from the local
   marketplace, verify samples arrive from a live session, `ctempo doctor`.
8. Adversarial review of the code, then README and final report.

## Constraint that shaped the run

This session ran in a cloud container, not on the MacBook. Everything that
touches "the real machine" (install, live samples, transcript bootstrap) was
done against the container's own `~/.claude`, and the Mac install is one
command (README, "Install"). See DECISIONS D-01.
