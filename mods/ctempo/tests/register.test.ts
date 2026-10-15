import type { ProcessRunResult, RenderSurface, SessionRateLimit } from 'claude-code'
import { expect, type MountTarget, mock, type TestBody, test } from 'claude-code/testing'

import { fromGitBash, resolveHome, toPayload } from '../hooks/register'

type StubOn = Parameters<TestBody>[1]
type Run = { argv: readonly string[]; init?: { stdin?: string; timeoutMs?: number; env?: Record<string, string> } }

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

type Options = {
  runtime?: string | null
  // Why the read fails when runtime is null.
  denied?: string
  limits?: SessionRateLimit[]
  env?: Record<string, string>
  run?: () => { value: ProcessRunResult }
}

function stub(on: StubOn, options: Options = {}) {
  const runs: Run[] = []
  const clock = mock.clock(on, { now: NOW })
  mock.env(on, options.env ?? { HOME: '/home/u' })
  const reads: string[] = []
  const runtime =
    options.runtime === undefined
      ? JSON.stringify({ python: '/usr/bin/python3', entry: '/repo/bin/ctempo' })
      : options.runtime
  on('fs.read', (_$, e) => {
    reads.push(String((e as { path?: string }).path))
    return runtime === null ? { deny: options.denied ?? 'ENOENT: no such file' } : { value: runtime }
  })
  on('session.usage', () => ({
    value: { startedAt: NOW - 60_000, context: { window: 200_000 }, rateLimits: options.limits ?? LIMITS },
  }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('process.run', (_$, e) => {
    runs.push(e)
    return options.run?.() ?? ran(JSON.stringify(CLI))
  })
  on('session.start', () => ({ cwd: '/work' }))
  on('session.measure', (_$, e) => ({ changed: e.changed }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['drawn by Claude Code'] }))
  return { runs, clock, reads }
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

// The test kit resolves e.path against the cwd before a stub sees it, which
// on Windows adds a drive letter and turns `/` into `\`: compare the end of
// the path, both sides with forward slashes.
function slashed(path: string | undefined): string {
  return (path ?? '').replace(/\\/g, '/')
}

function ending(suffix: string): RegExp {
  return new RegExp(`${slashed(suffix).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`)
}

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

test('shows why when the CLI fails, instead of a JSON parse error', async ($, on) => {
  const { clock } = stub(on, {
    run: () => ({
      value: {
        exitCode: 2,
        stdout: '',
        stderr: "python3: can't open file '/old/path/bin/ctempo': [Errno 2] No such file or directory\n",
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }),
  })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: /can't open file/ })).toBeDefined()
})

test('an error reply without surfaces still draws on Desktop', async ($, on) => {
  const { clock } = stub(on, {
    run: () => ran(JSON.stringify({ ok: false, line: 'ctempo: error', segments: [{ text: 'ctempo: error', tone: 'dim' }] })),
  })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: 'ctempo: error' })).toBeDefined()
})

test('an empty CTEMPO_HOME counts as unset, and the CLI gets the same home', async ($, on) => {
  const { runs, clock, reads } = stub(on, { env: { HOME: '/home/u', CTEMPO_HOME: '' } })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(slashed(reads[0])).toMatch(ending('/home/u/.claude-tempo/runtime.json'))
  expect(runs[0]?.init?.env).toEqual({ CTEMPO_HOME: '/home/u/.claude-tempo' })
})

// Review 4: settings.json env reaches the mod as written, so a `~` or a
// relative CTEMPO_HOME would otherwise be read from the session's cwd.
test('expands a leading ~ in CTEMPO_HOME and passes the result to the CLI', async ($, on) => {
  const { runs, clock, reads } = stub(on, { env: { HOME: '/home/u', CTEMPO_HOME: '~/.ctempo-moved' } })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(slashed(reads[0])).toMatch(ending('/home/u/.ctempo-moved/runtime.json'))
  expect(runs[0]?.init?.env).toEqual({ CTEMPO_HOME: '/home/u/.ctempo-moved' })
})

test('takes a relative CTEMPO_HOME from HOME, never from the session cwd', async ($, on) => {
  const { runs, clock, reads } = stub(on, { env: { HOME: '/home/u', CTEMPO_HOME: 'data/ctempo' } })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(slashed(reads[0])).toMatch(ending('/home/u/data/ctempo/runtime.json'))
  expect(runs[0]?.init?.env).toEqual({ CTEMPO_HOME: '/home/u/data/ctempo' })
})

test('uses an absolute CTEMPO_HOME as is, even without HOME', async ($, on) => {
  const { runs, clock, reads } = stub(on, { env: { CTEMPO_HOME: '/srv/ctempo' } })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(slashed(reads[0])).toMatch(ending('/srv/ctempo/runtime.json'))
  expect(runs[0]?.init?.env).toEqual({ CTEMPO_HOME: '/srv/ctempo' })
})

