// ctempo band: one quiet row above the prompt, for the Desktop Code tab where
// no statusLine renders. Each window is a dim name, a small bar (used, then a
// forecast to the reset), the percent and, only when there is something to
// say, a few words; a reset when the band is wide. Details (d) adds a
// sentence per window.
//
// Thin on purpose: all the math and all the words come from the Python CLI.
// The mod hands it the session's rate limits in the statusLine payload shape
// and draws what it prints. It is also the collector where no statusLine
// runs: each call records a sample (source "mod").

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionRateLimit, Timer } from 'claude-code'

import type { Phrase, View, Win } from '../types'

const view = atom({ plugin: 'ctempo', key: 'view' } as const, null)
const details = atom({ plugin: 'ctempo', key: 'details' } as const, false)

const RUN_TIMEOUT_MS = 10_000
const REFRESH_MS = 60_000
// How often the mod looks for a new reading: this session's own, or another
// chat's (the CLI touches CTEMPO_HOME/changed). Both are cheap; Python runs
// only when one moved.
const POLL_MS = 5_000
// The interpreter flags of a runtime.json written before it listed them.
const DEFAULT_ARGS: readonly string[] = ['-I', '-S']

type Runtime = { python: string; args: readonly string[]; entry: string; home: string; windows: boolean }

// Desktop paints theme keys; the terminal maps them to its palette. Tones of
// the classic line, then the bars line's words and bar parts.
const TONE: Record<string, { dimColor?: boolean; color?: string }> = {
  dim: { dimColor: true },
  plain: {},
  opportunity: { color: 'success' },
  warn: { color: 'warning' },
  alert: { color: 'error' },
  text: {},
  name: { dimColor: true },
  inactive: { dimColor: true },
  success: { color: 'success' },
  warning: { color: 'warning' },
  error: { color: 'error' },
  used: { color: 'claude' },
  forecast: { dimColor: true },
  track: { dimColor: true },
  idle: { dimColor: true },
  over: { color: 'error' },
  spare: { color: 'success' },
}

// The statusLine stdin shape: windows keyed by kind, resets_at in epoch seconds.
export function toPayload(sessionId: string, limits: readonly SessionRateLimit[]) {
  const rateLimits: Record<string, { used_percentage: number; resets_at?: number }> = {}
  for (const limit of limits) {
    const resetsAt = limit.resetsAt ? Date.parse(limit.resetsAt) : Number.NaN
    rateLimits[limit.kind] = {
      used_percentage: limit.percentUsed,
      ...(Number.isFinite(resetsAt) ? { resets_at: Math.floor(resetsAt / 1000) } : {}),
    }
  }
  return {
    session_id: sessionId,
    ...(limits.length > 0 ? { rate_limits: rateLimits } : {}),
    kinds: limits.map((limit) => limit.kind),
  }
}

// What a refresh compares: the readings, not the objects that carry them.
function limitsKey(limits: readonly SessionRateLimit[]): string {
  return JSON.stringify(limits.map((limit) => [limit.kind, limit.percentUsed, limit.resetsAt ?? null]))
}

// Module state: a reload starts it over, which is what we want.
let runtime: Runtime | null = null
let running = false
let timer: Timer | null = null
let periodMs = REFRESH_MS
// A refresh asked for while one runs: the newest limits win, null = re-read.
let queued: readonly SessionRateLimit[] | null | undefined
// What the last run saw: this session's limits, and the stamp's time.
let seenLimits: string | null = null
let seenStamp: number | null = null

// The user's home as Python's os.path.expanduser finds it, and whether
// Windows path rules apply.
export type UserHome = { windows: boolean; home: string | undefined }

// No $ API names the OS; Windows sets OS=Windows_NT system-wide. There Python
// ignores HOME (Git Bash sets one of its own) and takes USERPROFILE, else
// HOMEDRIVE+HOMEPATH; an empty value counts as unset.
async function userHome($: EngineInterface): Promise<UserHome> {
  if ((await $.env.get('OS')) !== 'Windows_NT') return { windows: false, home: await $.env.get('HOME') }
  const profile = await $.env.get('USERPROFILE')
  if (profile) return { windows: true, home: profile }
  const homePath = await $.env.get('HOMEPATH')
  return { windows: true, home: homePath ? `${(await $.env.get('HOMEDRIVE')) ?? ''}${homePath}` : undefined }
}

