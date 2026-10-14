import type { ProcessRunResult, RenderSurface, SessionRateLimit } from 'claude-code'
import { expect, type MountTarget, mock, type TestBody, test } from 'claude-code/testing'

import { toPayload } from '../hooks/register'

type StubOn = Parameters<TestBody>[1]
type Run = { argv: readonly string[]; init?: { stdin?: string; timeoutMs?: number } }

const NOW = 1_791_964_800_000 // 2026-10-14T08:00:00Z
const LIMITS: SessionRateLimit[] = [
  { kind: 'five_hour', percentUsed: 62, resetsAt: '2026-10-14T10:20:00.000Z' },
  { kind: 'seven_day', percentUsed: 48.5, resetsAt: '2026-10-16T09:00:00Z' },
]
const CLI = {
  ok: true,
  line: '5h 62% ×1.4▲ →88% @10:20 · wk 49% +3 ×0.8▼ →96% @Fri 09:00',
  segments: [
    { text: '5h 62% ×1.4▲ →88% @10:20', tone: 'dim' },
    { text: ' · ', tone: 'dim' },
    { text: 'wk 49% +3 ×0.8▼ →96% @Fri 09:00', tone: 'warn' },
  ],
  surfaces: ['desktop'],
  refresh_s: 60,
}

function ran(stdout: string): { value: ProcessRunResult } {
  return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
}

type Options = { runtime?: string | null; limits?: SessionRateLimit[] }

function stub(on: StubOn, options: Options = {}) {
  const runs: Run[] = []
  const clock = mock.clock(on, { now: NOW })
  mock.env(on, { HOME: '/home/u' })
  const runtime =
    options.runtime === undefined
      ? JSON.stringify({ python: '/usr/bin/python3', entry: '/repo/bin/ctempo' })
      : options.runtime
  on('fs.read', () => (runtime === null ? { deny: 'ENOENT: no such file' } : { value: runtime }))
  on('session.usage', () => ({
    value: { startedAt: NOW - 60_000, context: { window: 200_000 }, rateLimits: options.limits ?? LIMITS },
  }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('process.run', (_$, e) => {
    runs.push(e)
    return ran(JSON.stringify(CLI))
  })
  on('session.start', () => ({ cwd: '/work' }))
  on('session.measure', (_$, e) => ({ changed: e.changed }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['drawn by Claude Code'] }))
  return { runs, clock }
}

function band(surface: RenderSurface): MountTarget {
  return {
    plugin: 'ctempo',
    component: 'AbovePrompt',
    surface,
    viewport: { columns: 120, rows: 40, isFullscreen: false },
    props: {
      hasSurvey: false,
      isWorking: false,
      maxRows: 5,
      bodyColumns: 115,
      scroll: { offset: 0, bodyRows: 5 },
      view: {},
    },
  }
}

const start = (surface: RenderSurface) => ({ surface, isInteractive: true, cwd: '/work' })

test('converts rate limits to the statusLine payload', async () => {
  const p = toPayload('s', LIMITS)
  expect(p.session_id).toBe('s')
  expect(p.rate_limits?.five_hour).toEqual({ used_percentage: 62, resets_at: 1_791_973_200 })
  expect(p.rate_limits?.seven_day?.resets_at).toBe(1_792_141_200)
  expect(p.kinds).toEqual(['five_hour', 'seven_day'])
  const empty = toPayload('s', [])
  expect(empty.rate_limits).toBeUndefined()
  expect(toPayload('s', [{ kind: 'spend_limit', percentUsed: 120 }]).rate_limits?.spend_limit).toEqual({
    used_percentage: 120,
  })
})

test('draws the ctempo line above the prompt in Desktop', async ($, on) => {
  const { runs, clock } = stub(on)
  await $.session.start(start('desktop'))
  await clock.advance(1)

  expect(runs.length).toBe(1)
  expect(runs[0]?.argv).toEqual([
    '/usr/bin/python3', '-I', '-S', '/repo/bin/ctempo', 'statusline', '--source', 'mod', '--format', 'json',
  ])
  const input = JSON.parse(runs[0]?.init?.stdin ?? '{}')
  expect(input.session_id).toBe('sess-1')
  expect(input.rate_limits.five_hour).toEqual({ used_percentage: 62, resets_at: 1_791_973_200 })

  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: '5h 62% ×1.4▲ →88% @10:20' })).toMatchObject({
    props: { dimColor: true },
  })
  expect(await ui.find({ type: 'Text', text: 'wk 49% +3 ×0.8▼ →96% @Fri 09:00' })).toMatchObject({
    props: { color: 'warning' },
  })
  expect(await ui.find({ type: 'Text', text: 'drawn by Claude Code' })).toBeDefined()
})

test('leaves the terminal band alone, where the statusLine already shows it', async ($, on) => {
  const { clock } = stub(on)
  await $.session.start(start('terminal'))
  await clock.advance(1)
  const ui = await $.ui.mount(band('terminal'))
  expect(await ui.find({ type: 'Text', text: 'drawn by Claude Code' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^5h/ })).toBeUndefined()
})

test('records new readings when a window moves and refreshes every minute', async ($, on) => {
  const { runs, clock } = stub(on)
  await $.session.start(start('desktop'))
  await clock.advance(1)
  const moved: SessionRateLimit[] = [{ kind: 'five_hour', percentUsed: 63, resetsAt: '2026-10-14T10:20:00Z' }]
  await $.session.measure({ context: { window: 200_000 }, rateLimits: moved, changed: ['rateLimits'] })
  await clock.advance(1)
  expect(runs.length).toBe(2)
  expect(JSON.parse(runs[1]?.init?.stdin ?? '{}').rate_limits.five_hour.used_percentage).toBe(63)
  await $.session.measure({ context: { window: 200_000 }, rateLimits: moved, changed: ['context'] })
  expect(runs.length).toBe(2)
  await clock.advance(60_000)
  expect(runs.length).toBe(3)
})

test('explains itself when ctempo is not installed', async ($, on) => {
  const { runs, clock } = stub(on, { runtime: null })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(0)
  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: /^ctempo: / })).toMatchObject({ props: { dimColor: true } })
})