test("asks for an absolute path when CTEMPO_HOME names another user's ~", async ($, on) => {
  const { runs, clock, reads } = stub(on, { env: { HOME: '/home/u', CTEMPO_HOME: '~other/ctempo' } })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(reads.length).toBe(0)
  expect(runs.length).toBe(0)
  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: /absolute path/ })).toBeDefined()
})

// Windows: Python takes the home from USERPROFILE (else HOMEDRIVE+HOMEPATH)
// and never from HOME, and `ctempo install` writes runtime.json there.
const WINDOWS = { OS: 'Windows_NT', USERPROFILE: 'C:\\Users\\u' }
const WINDOWS_HOMES: [string, Record<string, string>, string][] = [
  ['USERPROFILE', WINDOWS, 'C:\\Users\\u\\.claude-tempo'],
  ['USERPROFILE, with HOME ignored', { ...WINDOWS, HOME: '/c/Users/other' }, 'C:\\Users\\u\\.claude-tempo'],
  [
    'HOMEDRIVE+HOMEPATH without USERPROFILE',
    { OS: 'Windows_NT', USERPROFILE: '', HOMEDRIVE: 'D:', HOMEPATH: '\\Users\\u', HOME: '/home/u' },
    'D:\\Users\\u\\.claude-tempo',
  ],
  ['~\\ in CTEMPO_HOME', { ...WINDOWS, CTEMPO_HOME: '~\\ctempo' }, 'C:\\Users\\u\\ctempo'],
  ['~/ in CTEMPO_HOME', { ...WINDOWS, CTEMPO_HOME: '~/ctempo' }, 'C:\\Users\\u\\ctempo'],
  ['a relative CTEMPO_HOME', { ...WINDOWS, CTEMPO_HOME: 'data\\ctempo' }, 'C:\\Users\\u\\data\\ctempo'],
  ['an absolute E:\\ CTEMPO_HOME', { ...WINDOWS, CTEMPO_HOME: 'E:\\data\\ctempo' }, 'E:\\data\\ctempo'],
  ['an absolute E:/ CTEMPO_HOME', { ...WINDOWS, CTEMPO_HOME: 'E:/data/ctempo' }, 'E:/data/ctempo'],
  ['a rooted \\x, on the home drive', { ...WINDOWS, CTEMPO_HOME: '\\data\\ctempo' }, 'C:\\data\\ctempo'],
  ['a rooted /x, on the home drive', { ...WINDOWS, CTEMPO_HOME: '/data/ctempo' }, 'C:/data/ctempo'],
  ["Git Bash's /c/Users/u/ctempo", { ...WINDOWS, CTEMPO_HOME: '/c/Users/u/ctempo' }, 'C:/Users/u/ctempo'],
  ["Git Bash's /d/data, on another drive", { ...WINDOWS, CTEMPO_HOME: '/d/data' }, 'D:/data'],
]
for (const [what, env, home] of WINDOWS_HOMES) {
  test(`Windows: finds the ctempo home from ${what}`, async ($, on) => {
    const { runs, clock, reads } = stub(on, { env })
    await $.session.start(start('desktop'))
    await clock.advance(1)
    expect(slashed(reads[0])).toMatch(ending(`${home}/runtime.json`))
    expect(runs[0]?.init?.env).toEqual({ CTEMPO_HOME: home })
  })
}

test('Windows: joins runtime.json with a backslash', async ($, on) => {
  const { clock, reads } = stub(on, { env: WINDOWS })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  // The test kit keeps this path as is on Windows; elsewhere it only puts the
  // cwd before it.
  expect(reads[0]).toMatch(/C:\\Users\\u\\\.claude-tempo\\runtime\.json$/)
})

test('Windows: asks for USERPROFILE, not HOME', async ($, on) => {
  const { runs, clock, reads } = stub(on, { env: { OS: 'Windows_NT', HOME: '/c/Users/u' } })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(reads.length).toBe(0)
  expect(runs.length).toBe(0)
  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: /USERPROFILE is unset/ })).toBeDefined()
})

test('Windows: says why a network CTEMPO_HOME cannot be read', async ($, on) => {
  const { runs, clock } = stub(on, {
    env: { ...WINDOWS, CTEMPO_HOME: '\\\\srv\\share\\ctempo' },
    runtime: null,
    denied: 'a network location is not reached from here',
  })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(0)
  const ui = await $.ui.mount(band('desktop'))
  expect(
    await ui.find({ type: 'Text', text: /network path \\\\srv\\share\\ctempo: set CTEMPO_HOME to a local folder/ }),
  ).toBeDefined()
})

