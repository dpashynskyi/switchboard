import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { BandData, BandPool } from '../types'

// The band above the prompt: the week's pools (from the router, through band.py) and the linked sessions.
// It reads local files and runs one local script, once a minute; it calls no model.
const data = atom({ plugin: 'switchboard', key: 'data' } as const, null, { shape: 'band-3' })
const isHidden = atom({ plugin: 'switchboard', key: 'isHidden' } as const, false)
const error = atom({ plugin: 'switchboard', key: 'error' } as const, null)

// The words a person reads, in the language ASK_LANG names (uk, en), as in skills/ask/limits.py.
const DEFAULT_LANG: Lang = 'en'
const WORDS = {
  uk: {
    d: 'д', h: 'год', min: 'хв', decimal: ',', skip: '❌ роутер обходить: ', burns: '⚠️ згорить першим',
    pace: '⚠️ витрачається швидко: роутер бере спокійніший', stale: '⚠️ цифри застарілі', code: 'код',
    command: 'Сховати чи показати смугу квот і сесій над полем вводу (запам\'ятовується)',
    hidden: 'Смугу квот сховано.', shown: 'Смугу квот показано.', reading: 'Квоти: читаю…',
    working: ['сесія працює', 'сесії працюють', 'сесій працюють'], idle: ['вільна', 'вільні', 'вільних'],
    claudeMissing: '⚠️ Claude: цифр немає', claudeOld: (age: string) => `⚠️ Claude: цифри ${age} тому`,
    heads: ['Провайдер', 'Модель', 'Лишилось', 'Скидання', 'Стан'], noQuotas: 'квот немає', updated: 'оновлено',
  },
  en: {
    d: 'd', h: 'h', min: 'min', decimal: '.', skip: '❌ the router skips it: ', burns: '⚠️ burns first',
    pace: '⚠️ spent fast: the router takes a calmer one', stale: '⚠️ stale numbers', code: 'exit',
    command: 'Hide or show the band of quotas and sessions above the prompt (remembered)',
    hidden: 'The quota band is hidden.', shown: 'The quota band is shown.', reading: 'Quotas: reading…',
    working: ['session working', 'sessions working', 'sessions working'], idle: ['idle', 'idle', 'idle'],
    claudeMissing: '⚠️ Claude: no numbers', claudeOld: (age: string) => `⚠️ Claude: numbers ${age} old`,
    heads: ['Provider', 'Model', 'Left', 'Resets', 'State'], noQuotas: 'no quotas', updated: 'updated',
  },
}
type Lang = 'uk' | 'en'
let lang: Lang = DEFAULT_LANG
const w = () => WORDS[lang]

const REFRESH_MS = 60_000
const STALE_S = 3600

