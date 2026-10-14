# Live verification (cloud container, 2026-10-14)

This session runs headless behind the Claude desktop app (entrypoint
`remote_desktop`), so it never renders a status line itself. To see ctempo
work on real data, a nested interactive `claude --model haiku "Reply with just
the word ok."` was driven in a pseudo-terminal (`scratchpad/live_pty.py`, not
part of the repo) after `ctempo install --apply`. Its first API response
carried the account's real rate limits (Max plan).

## What the status line printed

Before the first API response (no `rate_limits` on stdin yet):

```
ctempo: waiting for usage data
```

After it (statusLine, terminal):

```
5h 32% ~×0.6▼ dry 11:35 @13:00 · wk 51% -25 ×3.0▲ →67% @Fri 02:00
```

The `~` marks the 5h window-average fallback (one reading so far): 32 points
since the window opened at 08:00, about 28 %/h, mostly this very build session.
Five minutes later, with a second reading, the least-squares fit took over:

```
5h 35% ×0.5▼ dry 11:16 @13:00 · wk 52% -24 ×2.9▲ →69% @Fri 02:00
```

With `mod.surfaces` temporarily set to `["desktop", "terminal"]`, the mod's
band drew the same line above the prompt (the `[-]` is the host's collapse
control):

```
5h 35% ×0.5▼ dry 11:16 @13:00 · wk 52% -24 ×2.9▲ →69% @Fri 02:00[-]
```

## What was recorded

| time (UTC) | window | used | resets_at (UTC) | source |
|---|---|---|---|---|
| 09:08:55 | five_hour | 32 | 13:00:00 | mod |
| 09:08:55 | seven_day | 51 | Fri 02:00:00 | mod |
| 09:13:55 | five_hour | 35 | 13:00:00 | mod |
| 09:13:55 | seven_day | 52 | Fri 02:00:00 | mod |

- The mod (on `session.measure`) wrote first; the status line's identical
  readings a few hundred milliseconds later were deduplicated, as designed.
  `doctor` shows both collectors' last line and the raw `rate_limits` each one
  received:
  `{"five_hour":{"used_percentage":32,"resets_at":1791982800},"seven_day":{"used_percentage":51,"resets_at":1792116000}}`.
- `rateLimits` kinds seen at runtime: `five_hour`, `seven_day` (no
  `spend_limit`: not behind a gateway). They match the bundled type file.
- Both `resets_at` values were whole hours; no wobble showed up in these
  readings (ccpool reports a few seconds of it, so the 300 s bucket stays).
- The nested session reported the parent session's id (it inherits
  `CLAUDE_CODE_SESSION_ID`), an artifact of nesting.

## Real data found a bug

The transcript bootstrap on this container had about one hour of history; the
prior became a spike (3 "awake" hours a week). Fixed: the transcript prior now
ramps in with the days it covers (full at 14 days) and the rest is uniform
(DECISIONS D-14).

## Install round trip on a copy

`notes/roundtrip.txt`: a realistic settings.json (permissions, a Stop hook, an
existing statusLine, env) in a throwaway `HOME`/`CLAUDE_CONFIG_DIR`, real
`claude plugin` commands: install composed with the existing statusLine and
added the marketplace and plugin keys; uninstall removed them and the file
came back byte-identical to both the backup and the original.
