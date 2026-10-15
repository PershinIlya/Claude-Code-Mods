// ctempo band: the same pace line as the terminal statusLine, drawn above the
// prompt where no statusLine renders (the Desktop Code tab).
//
// Thin on purpose: all the math lives in the Python CLI. The mod hands it the
// session's rate limits in the statusLine payload shape and draws the
// tone-tagged segments it prints. It is also the collector where no
// statusLine runs: each call records a sample (source "mod").

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionRateLimit, Timer } from 'claude-code'

import type { Tone, View } from '../types'

const view = atom({ plugin: 'ctempo', key: 'view' } as const, null)

const RUN_TIMEOUT_MS = 10_000
const REFRESH_MS = 60_000
// The interpreter flags of a runtime.json written before it listed them.
const DEFAULT_ARGS: readonly string[] = ['-I', '-S']

type Runtime = { python: string; args: readonly string[]; entry: string; home: string }

// Desktop paints theme keys; the terminal maps them to its palette.
const TONE: Record<Tone, { dimColor?: boolean; color?: string }> = {
  dim: { dimColor: true },
  plain: {},
  opportunity: { color: 'success' },
  warn: { color: 'warning' },
  alert: { color: 'error' },
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

// Module state: a reload starts it over, which is what we want.
let runtime: Runtime | null = null
let running = false
let timer: Timer | null = null
let periodMs = REFRESH_MS
// A refresh asked for while one runs: the newest limits win, null = re-read.
let queued: readonly SessionRateLimit[] | null | undefined

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

// The same rules as the CLI: an empty CTEMPO_HOME counts as unset, a leading
// `~` is the user's home, and a relative path is taken from it, never from the
// session's cwd (Claude Code copies settings.json env values as written). On
// Windows `~\` works like `~/`, `C:\`, `C:/` and `\\server\share` are
// absolute, a rooted path with no drive (`\x`, `/x`) takes the drive of the
// user's home, as Python's ntpath.join does, and `C:x` (relative to that
// drive's current folder) is refused.
export function resolveHome(given: string | undefined, user: UserHome): string {
  const { windows, home } = user
  if (given && (windows ? /^(?:[A-Za-z]:[\\/]|[\\/]{2})/ : /^\//).test(given)) return given
  if (windows && given && /^[A-Za-z]:/.test(given)) {
    throw new Error(`CTEMPO_HOME ${given} is relative to a drive's current folder; use an absolute path`)
  }
  if (windows && given && /^[\\/]/.test(given)) return `${driveOf(home)}${given}`
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
  const parsed = JSON.parse(text.replace(/^\uFEFF/, '')) as { python?: unknown; args?: unknown; entry?: unknown }
  if (typeof parsed.python !== 'string' || typeof parsed.entry !== 'string') {
    throw new Error('runtime.json has no python/entry; run `ctempo install --apply`')
  }
  const args = parsed.args === undefined ? DEFAULT_ARGS : parsed.args
  if (!Array.isArray(args) || !args.every((arg) => typeof arg === 'string')) {
    throw new Error('runtime.json args is not a list of strings; run `ctempo install --apply`')
  }
  runtime = { python: parsed.python, args, entry: parsed.entry, home }
  return runtime
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

async function runOnce($: EngineInterface, limits: readonly SessionRateLimit[] | null) {
  const rt = await loadRuntime($)
  const current = limits ?? (await $.session.usage()).rateLimits
  const payload = toPayload(await $.session.id(), current)
  const result = await $.process.run(
    [rt.python, ...rt.args, rt.entry, 'statusline', '--source', 'mod', '--format', 'json'],
    { stdin: JSON.stringify(payload), timeoutMs: RUN_TIMEOUT_MS, env: { CTEMPO_HOME: rt.home } },
  )
  if (result.exitCode !== 0 || result.stdout.trim() === '') {
    const why = result.stderr.trim().split('\n').pop()
    throw new Error(why || `ctempo exited ${result.exitCode}`)
  }
  const parsed = JSON.parse(result.stdout) as Partial<View>
  if (!Array.isArray(parsed.segments)) {
    throw new Error(typeof parsed.line === 'string' ? parsed.line : 'unexpected output from ctempo')
  }
  const segments = parsed.segments
  await update($, view, () => ({
    line: typeof parsed.line === 'string' ? parsed.line : '',
    segments,
    surfaces: Array.isArray(parsed.surfaces) ? parsed.surfaces : ['desktop'],
    refresh_s: typeof parsed.refresh_s === 'number' ? parsed.refresh_s : 60,
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
    return started
  })

  on('session.measure', async ($, e, next) => {
    const measured = await next(e)
    if (e.changed.includes('rateLimits')) {
      void refresh($, e.rateLimits)
    }
    return measured
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const current = await read($, view)
    if (current === null || e.props.hasSurvey || !current.surfaces.includes(e.surface)) {
      return next(e)
    }
    const { Box, Text } = $.ui.resolve(e)
    const ours = current.error ? (
      <Text dimColor wrap="truncate-end">
        ctempo: {current.error}
      </Text>
    ) : (
      <Box flexDirection="row">
        {current.segments.map((segment, index) => (
          <Text key={`s${index}`} wrap="truncate-end" {...TONE[segment.tone]}>
            {segment.text}
          </Text>
        ))}
      </Box>
    )
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
