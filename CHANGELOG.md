# Changelog

## 0.1.0 (2026-10)

First public release.

- The 5-hour and weekly pace in the Claude Code status line, a band above
  the prompt in the Claude Desktop Code tab, and the macOS menu bar
  (SwiftBar), on macOS, Linux and Windows.
- Details: a chart per window (what was used, the forecast with its
  uncertainty, earlier forecasts, the week's nights, when it runs out) and a
  few numbers.
- An hour-of-week profile learned from your sessions, a weekly forecast
  that follows it, and forecasts that warn only when the warning holds.
- Optional `/usage` fetch, so usage from claude.ai, Cowork or another
  computer counts too; macOS notifications.
- `pacer install` / `uninstall` with a dry run, backups and a byte-for-byte
  restore; `pacer doctor`.

Before this release pacer was called ctempo. `pacer install --apply` moves
an install made under that name (its data folder, mod and menu bar plugin).
