// Segment tones of the text line: the classic line's, and the bars line's
// words and bar parts (ctempo/render.py ANSI). An unknown one draws plain.
export type Tone = string

export type Segment = { text: string; tone: Tone }

// What a window's bar draws (ctempo/band.py).
export type BarKind = 'run' | 'rough' | 'dry' | 'expire' | 'maxed' | 'idle' | 'wait'

// The words after the percent: at most one number, drawn bold.
export type Phrase = {
  pre: string
  num: string
  post: string
  // `pre` and `post` in a narrow band ("Out 15:40" for "Runs out 15:40").
  narrow_pre?: string
  narrow: string
  tone: 'inactive' | 'success' | 'warning' | 'error'
  // The window needs you: its words always show, the other percent dims.
  attention: boolean
  // Grey words ("Can go 2.7x faster"): the first to go when room is short.
  calm: boolean
}

// One window of the band (band.window).
export type Win = {
  id: 'h5' | 'wk'
  name: string
  short: string
  kind: BarKind
  pct: number | null
  // A milder run-out (yellow): the red forecast fades.
  soft?: boolean
  used: number
  projected: number | null
  reset: string | null
  reset_ts: number | null
  phrase: Phrase | null
  sentence: string
}

// What `ctempo statusline --format json` prints, plus the mod's own error.
export type View = {
  line: string
  segments: Segment[]
  surfaces: string[]
  refresh_s: number
  windows?: Win[] | null
  // When a reading last changed, and when the CLI ran: epoch seconds.
  updated_ts?: number | null
  now?: number
  error?: string
}

declare module 'claude-code' {
  interface PluginState {
    ctempo: { view: View | null; details: boolean }
  }
}
