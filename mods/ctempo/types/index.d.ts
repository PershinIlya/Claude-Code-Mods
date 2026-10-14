export type Tone = 'dim' | 'plain' | 'opportunity' | 'warn' | 'alert'

export type Segment = { text: string; tone: Tone }

// What `ctempo statusline --format json` prints, plus the mod's own error.
export type View = {
  line: string
  segments: Segment[]
  surfaces: string[]
  refresh_s: number
  error?: string
}

declare module 'claude-code' {
  interface PluginState {
    ctempo: { view: View | null }
  }
}
