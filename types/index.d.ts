export type BandPool = {
  name: string
  provider: string
  model: string
  left_pct: number
  resets: string
  burns_first: boolean
  /** Why the router refuses the pool, or null. */
  skip: string | null
  /** The weekly pace over the router's pace_skip: it takes a calmer pool first, when there is one. */
  pace_over: boolean
  stale: boolean
}

export type BandSessions = {
  working: number
  idle: number
}

export type BandData = {
  at: string
  errors: string[]
  pools: BandPool[]
  claude: { status: string | null; age_s: number | null }
  sessions: BandSessions | null
}

/** A provider the launcher row offers (mods/launcher/launch.py list): its models in the router's order. */
export type LaunchProvider = {
  id: string
  label: string
  color: string
  /** The provider's logo as SVG markup, in its colour; "" when it has none. */
  icon?: string
  models: string[]
  clients: { id: string; label: string }[]
}

/** What the launcher row has picked: the provider whose clients show, and its model. */
export type LaunchPick = { provider: string; model: string }

declare module 'claude-code' {
  interface PluginState {
    'switchboard': { data: Shaped<BandData | null>; isHidden: boolean; error: string | null
      launch: Shaped<LaunchProvider[] | null>
      launchPick: LaunchPick | null
      launchNote: string | null
      isLauncherHidden: boolean
    }
  }
}