function span(seconds: number): string {
  if (seconds >= 86400) return `${(seconds / 86400).toFixed(1).replace('.', w().decimal)} ${w().d}`
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)} ${w().h}`
  return `${Math.floor(seconds / 60)} ${w().min}`
}

function mark(p: BandPool): string {
  if (p.skip) return ' ❌'
  if (p.burns_first || p.pace_over) return ' ⚠️'
  return ''
}

// Ukrainian count forms: 1 сесія, 2 сесії, 5 сесій (and 11 to 14 take the third); English: one, then many.
function plural(n: number, one: string, few: string, many: string): string {
  if (lang !== 'uk') return n === 1 ? one : many
  const tens = n % 100
  if (tens >= 11 && tens <= 14) return many
  if (n % 10 === 1) return one
  if (n % 10 >= 2 && n % 10 <= 4) return few
  return many
}

function stateCell(p: BandPool): string {
  if (p.skip) return `${w().skip}${p.skip}`
  if (p.burns_first) return w().burns
  if (p.pace_over) return w().pace
  if (p.stale) return w().stale
  return '✅'
}

let isRunning = false

// The folder that holds skills/ask and this mod: SWITCHBOARD_ROOT, else the usual clone in the home folder.
async function rootOf($: EngineInterface): Promise<string> {
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? ''
  return (await $.env.get('SWITCHBOARD_ROOT')) ?? `${home}/switchboard`
}

// The Claude week as the engine read it from the last response, stored where the router and band.py read it. The
// desktop app runs no status line, so without this its Claude numbers waited for a /limits. statusline_usage.py
// takes the status line's shape, keeps the other windows and writes nothing when the number has not moved.
async function storeClaudeWeek($: EngineInterface, week: { percentUsed: number; resetsAt?: string }): Promise<void> {
  try {
    const stdin = JSON.stringify({ rate_limits: { seven_day: { used_percentage: week.percentUsed, resets_at: week.resetsAt } } })
    await $.process.run(['python', `${await rootOf($)}/skills/ask/statusline_usage.py`], { stdin, timeoutMs: 30_000 })
  } catch {
    // The next turn's reading tries again; /limits still writes the same file.
  }
}

async function refresh($: EngineInterface): Promise<void> {
    if (isRunning) return
    isRunning = true
    try {
      const script = `${await rootOf($)}/mods/limits-band/band.py`
      const sessionId = await $.session.id()
      const ran = await $.process.run(['python', script, '--session', sessionId], { timeoutMs: 90_000 })
      if (ran.exitCode !== 0) {
        const last = ran.stderr.trim().split('\n').pop()?.trim().slice(0, 120) ?? ''
        await update($, error, () => `band.py: ${w().code} ${ran.exitCode}${last ? `: ${last}` : ''}`)
        return
      }
      const parsed = JSON.parse(ran.stdout.trim()) as BandData
      await update($, data, () => parsed)
      await update($, error, () => null)
    } catch (err) {
      // The session may have ended meanwhile: then there is nowhere to say it.
      await update($, error, () => `band.py: ${String(err).slice(0, 80)}`).catch(() => undefined)
    } finally {
      isRunning = false
    }
}

export const register: Register = on => {

  on('session.start', async ($, e, next) => {
    const asked = ((await $.env.get('ASK_LANG')) ?? '').trim().toLowerCase()
    lang = asked === 'uk' || asked === 'en' ? asked : asked ? 'en' : DEFAULT_LANG
    await $.command.register({ name: 'limits-band', description: w().command })
    const stored = await $.store.get('isHidden')
    await update($, isHidden, () => stored === true)
    void refresh($)
    $.clock.every(REFRESH_MS, () => void refresh($))

    return next(e)
  })

  // After each turn, and when a window moves a whole point.
  on('session.measure', async ($, e, next) => {
    const week = e.rateLimits.find(r => r.kind === 'seven_day')
    if (week) void storeClaudeWeek($, week)

    return next(e)
  })

  on('command.run', { command: 'limits-band' }, async $ => {
    const hidden = !(await read($, isHidden))
    await update($, isHidden, () => hidden)
    await $.store.set('isHidden', hidden)
    if (!hidden) await refresh($)

    return { text: hidden ? w().hidden : w().shown }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || (await read($, isHidden))) return next(e)
    const band = await read($, data)
    const failed = await read($, error)
    const { Box, Text } = $.ui.resolve(e)

    if (band === null) {
      return (
        <Box>
          <Text dimColor>{failed ?? w().reading}</Text>
        </Box>
      )
    }

    const tail: string[] = []
    const s = band.sessions
    if (s) {
      const said = [
        s.working > 0 ? `${s.working} ${plural(s.working, ...(w().working as [string, string, string]))}` : '',
        s.idle > 0 ? `${s.idle} ${plural(s.idle, ...(w().idle as [string, string, string]))}` : '',
      ].filter(Boolean)
      if (said.length > 0) tail.push(said.join(', '))
    }
    const c = band.claude
    if (c.status === 'missing') tail.push(w().claudeMissing)
    else if ((c.age_s ?? 0) > STALE_S) tail.push(w().claudeOld(span(c.age_s ?? 0)))
    if (band.errors.length > 0) tail.push(`⚠️ ${band.errors[0]}`)
    if (failed) tail.push(`⚠️ ${failed}`)

    // A table drawn as columns, so that each column lines up on every surface; numbers flush right.
    const columns: { head: string; cells: string[]; isNumber: boolean }[] = [
      { head: w().heads[0], cells: band.pools.map(p => p.provider), isNumber: false },
      { head: w().heads[1], cells: band.pools.map(p => p.model), isNumber: false },
      { head: w().heads[2], cells: band.pools.map(p => `${p.left_pct}%`), isNumber: true },
      { head: w().heads[3], cells: band.pools.map(p => p.resets), isNumber: true },
      { head: w().heads[4], cells: band.pools.map(stateCell), isNumber: false },
    ]
    // Collapsed to one line until the pointer is over it, where a surface has a pointer;
    // the table opens above the line, so the line stays under the pointer and the table does not flicker.
    const canHover = e.surface === 'desktop' || e.surface === 'vscode' || (e.surface === 'terminal' && e.viewport?.isFullscreen === true)
    // One entry per provider, its pools' leftovers in the table's order: "Google 48% / 44%".
    const providers: string[] = []
    for (const p of band.pools) if (!providers.includes(p.provider)) providers.push(p.provider)
    const summary = providers
      .map(name => `${name} ${band.pools.filter(p => p.provider === name).map(p => `${p.left_pct}%${mark(p)}`).join(' / ')}`)
      .join(' · ')
    const line = [summary || w().noQuotas, ...tail].join('   │   ')

    return (
      <Box key="band" flexDirection="column">
        <Box
          flexDirection="column"
          display={canHover ? 'none' : 'flex'}
          hover={canHover ? { display: 'flex' } : undefined}
        >
          <Box flexDirection="row" columnGap={3}>
            {columns.map(col => (
              <Box flexDirection="column" alignItems={col.isNumber ? 'flex-end' : 'flex-start'}>
                <Text dimColor>{col.head}</Text>
                {col.cells.map((cell, i) => (
                  <Text dimColor={band.pools[i]?.stale ?? false}>{cell}</Text>
                ))}
              </Box>
            ))}
          </Box>
          <Text dimColor>{w().updated} {band.at}</Text>
        </Box>
        <Text dimColor wrap="truncate-end">
          {line}
        </Text>
      </Box>
    )
  })
}
