import type { ProcessRunResult, RenderSurface, SessionRateLimit } from 'claude-code'
import { expect, type MountTarget, mock, type TestBody, test } from 'claude-code/testing'

import { age, barSvg, fromGitBash, layout, resolveHome, toPayload } from '../hooks/register'
import type { Phrase, Win } from '../types'

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
  // What $.fs.exists answers; left out, nothing answers it (the call rejects).
  exists?: (path: string) => boolean
  // CTEMPO_HOME/changed as the CLI leaves it; absent, the file is missing.
  stamp?: { mtimeMs: number; text: string }
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
    const path = String((e as { path?: string }).path)
    if (/[\\/]changed$/.test(path)) {
      return options.stamp ? { value: options.stamp.text } : { deny: 'ENOENT: no such file' }
    }
    reads.push(path)
    return runtime === null ? { deny: options.denied ?? 'ENOENT: no such file' } : { value: runtime }
  })
  on('fs.stat', (_$, e) => {
    const stamp = options.stamp
    if (!stamp || !/[\\/]changed$/.test(e.path)) return { deny: 'ENOENT: no such file' }
    return { value: { kind: 'file', size: stamp.text.length, mtimeMs: stamp.mtimeMs, isLink: false } }
  })
  on('session.usage', () => ({
    value: { startedAt: NOW - 60_000, context: { window: 200_000 }, rateLimits: options.limits ?? LIMITS },
  }))
  on('session.id', () => ({ value: 'sess-1' }))
  const exists = options.exists
  if (exists) on('fs.exists', (_$, e) => ({ value: exists(e.path) }))
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
  const options: Options = {}
  const { runs, clock } = stub(on, options)
  await $.session.start(start('desktop'))
  await clock.advance(1)
  const moved: SessionRateLimit[] = [{ kind: 'five_hour', percentUsed: 63, resetsAt: '2026-10-14T10:20:00Z' }]
  options.limits = moved
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
  ]
  for (const [given, folder] of cli) {
    expect(resolveHome(given, windows('C:\\Users\\me')).replace(/\//g, '\\')).toBe(folder)
  }
  // Not a drive: refused, as config.windows_home refuses them (TildeHome).
  for (const given of ['/cc/x', '/1/x', '/c\\x', '/\u00e9/x']) {
    expect(() => resolveHome(given, windows('C:\\Users\\me'))).toThrow(/has no drive/)
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

// Review 15: Git Bash, which runs the statusLine on Windows, rewrites a
// CTEMPO_HOME rooted with a slash and no drive to a folder under its own
// install (C:\Program Files\Git\srv\ctempo) before Python sees it, while the
// mod hands Python the value as given: the band and the line would use 2
// folders. The CLI refuses the same values (tests/test_config.py, NoDriveHome).
// The example is on the home's drive, where older versions put the folder.
const PROFILES: [string, Record<string, string>, string][] = [
  ['a profile on C:', WINDOWS, 'C:'],
  ['a profile on D:', { ...WINDOWS, USERPROFILE: 'D:\\Users\\u' }, 'D:'],
]
for (const value of ['/srv/ctempo', '/ctempo', '/tmp/x', '/']) {
  for (const [what, env, drive] of PROFILES) {
    test(`Windows: refuses the drive-less CTEMPO_HOME ${value} with ${what}, naming the fix`, async ($, on) => {
      const { runs, clock, reads } = stub(on, { env: { ...env, CTEMPO_HOME: value } })
      await $.session.start(start('desktop'))
      await clock.advance(1)
      expect(reads.length).toBe(0)
      expect(runs.length).toBe(0)
      const ui = await $.ui.mount(band('desktop'))
      const why =
        `CTEMPO_HOME ${value} has no drive; Git Bash rewrites such a path to a folder under its own install. ` +
        `Write it with a drive, e.g. ${drive}${value}, or as ~/...`
      expect(await ui.find({ type: 'Text', text: ending(why) })).toBeDefined()
    })
  }
}

test("the drive-less example takes the home's drive, else C:", async () => {
  const windows = (home: string | undefined) => ({ windows: true, home })
  const example = (drive: string) =>
    'CTEMPO_HOME /srv/ctempo has no drive; Git Bash rewrites such a path to a folder under its own install. ' +
    `Write it with a drive, e.g. ${drive}/srv/ctempo, or as ~/...`
  expect(() => resolveHome('/srv/ctempo', windows('D:\\Users\\me'))).toThrow({ message: example('D:') })
  const unc = '\\\\srv\\share'
  expect(() => resolveHome('/srv/ctempo', windows(`${unc}\\me`))).toThrow({ message: example(unc) })
  expect(() => resolveHome('/srv/ctempo', windows('\\Users\\me'))).toThrow({ message: example('C:') })
  expect(() => resolveHome('/srv/ctempo', windows(undefined))).toThrow({ message: example('C:') })
  // C:x as the CLI refuses it, word for word (tests/test_config.py, NoDriveHome).
  for (const given of ['C:x', 'c:ctempo', 'D:']) {
    expect(() => resolveHome(given, windows('C:\\Users\\me'))).toThrow({
      message: `CTEMPO_HOME ${given} is relative to a drive's current folder; use an absolute path`,
    })
  }
})

test('the drive-less rule is Windows only, and UNC, \\x and Git Bash drives still work', async () => {
  const windows = (home: string | undefined) => ({ windows: true, home })
  expect(resolveHome('/srv/ctempo', { windows: false, home: '/home/u' })).toBe('/srv/ctempo')
  expect(resolveHome('//srv/share/x', windows('C:\\Users\\u'))).toBe('//srv/share/x')
  expect(resolveHome('/\\srv\\share\\x', windows('C:\\Users\\u'))).toBe('/\\srv\\share\\x')
  expect(resolveHome('\\srv\\ctempo', windows('D:\\Users\\u'))).toBe('D:\\srv\\ctempo')
  expect(resolveHome('/d/srv', windows('C:\\Users\\u'))).toBe('D:/srv')
})

// Review 15: on Windows a pinned python.exe that is gone does not fail to
// start. cmd.exe runs the command instead, and its message names neither
// Python nor the fix: the band said "ctempo: operable program or batch file.".
const GONE = 'C:\\Users\\u\\AppData\\Local\\Programs\\Python\\Python311\\python.exe'
const GONE_RUNTIME = JSON.stringify({ python: GONE, args: ['-I', '-S', '-X', 'utf8'], entry: 'C:\\repo\\bin\\ctempo' })
const NOT_RECOGNIZED =
  `'${GONE}' is not recognized as an internal or external command,\r\n` + 'operable program or batch file.\r\n'
const NOT_FOUND = /Python not found at C:\\Users\\u\\.*python\.exe: run `ctempo install --apply`/

function failed(exitCode: number, stderr: string): () => { value: ProcessRunResult } {
  return () => ({ value: { exitCode, stdout: '', stderr, isStdoutTruncated: false, isStderrTruncated: false } })
}

const MISSING: [string, number, string][] = [
  ['its folder still there (exit 9009)', 9009, NOT_RECOGNIZED],
  ['its folder gone', 1, 'The system cannot find the path specified.\r\n'],
]
for (const [what, exitCode, stderr] of MISSING) {
  test(`Windows: names a missing interpreter, ${what}`, async ($, on) => {
    const { runs, clock, reads } = stub(on, {
      env: WINDOWS,
      runtime: GONE_RUNTIME,
      exists: () => false,
      run: failed(exitCode, stderr),
    })
    await $.session.start(start('desktop'))
    await clock.advance(1)
    expect(runs.length).toBe(1)
    const ui = await $.ui.mount(band('desktop'))
    expect(await ui.find({ type: 'Text', text: NOT_FOUND })).toBeDefined()
    // runtime.json is read again on the next refresh: a reinstall needs no new session.
    await clock.advance(60_000)
    expect(reads.length).toBe(2)
  })
}

test("Windows: keeps cmd.exe's whole message when exists() cannot tell", async ($, on) => {
  // No fs.exists answer: the call rejects, which counts as present (a Store alias it cannot stat).
  const { clock, reads } = stub(on, { env: WINDOWS, runtime: GONE_RUNTIME, run: failed(9009, NOT_RECOGNIZED) })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  const ui = await $.ui.mount(band('desktop'))
  const whole = /not recognized as an internal or external command, operable program or batch file\.$/
  expect(await ui.find({ type: 'Text', text: whole })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Python not found/ })).toBeUndefined()
  await clock.advance(60_000)
  expect(reads.length).toBe(1)
})

// -- the band ---------------------------------------------------------------

function win(over: Partial<Win> & Pick<Win, 'id'>): Win {
  const week = over.id === 'wk'
  return {
    name: week ? 'Week' : '5h',
    short: week ? 'Wk' : '5h',
    kind: 'run',
    pct: week ? 48 : 55,
    approx: false,
    used: week ? 48 : 55.3,
    projected: week ? 96 : 95,
    reset: week ? 'Fri' : '16:30',
    reset_ts: week ? 1_792_123_200 : 1_791_991_800,
    phrase: null,
    sentence: week ? 'Week: 48% used, resets Fri 04:00. On pace, about 96% by then.' : '5-hour: 55% used, resets 16:30. On pace, about 95% by then.',
    ...over,
  }
}

const phrase = (p: Partial<Phrase>): Phrase => ({
  pre: '',
  num: '',
  post: '',
  narrow: p.post ?? '',
  tone: 'inactive',
  attention: false,
  calm: false,
  ...p,
})

const RUNS_OUT = phrase({ pre: 'Runs out ', num: '15:40', tone: 'error', attention: true })
const FASTER = phrase({ pre: 'Can go ', num: '2.7x', post: ' faster', tone: 'inactive', calm: true })
const UNUSED = phrase({ num: '40%', post: ' will go unused', narrow: ' unused', tone: 'success', attention: true })

function cli(windows: Win[], extra: Record<string, unknown> = {}) {
  return ran(JSON.stringify({ ...CLI, windows, updated_ts: 1_791_964_500, now: 1_791_964_680, ...extra }))
}

function sized(surface: RenderSurface, bodyColumns: number): MountTarget {
  const target = band(surface)
  return { ...target, props: { ...(target.props as object), bodyColumns } } as MountTarget
}

async function drawn($: Parameters<TestBody>[0], on: StubOn, windows: Win[], columns = 115, options: Options = {}) {
  const { clock, runs } = stub(on, { run: () => cli(windows), ...options })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  return { ui: await $.ui.mount(sized('desktop', columns)), clock, runs }
}

test('draws a bar, the percent and the reset per window, and no words while on pace', async ($, on) => {
  const { ui } = await drawn($, on, [win({ id: 'h5' }), win({ id: 'wk' })])
  const bars = await ui.findAll({ type: 'Svg' })
  expect(bars.length).toBe(2)
  expect(bars[0]?.props).toMatchObject({ width: 64, height: 14 })
  // An image, not a frame: a frame paints a white box where the app's and the system's themes differ.
  expect(bars[0]?.props.isInteractive).toBeUndefined()
  expect(String(bars[0]?.props.alt)).toMatch(/^5-hour: 55% used/)
  expect(String(bars[0]?.props.source)).toContain('<title>5-hour: 55% used, resets 16:30.')
  expect(await ui.find({ type: 'Text', text: '55%' })).toMatchObject({ props: { color: 'text', weight: 'medium' } })
  expect(await ui.find({ type: 'Text', text: 'resets 16:30' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: 'resets Fri' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /faster|pace/ })).toBeUndefined()
  expect(await ui.find({ type: 'Button', text: 'Details' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /drawn by Claude Code/ })).toBeDefined()
})

test('a window that needs you says so in color, and the other percent dims', async ($, on) => {
  const { ui } = await drawn($, on, [win({ id: 'h5', kind: 'dry', pct: 70, used: 70, phrase: RUNS_OUT }), win({ id: 'wk' })])
  expect(await ui.find({ type: 'Text', text: 'Runs out 15:40' })).toMatchObject({
    props: { color: 'error', weight: 'medium' },
    children: ['Runs out ', { type: 'Text', props: { weight: 'semibold', color: 'error' }, children: ['15:40'] }, ''],
  })
  expect(await ui.find({ type: 'Text', text: '70%' })).toMatchObject({ props: { color: 'text' } })
  expect(await ui.find({ type: 'Text', text: '48%' })).toMatchObject({ props: { color: 'inactive', weight: 'regular' } })
})

test('narrow: resets and calm words go to the tooltip, bars shrink, the words that matter stay', async ($, on) => {
  const windows = [win({ id: 'h5', phrase: FASTER }), win({ id: 'wk', kind: 'expire', pct: 40, used: 40, projected: 60, phrase: UNUSED })]
  const { ui } = await drawn($, on, windows, 55)
  expect((await ui.find({ type: 'Svg' }))?.props.width).toBe(44)
  expect(await ui.find({ type: 'Text', text: /resets/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /faster/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: '40% unused' })).toMatchObject({ props: { color: 'success' } })
  expect(await ui.find({ type: 'Button', text: 'Details' })).toBeDefined()
})

test('tiny: no Details button and Wk; below 44 the calm window keeps its name and percent', async ($, on) => {
  const windows = [win({ id: 'h5', kind: 'dry', pct: 70, used: 70, phrase: RUNS_OUT }), win({ id: 'wk' })]
  const { ui } = await drawn($, on, windows, 48)
  expect(await ui.find({ type: 'Button' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'Wk' })).toBeDefined()
  expect((await ui.findAll({ type: 'Svg' })).length).toBe(2)
  const bare = await $.ui.mount(sized('desktop', 40))
  expect((await bare.findAll({ type: 'Svg' })).length).toBe(1)
  expect(await bare.find({ type: 'Text', text: '48%' })).toBeDefined()
})

test('narrow, 2 windows that need you get the room: short words, no Details button', async ($, on) => {
  const out = phrase({ pre: 'Runs out ', narrow_pre: 'Out ', num: '05:20', tone: 'error', attention: true })
  const back = phrase({ pre: 'Back ', num: 'Fri 04:00', tone: 'error', attention: true })
  const windows = [win({ id: 'h5', kind: 'dry', pct: 70, used: 70, phrase: out }), win({ id: 'wk', kind: 'maxed', pct: 100, used: 100, phrase: back })]
  const { ui } = await drawn($, on, windows, 60)
  expect(await ui.find({ type: 'Text', text: 'Out 05:20' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: 'Back Fri 04:00' })).toBeDefined()
  expect(await ui.find({ type: 'Button' })).toBeUndefined()
  expect((await ui.findAll({ type: 'Svg' })).length).toBe(2)
  const tiny = await $.ui.mount(sized('desktop', 55)) // only without the bars do both phrases fit
  expect(await tiny.find({ type: 'Svg' })).toBeUndefined()
  expect(await tiny.find({ type: 'Text', text: 'Out 05:20' })).toBeDefined()
  const mid = await $.ui.mount(sized('desktop', 80)) // the big bars, and room for the whole lead
  expect(await mid.find({ type: 'Text', text: 'Runs out 05:20' })).toBeDefined()
  const wide = await $.ui.mount(sized('desktop', 115))
  expect(await wide.find({ type: 'Text', text: 'Runs out 05:20' })).toBeDefined()
  expect(await wide.find({ type: 'Button', text: 'Details' })).toBeDefined()
})

test('narrow, a long phrase takes the Details button\'s room; a short form keeps it', async ($, on) => {
  const may = phrase({ pre: 'May run out ', narrow_pre: 'Out ', num: 'Sun 22:45', tone: 'warning', attention: true })
  const { ui } = await drawn($, on, [win({ id: 'h5' }), win({ id: 'wk', kind: 'dry', soft: true, phrase: may })], 60)
  expect(await ui.find({ type: 'Text', text: 'Out Sun 22:45' })).toBeDefined()
  expect(await ui.find({ type: 'Button', text: 'Details' })).toBeDefined()
  const squeezed = await $.ui.mount(sized('desktop', 55)) // below 56 cells, 13 characters are long
  expect(await squeezed.find({ type: 'Button' })).toBeUndefined()
})

test('narrow, an older CLI without the short form gives a long phrase the button\'s room', async ($, on) => {
  const old = phrase({ pre: 'May run out ', num: 'Sun 22:45', tone: 'warning', attention: true })
  const { ui } = await drawn($, on, [win({ id: 'h5' }), win({ id: 'wk', kind: 'dry', phrase: old })], 55)
  expect(await ui.find({ type: 'Text', text: 'May run out Sun 22:45' })).toBeDefined()
  expect(await ui.find({ type: 'Button' })).toBeUndefined()
})

test('a window still waiting beside one with data says so', async ($, on) => {
  const wait = win({ id: 'wk', kind: 'wait', pct: null, used: 0, projected: null, reset: null, reset_ts: null })
  const { ui } = await drawn($, on, [win({ id: 'h5' }), wait])
  expect(await ui.find({ type: 'Text', text: 'waiting' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Waiting for/ })).toBeUndefined()
})

test('the width thresholds', async () => {
  expect(layout(115)).toEqual({ halves: true, W: 64, tiny: false, bare: false })
  expect(layout(103)).toEqual({ halves: false, W: 64, tiny: false, bare: false })
  expect(layout(71)).toEqual({ halves: false, W: 44, tiny: false, bare: false })
  expect(layout(51)).toEqual({ halves: false, W: 44, tiny: true, bare: false })
  expect(layout(43)).toEqual({ halves: false, W: 44, tiny: true, bare: true })
})

test('Details opens a sentence per window and says how old the reading is', async ($, on) => {
  const { ui } = await drawn($, on, [win({ id: 'h5' }), win({ id: 'wk' })])
  await ui.press({ key: 'details' })
  expect(await ui.find({ type: 'Text', text: /^5-hour: 55% used, resets 16:30\. On pace, about 95% by then\.$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /about 96% by then\. Updated 3 min ago\.$/ })).toBeDefined()
  expect(await ui.find({ type: 'Button', text: 'Details' })).toBeDefined() // the same label: the row never shifts
  await ui.press({ key: 'details' })
  expect(await ui.find({ type: 'Text', text: /Updated/ })).toBeUndefined()
})

test('an estimate reads ≈, and waiting shows one line and no Details', async ($, on) => {
  const { ui } = await drawn($, on, [win({ id: 'h5', pct: 37, approx: true }), win({ id: 'wk' })])
  expect(await ui.find({ type: 'Text', text: '≈37%' })).toBeDefined()
})

test('waiting for the first reading', async ($, on) => {
  const waiting = (id: 'h5' | 'wk') =>
    win({ id, kind: 'wait', pct: null, used: 0, projected: null, reset: null, reset_ts: null, sentence: 'waiting' })
  const { ui } = await drawn($, on, [waiting('h5'), waiting('wk')])
  expect(await ui.find({ type: 'Text', text: 'Waiting for the first reading' })).toBeDefined()
  expect(await ui.find({ type: 'Button' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /%/ })).toBeUndefined()
  expect(String((await ui.find({ type: 'Svg' }))?.props.source)).toContain('<animate')
  // Wide, the windows are not halves while waiting, so they keep a gap between them and the words.
  expect(JSON.stringify(await ui.drawn())).toContain('"columnGap":"md"')
})

test('an older CLI without windows, or broken ones, gets the text line', async ($, on) => {
  const { ui } = await drawn($, on, [win({ id: 'h5' })])
  expect(await ui.find({ type: 'Svg' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: '5h 62% ×1.4▲ →88% @10:20' })).toBeDefined()
})

test('the bars draw what each kind means', async () => {
  const svg = (over: Partial<Win>) => barSvg(win({ id: 'h5', ...over }), 64)
  expect(svg({})).toMatch(/class='ctb-bf'/)
  expect(svg({})).not.toMatch(/class='ctb-b[wdmx]'|linearGradient/)
  expect(svg({ kind: 'dry' })).toMatch(/class='ctb-bx'.*class='ctb-bw'\/>/)
  const soft = svg({ kind: 'dry', soft: true })
  expect(soft).toMatch(/linearGradient id='ctb[0-9a-f]{5}'.*stop-color:var\(--r\)/)
  expect(soft).toMatch(/class='ctb-bw' style='opacity:.45'/)
  expect(svg({ kind: 'dry', used: 95 })).not.toMatch(/class='ctb-bx'/) // no room left: the wall alone
  expect(svg({ kind: 'expire', used: 40, projected: 60 })).toMatch(/class='ctb-bd'/)
  expect(svg({ kind: 'maxed', used: 100 })).toMatch(/class='ctb-bm'/)
  expect(svg({ kind: 'idle' })).toMatch(/class='ctb-bi'/)
  expect(svg({ kind: 'idle' })).not.toMatch(/class='ctb-bf'/)
  const rough = svg({ kind: 'rough', used: 8, projected: 58 })
  const id = /linearGradient id='(ctb[0-9a-f]{5})'/.exec(rough)?.[1]
  expect(id).toBeDefined()
  expect(rough).toContain(`fill='url(#${id})'`)
  expect(svg({ projected: null })).not.toMatch(/class='ctb-bf'/)
  expect(svg({ sentence: 'a < b & c' })).toContain('<title>a &lt; b &amp; c</title>')
  expect(svg({}).length).toBeLessThan(131_072)
})

test('says how old the reading is', async () => {
  expect(age(1000, 1030)).toBe('Updated just now.')
  expect(age(1000, 1000 + 180)).toBe('Updated 3 min ago.')
  expect(age(1000, 1000 + 7200)).toBe('Updated 2 h ago.')
  expect(age(null, 1000)).toBe('')
})

test('runs Python at once when this session reads a new level, and not when nothing moved', async ($, on) => {
  // The stub reads options.limits at each call.
  const options: Options = { run: () => cli([win({ id: 'h5' }), win({ id: 'wk' })]) }
  const { runs, clock } = stub(on, options)
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(1)
  await clock.advance(15_000)
  expect(runs.length).toBe(1)
  // A tenth of a point: session.measure waits for a whole one, the poll does not.
  options.limits = [{ kind: 'five_hour', percentUsed: 62.1, resetsAt: '2026-10-14T10:20:00.000Z' }, LIMITS[1] as SessionRateLimit]
  await clock.advance(5_000)
  expect(runs.length).toBe(2)
  expect(JSON.parse(runs[1]?.init?.stdin ?? '{}').rate_limits.five_hour.used_percentage).toBe(62.1)
  await clock.advance(10_000)
  expect(runs.length).toBe(2)
})

test("redraws when another chat's reading touches the stamp, not after its own", async ($, on) => {
  const stamp = { mtimeMs: 1000, text: '1791964000.000\n' }
  const options: Options = { stamp }
  const { runs, clock } = stub(on, {
    ...options,
    run: () => {
      // This run records a fresh reading: the CLI touches the stamp with its own time.
      stamp.mtimeMs += 1
      stamp.text = '1791964680.000\n'
      return cli([win({ id: 'h5' }), win({ id: 'wk' })])
    },
  })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(1)
  await clock.advance(15_000)
  expect(runs.length).toBe(1)
  stamp.mtimeMs = 5000 // another chat
  stamp.text = '1791964700.000\n'
  await clock.advance(5_000)
  expect(runs.length).toBe(2)
  await clock.advance(15_000)
  expect(runs.length).toBe(2)
})

test('a failing run is not retried by the poll, only by the refresh timer', async ($, on) => {
  const options: Options = { run: failed(2, 'Traceback: boom\n') }
  const { runs, clock } = stub(on, options)
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(1)
  options.limits = [{ kind: 'five_hour', percentUsed: 63, resetsAt: '2026-10-14T10:20:00.000Z' }]
  await clock.advance(5_000)
  expect(runs.length).toBe(2)
  await clock.advance(30_000)
  expect(runs.length).toBe(2)
  await clock.advance(30_000)
  expect(runs.length).toBe(3)
})