test('resolves CTEMPO_HOME like Python on each platform', async () => {
  const posix = (home: string | undefined) => ({ windows: false, home })
  const windows = (home: string | undefined) => ({ windows: true, home })
  expect(resolveHome(undefined, posix('/home/u/'))).toBe('/home/u/.claude-tempo')
  expect(resolveHome('C:\\x', posix('/home/u'))).toBe('/home/u/C:\\x')
  expect(() => resolveHome('~\\x', posix('/home/u'))).toThrow(/only ~ and ~\/ are expanded/)
  expect(resolveHome(undefined, windows('C:\\'))).toBe('C:\\.claude-tempo')
  expect(resolveHome('~', windows('C:\\Users\\u'))).toBe('C:\\Users\\u')
  expect(resolveHome('\\\\srv\\share\\x', windows('C:\\Users\\u'))).toBe('\\\\srv\\share\\x')
  expect(resolveHome('//srv/share/x', windows('C:\\Users\\u'))).toBe('//srv/share/x')
  expect(resolveHome('\\x', windows('\\\\srv\\share\\Users\\u'))).toBe('\\\\srv\\share\\x')
  expect(resolveHome('\\x', windows(undefined))).toBe('\\x')
  expect(resolveHome('C:\\x', windows(undefined))).toBe('C:\\x')
  expect(() => resolveHome('C:x', windows('C:\\Users\\u'))).toThrow(/relative to a drive's current folder/)
  expect(() => resolveHome('~other\\x', windows('C:\\Users\\u'))).toThrow(/only ~, ~\/ and ~\\ are expanded/)
})

// A CTEMPO_HOME set from Git Bash (export CTEMPO_HOME=/c/Users/me/x) names a
// drive: the CLI reads it so (config.windows_home), and the mod must find the
// same folder, or the band reads a runtime.json the CLI never wrote.
test("Windows: reads Git Bash's /c/Users/x as C:/Users/x, as the CLI does", async () => {
  const windows = (home: string | undefined) => ({ windows: true, home })
  // config.windows_home's results for the same values (tests/test_statusline.py, TildeHome), separators aside.
  const cli: [string, string][] = [
    ['/d/tempo', 'D:\\tempo'],
    ['/C/Users/me/x', 'C:\\Users\\me\\x'],
    ['/c', 'C:\\'],
    ['/e/', 'E:\\'],
    ['/cc/x', 'C:\\cc\\x'],
    ['/1/x', 'C:\\1\\x'],
    ['/c\\x', 'C:\\c\\x'],
    ['/\u00e9/x', 'C:\\\u00e9\\x'],
  ]
  for (const [given, folder] of cli) {
    expect(resolveHome(given, windows('C:\\Users\\me')).replace(/\//g, '\\')).toBe(folder)
  }
  expect(resolveHome('/d/data', windows(undefined))).toBe('D:/data') // a drive of its own needs no home
  expect(fromGitBash('/z/')).toBe('Z:/')
  expect(fromGitBash('/data')).toBe('/data')
  expect(resolveHome('/c/Users/x', { windows: false, home: '/home/u' })).toBe('/c/Users/x') // macOS, Linux: as given
})

test('runs Python with the flags runtime.json lists', async ($, on) => {
  const runtime = JSON.stringify({
    python: 'C:\\Python313\\python.exe',
    args: ['-I', '-S', '-X', 'utf8'],
    entry: 'C:\\repo\\bin\\ctempo',
  })
  const { runs, clock } = stub(on, { env: WINDOWS, runtime })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs[0]?.argv).toEqual([
    'C:\\Python313\\python.exe', '-I', '-S', '-X', 'utf8', 'C:\\repo\\bin\\ctempo',
    'statusline', '--source', 'mod', '--format', 'json',
  ])
})

test('refuses runtime.json args that are not a list of strings', async ($, on) => {
  const runtime = JSON.stringify({ python: '/usr/bin/python3', args: '-I -S', entry: '/repo/bin/ctempo' })
  const { runs, clock } = stub(on, { runtime })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(0)
  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: /args is not a list of strings/ })).toBeDefined()
})

test('reads a runtime.json saved with a byte order mark', async ($, on) => {
  const runtime = `\uFEFF${JSON.stringify({ python: '/usr/bin/python3', entry: '/repo/bin/ctempo' })}`
  const { runs, clock } = stub(on, { runtime })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs[0]?.argv.slice(0, 4)).toEqual(['/usr/bin/python3', '-I', '-S', '/repo/bin/ctempo'])
})

test('follows mod.refresh_s from the CLI', async ($, on) => {
  const { runs, clock } = stub(on, { run: () => ran(JSON.stringify({ ...CLI, refresh_s: 300 })) })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(1)
  await clock.advance(60_000)
  expect(runs.length).toBe(1)
  await clock.advance(240_000)
  expect(runs.length).toBe(2)
})