// One separator between the two, as os.path.join puts it.
function join(base: string, name: string, windows: boolean): string {
  const ends = windows ? /[\\/]$/ : /\/$/
  return ends.test(base) ? `${base}${name}` : `${base}${windows ? '\\' : '/'}${name}`
}

// A UNC path (\\server\share): $.fs.read refuses most of them.
function isNetwork(path: string, windows: boolean): boolean {
  return windows && /^[\\/]{2}/.test(path)
}

// The drive of a Windows path as ntpath.splitdrive reads it: `C:`, or
// `\\server\share`; '' when it has none.
function driveOf(path: string | undefined): string {
  return path?.match(/^(?:[A-Za-z]:|[\\/]{2}[^\\/]+[\\/][^\\/]+)/)?.[0] ?? ''
}

// Git Bash's spelling of a drive path: `/c/Users/x` is `C:/Users/x`, and `/d`
// is `D:/` (a slash, one ASCII letter, then a slash or the end), as the CLI's
// config.windows_home reads it. Anything else is returned as given.
export function fromGitBash(path: string): string {
  const drive = /^\/([A-Za-z])(?:\/|$)/.exec(path)?.[1]
  return drive ? `${drive.toUpperCase()}:/${path.slice(3)}` : path
}

// The same rules as the CLI: an empty CTEMPO_HOME counts as unset, a leading
// `~` is the user's home, and a relative path is taken from it, never from the
// session's cwd (Claude Code copies settings.json env values as written). On
// Windows `~\` works like `~/`, Git Bash's `/c/Users/x` is `C:/Users/x`,
// `C:\`, `C:/` and `\\server\share` are absolute, a path rooted with a
// backslash and no drive (`\x`) takes the drive of the user's home, as
// Python's ntpath.join does, and `C:x` (relative to that drive's current
// folder) is refused. So is any other path rooted with a slash and no drive
// (`/x`): Git Bash, which runs the statusLine, rewrites it to a folder under
// its own install before Python sees it, so the band and the line would use
// different folders. Its message names `/x` on the home's drive, the folder
// older versions used. The CLI refuses both with the same messages
// (config.windows_home).
export function resolveHome(value: string | undefined, user: UserHome): string {
  const { windows, home } = user
  const given = windows && value ? fromGitBash(value) : value
  if (given && (windows ? /^(?:[A-Za-z]:[\\/]|[\\/]{2})/ : /^\//).test(given)) return given
  if (windows && given && /^[A-Za-z]:/.test(given)) {
    throw new Error(`CTEMPO_HOME ${given} is relative to a drive's current folder; use an absolute path`)
  }
  if (windows && given?.startsWith('/')) {
    throw new Error(
      `CTEMPO_HOME ${given} has no drive; Git Bash rewrites such a path to a folder under its own install. ` +
        `Write it with a drive, e.g. ${driveOf(home) || 'C:'}${given}, or as ~/...`,
    )
  }
  if (windows && given && /^\\/.test(given)) return `${driveOf(home)}${given}`
  const tilde = windows ? /^~(?:[\\/]|$)/ : /^~(?:\/|$)/
  if (given?.startsWith('~') && !tilde.test(given)) {
    const forms = windows ? '~, ~/ and ~\\' : '~ and ~/'
    throw new Error(`CTEMPO_HOME ${given}: only ${forms} are expanded; use an absolute path`)
  }
  if (!home) {
    const name = windows ? 'USERPROFILE' : 'HOME'
    throw new Error(`${name} is unset: set CTEMPO_HOME to an absolute path, or run \`ctempo install --apply\``)
  }
  if (!given) return join(home, '.claude-tempo', windows)
  const rest = given.replace(windows ? /^~[\\/]?/ : /^~\/?/, '')
  return rest ? join(home, rest, windows) : home
}

async function loadRuntime($: EngineInterface): Promise<Runtime> {
  if (runtime !== null) return runtime
  const user = await userHome($)
  const home = resolveHome(await $.env.get('CTEMPO_HOME'), user)
  let text: string
  try {
    text = await $.fs.read(join(home, 'runtime.json', user.windows))
  } catch (error) {
    if (!isNetwork(home, user.windows)) throw error
    const why = error instanceof Error ? error.message : String(error)
    throw new Error(`cannot read runtime.json from network path ${home}: set CTEMPO_HOME to a local folder (${why})`)
  }
  // A byte order mark (a Windows editor's) would make JSON.parse throw.
  const parsed = JSON.parse(text.replace(/^﻿/, '')) as { python?: unknown; args?: unknown; entry?: unknown }
  if (typeof parsed.python !== 'string' || typeof parsed.entry !== 'string') {
    throw new Error('runtime.json has no python/entry; run `ctempo install --apply`')
  }
  const args = parsed.args === undefined ? DEFAULT_ARGS : parsed.args
  if (!Array.isArray(args) || !args.every((arg) => typeof arg === 'string')) {
    throw new Error('runtime.json args is not a list of strings; run `ctempo install --apply`')
  }
  runtime = { python: parsed.python, args, entry: parsed.entry, home, windows: user.windows }
  return runtime
}

// The stamp the CLI touches when a reading changes what is shown
// (config.stamp_path): its time, and the run time written in it. Absent, 0.
async function stamp($: EngineInterface, rt: Runtime): Promise<{ mtime: number; ts: number }> {
  const path = join(rt.home, 'changed', rt.windows)
  const mtime = await $.fs
    .stat(path)
    .then((s) => s.mtimeMs)
    .catch(() => 0)
  if (mtime === 0) return { mtime, ts: Number.NaN }
  const ts = await $.fs
    .read(path)
    .then((text) => Number.parseFloat(text))
    .catch(() => Number.NaN)
  return { mtime, ts }
}

// The CLI's mod.refresh_s, clamped; re-arms the timer when it changes.
function rearm($: EngineInterface, seconds: unknown) {
  const s = typeof seconds === 'number' && Number.isFinite(seconds) ? seconds : 60
  const ms = Math.min(3600, Math.max(15, s)) * 1000
  if (ms === periodMs || timer === null) return
  timer.cancel()
  periodMs = ms
  timer = $.clock.every(periodMs, () => {
    void refresh($)
  })
}

const isNum = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x)
const isStr = (x: unknown): x is string => typeof x === 'string'

function isPhrase(x: unknown): x is Phrase {
  const p = x as Phrase
  return (
    typeof p === 'object' && p !== null && isStr(p.pre) && isStr(p.num) && isStr(p.post) && isStr(p.narrow) &&
    isStr(p.tone) && typeof p.attention === 'boolean' && typeof p.calm === 'boolean'
  )
}

// The CLI's windows, when every one is whole; else the text line is drawn.
function windowsOf(x: unknown): Win[] | null {
  if (!Array.isArray(x) || x.length !== 2) return null
  const ok = x.every((w: Win) => {
    return (
      typeof w === 'object' && w !== null && (w.id === 'h5' || w.id === 'wk') && isStr(w.name) && isStr(w.short) &&
      isStr(w.kind) && (w.pct === null || isNum(w.pct)) && isNum(w.used) &&
      (w.projected === null || isNum(w.projected)) && (w.reset === null || isStr(w.reset)) &&
      (w.phrase === null || isPhrase(w.phrase)) && isStr(w.sentence)
    )
  })
  return ok ? (x as Win[]) : null
}

async function runOnce($: EngineInterface, limits: readonly SessionRateLimit[] | null) {
  const rt = await loadRuntime($)
  const current = limits ?? (await $.session.usage()).rateLimits
  const payload = toPayload(await $.session.id(), current)
  const before = await stamp($, rt)
  // Seen even when the run fails: the poll must not retry a failing run every
  // 5 s; the refresh timer does, at its own pace.
  seenLimits = limitsKey(current)
  seenStamp = before.mtime
  const result = await $.process.run(
    [rt.python, ...rt.args, rt.entry, 'statusline', '--source', 'mod', '--format', 'json'],
    { stdin: JSON.stringify(payload), timeoutMs: RUN_TIMEOUT_MS, env: { CTEMPO_HOME: rt.home } },
  )
  if (result.exitCode !== 0 || result.stdout.trim() === '') {
    // On Windows a missing interpreter does not fail to start: the spawn falls
    // back to cmd.exe, which says only "...operable program or batch file."
    // (exit 9009) or "The system cannot find the path specified.". Asked only
    // after a failed run, so a Store alias exists() cannot see is not
    // misreported while it works.
    if (result.exitCode !== 0 && !(await $.fs.exists(rt.python).catch(() => true))) {
      runtime = null // read runtime.json again on the next refresh, after a reinstall
      throw new Error(`Python not found at ${rt.python}: run \`ctempo install --apply\``)
    }
    const lines = result.stderr.trim().split(/\r?\n/)
    // cmd.exe's own message spans 2 lines.
    const why = result.exitCode === 9009 ? lines.join(' ') : lines.pop()
    throw new Error(why || `ctempo exited ${result.exitCode}`)
  }
  const parsed = JSON.parse(result.stdout) as Partial<View>
  if (!Array.isArray(parsed.segments)) {
    throw new Error(typeof parsed.line === 'string' ? parsed.line : 'unexpected output from ctempo')
  }
  // This run's own touch of the stamp is no news; another chat's since is.
  const after = await stamp($, rt)
  if (isNum(parsed.now) && Math.abs(after.ts - parsed.now) < 0.002) seenStamp = after.mtime
  const segments = parsed.segments
  await update($, view, () => ({
    line: typeof parsed.line === 'string' ? parsed.line : '',
    segments,
    surfaces: Array.isArray(parsed.surfaces) ? parsed.surfaces : ['desktop'],
    refresh_s: typeof parsed.refresh_s === 'number' ? parsed.refresh_s : 60,
    windows: windowsOf(parsed.windows),
    updated_ts: isNum(parsed.updated_ts) ? parsed.updated_ts : null,
    now: isNum(parsed.now) ? parsed.now : undefined,
  }))
  rearm($, parsed.refresh_s)
}

async function refresh($: EngineInterface, limits: readonly SessionRateLimit[] | null = null) {
  if (running) {
    queued = limits ?? queued ?? null
    return
  }
  running = true
  let pending: readonly SessionRateLimit[] | null | undefined = limits
  try {
    while (pending !== undefined) {
      queued = undefined
      try {
        await runOnce($, pending)
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        await update($, view, (old) => ({
          ...(old ?? {}),
          line: old?.line ?? '',
          segments: old?.segments ?? [],
          surfaces: old?.surfaces ?? ['desktop'],
          refresh_s: old?.refresh_s ?? 60,
          error: message.slice(0, 200),
        }))
      }
      pending = queued
    }
  } finally {
    running = false
  }
}

// Runs Python only when a reading moved: this session's limits (any change,
// where session.measure waits for a whole point), or the stamp another
// chat's run touched.
async function poll($: EngineInterface) {
  // No runtime yet, or it is being read again after a failure: the refresh
  // timer does that, and reports why.
  const rt = runtime
  if (running || seenLimits === null || rt === null) return
  const limits = (await $.session.usage()).rateLimits
  const now = await stamp($, rt)
  if (limitsKey(limits) !== seenLimits || now.mtime !== seenStamp) await refresh($, limits)
}

// -- drawing ----------------------------------------------------------------

// Widths in the band's cells (bodyColumns), checked in Chromium at 7 px a
// cell from 40 to 140 columns: halves with resets and calm words from 104,
// 64 px bars from 72, no Details button and "Wk" below 52, and below 44 a
// calm window beside one that needs you keeps only its name and percent.
export const layout = (body: number) => ({
  halves: body >= 104,
  W: body >= 72 ? 64 : 44,
  tiny: body < 52,
  bare: body < 44,
})

const SVG_H = 14
const Y = 4.5
const BH = 5
const R = 2.5
const GAP = 1.5
// One style per bar, scoped to its root: Claude's orange in both themes, the
// rest lifted for a dark background. Track, forecast, idle fill and pill are
// color plus opacity, so they tint whatever is behind them.
const STYLE =
  '<style>.ctb-s{--t:#8a867c;--a:#d97757;--r:#d0564c;--g:#5b9c64}' +
  '@media(prefers-color-scheme:dark){.ctb-s{--t:#b4afa4;--r:#e5695e;--g:#74b97c}}' +
  '.ctb-s .ctb-bt{fill:var(--t);opacity:.22}.ctb-s .ctb-bu{fill:var(--a)}.ctb-s .ctb-bf{fill:var(--a);opacity:.32}' +
  '.ctb-s .ctb-bx{fill:var(--r);opacity:.38}.ctb-s .ctb-bm{fill:var(--r)}.ctb-s .ctb-bi{fill:var(--t);opacity:.6}' +
  '.ctb-s .ctb-bp{fill:var(--t);opacity:.45}.ctb-s line{stroke-width:2;stroke-linecap:round}' +
  '.ctb-s .ctb-bw{stroke:var(--r)}.ctb-s .ctb-bd{stroke:var(--g);stroke-dasharray:0 3.5}</style>'

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const r1 = (n: number) => Math.round(n * 10) / 10

// 5 hex digits of FNV-1a: a gradient id of its own per bar.
function hash(s: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h.toString(16).padStart(8, '0').slice(0, 5)
}

// The bar of one window, W px wide: used in orange, then after a hairline gap
// a pale forecast to where it lands at the reset (fading while the estimate
// is rough); red into a wall when it runs out; green dots over what will go
// unused; all red when maxed; grey when idle; a sliding pill while waiting.
export function barSvg(w: Win, W: number): string {
  const X = (p: number) => (Math.min(100, Math.max(0, p)) / 100) * W
  const pill = (x0: number, x1: number, c: string) =>
    `<rect x='${r1(x0)}' y='${Y}' width='${r1(Math.max(x1 - x0, BH))}' height='${BH}' rx='${R}' ` +
    (c.startsWith('url') ? `fill='${c}'/>` : `class='ctb-b${c}'/>`)
  const out = [
    `<svg xmlns='http://www.w3.org/2000/svg' class='ctb-s' width='${W}' height='${SVG_H}' viewBox='0 0 ${W} ${SVG_H}'>`,
    STYLE,
    `<title>${esc(w.sentence)}</title>`,
    pill(0, W, 't'),
  ]
  if (w.kind === 'wait') {
    out.push(
      `<rect y='${Y}' width='16' height='${BH}' rx='${R}' class='ctb-bp'>` +
        `<animate attributeName='x' values='0;${W - 16};0' dur='2.4s' repeatCount='indefinite'/></rect></svg>`,
    )
    return out.join('')
  }
  const u = X(w.used)
  const p = X(w.projected ?? w.used)
  const g0 = u > 0 ? Math.max(u, BH) + GAP : 0
  if (w.kind === 'maxed') out.push(pill(0, W, 'm'))
  else {
    if (w.kind === 'dry') {
      out.push(pill(g0, W, 'x'), `<line x1='${W - 1}' y1='1.5' x2='${W - 1}' y2='12.5' class='ctb-bw'/>`)
    } else if (w.kind !== 'idle' && w.projected !== null && p - g0 >= BH) {
      if (w.kind === 'rough') {
        const id = `ctb${hash(`${w.id}|${w.kind}|${w.used}|${w.projected}|${W}`)}`
        out.push(
          `<linearGradient id='${id}' gradientUnits='userSpaceOnUse' x1='${r1(g0)}' x2='${r1(p)}'>` +
            `<stop offset='.2' style='stop-color:var(--a);stop-opacity:.45'/>` +
            `<stop offset='1' style='stop-color:var(--a);stop-opacity:0'/></linearGradient>`,
          pill(g0, p, `url(#${id})`),
        )
      } else out.push(pill(g0, p, 'f'))
    }
    if (w.kind === 'expire' && W - 2.5 > p + 4) {
      out.push(`<line x1='${r1(p + 4)}' y1='7' x2='${r1(W - 2.5)}' y2='7' class='ctb-bd'/>`)
    }
    if (u > 0) out.push(pill(0, u, w.kind === 'idle' ? 'i' : 'u'))
  }
  out.push('</svg>')
  return out.join('')
}

// "Updated 3 min ago." from the CLI's own clock, so a stale view says so.
export function age(updated: number | null | undefined, now: number | undefined): string {
  if (!isNum(updated) || !isNum(now)) return ''
  const min = Math.max(0, Math.floor((now - updated) / 60))
  if (min < 1) return 'Updated just now.'
  if (min < 60) return `Updated ${min} min ago.`
  const h = Math.round(min / 60)
  return h < 48 ? `Updated ${h} h ago.` : `Updated ${Math.round(h / 24)} days ago.`
}

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    // Off the start path: the first prompt never waits for Python.
    $.clock.after(0, () => {
      void refresh($)
    })
    timer = $.clock.every(periodMs, () => {
      void refresh($)
    })
    $.clock.every(POLL_MS, () => {
      void poll($).catch(() => undefined)
    })
    return started
  })

  on('session.measure', async ($, e, next) => {
    const measured = await next(e)
    if (e.changed.includes('rateLimits') && limitsKey(e.rateLimits) !== seenLimits) {
      void refresh($, e.rateLimits)
    }
    return measured
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const current = await read($, view)
    if (current === null || e.props.hasSurvey || !current.surfaces.includes(e.surface)) {
      return next(e)
    }
    const { Box, Text, Button } = $.ui.resolve(e)
    const wins = current.windows
    let ours
    if (current.error) {
      ours = (
        <Text dimColor wrap="truncate-end">
          ctempo: {current.error}
        </Text>
      )
    } else if (wins && e.surface === 'desktop') {
      const { Svg } = $.ui.resolve(e)
      const L = layout(e.props.bodyColumns)
      const waiting = wins.every((w) => w.pct === null)
      const focus = wins.some((w) => w.phrase?.attention)
      const open = (await read($, details)) && !waiting && !L.tiny
      const toggle = () => update($, details, (v) => !v)
      const row = (
        <Box flexDirection="row" alignItems="center" columnGap="sm">
          <Box flexDirection="row" alignItems="center" flexGrow={1} flexShrink={1} columnGap={L.halves ? 0 : 'md'}>
            {wins.map((w) => {
              const p = w.phrase
              const att = Boolean(p?.attention)
              const dim = focus && !att
              const said = p && (L.halves || !p.calm) ? p : null
              const post = said ? (L.halves ? said.post : said.narrow) : ''
              return (
                <Box
                  key={`ctb-${w.id}`}
                  width={L.halves && !waiting ? '50%' : undefined}
                  flexShrink={1}
                  flexDirection="row"
                  alignItems="center"
                  columnGap="xs"
                >
                  <Text size="footnote" color="inactive">
                    {L.tiny && w.id === 'wk' ? w.short : w.name}
                  </Text>
                  {L.bare && focus && !att ? null : (
                    <Svg source={barSvg(w, L.W)} alt={w.sentence} width={L.W} height={SVG_H} isInteractive />
                  )}
                  {w.pct === null ? null : (
                    <Text
                      size="footnote"
                      tabular
                      weight={dim ? 'regular' : 'medium'}
                      color={dim ? 'inactive' : 'text'}
                    >
                      {`${w.approx ? '≈' : ''}${w.pct}%`}
                    </Text>
                  )}
                  {said ? (
                    <Text
                      size="footnote"
                      wrap="truncate-end"
                      color={said.tone}
                      weight={said.tone === 'inactive' ? 'regular' : 'medium'}
                    >
                      {said.pre}
                      {said.num ? (
                        <Text weight="semibold" tabular color={said.tone === 'inactive' ? 'text' : said.tone}>
                          {said.num}
                        </Text>
                      ) : null}
                      {post}
                    </Text>
                  ) : null}
                  {L.halves && w.reset && w.pct !== null && w.kind !== 'maxed' ? (
                    <Box marginLeft="xs">
                      <Text size="footnote" color="inactive" tabular>
                        {`resets ${w.reset}`}
                      </Text>
                    </Box>
                  ) : null}
                </Box>
              )
            })}
            {waiting ? (
              <Text size="footnote" color="inactive">
                {L.halves ? 'Waiting for the first reading' : 'Waiting for data'}
              </Text>
            ) : null}
          </Box>
          {waiting || L.tiny ? null : (
            <Button key="details" plain dimColor hotkey="d" label={open ? 'Hide' : 'Details'} onPress={toggle} />
          )}
        </Box>
      )
      ours = open ? (
        <Box flexDirection="column">
          {row}
          <Text size="footnote" color="inactive">
            {wins[0]?.sentence ?? ''}
          </Text>
          <Text size="footnote" color="inactive">
            {[wins[1]?.sentence ?? '', age(current.updated_ts, current.now)].filter(Boolean).join(' ')}
          </Text>
        </Box>
      ) : (
        row
      )
    } else {
      // The terminal (when asked for), or an older CLI: the text line.
      ours = (
        <Box flexDirection="row">
          {current.segments.map((segment, index) => (
            <Text key={`s${index}`} wrap="truncate-end" {...(TONE[segment.tone] ?? {})}>
              {segment.text}
            </Text>
          ))}
        </Box>
      )
    }
    // Keep what other mods draw in the band below ours.
    const below = await next(e)
    return below ? (
      <Box flexDirection="column">
        {ours}
        {below}
      </Box>
    ) : (
      ours
    )
  })
}
