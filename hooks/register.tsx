import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderInput } from 'claude-code'

import type { BandData, BandPool, LaunchProvider } from '../types'

// The band above the prompt: the week's pools (from the router, through band.py) and the linked sessions.
// It reads local files and runs one local script, once a minute; it calls no model.
const data = atom({ plugin: 'switchboard', key: 'data' } as const, null, { shape: 'band-3' })
const isHidden = atom({ plugin: 'switchboard', key: 'isHidden' } as const, false)
const error = atom({ plugin: 'switchboard', key: 'error' } as const, null)
// The launcher row above the band: a new session with any provider the router finds, in one of its clients.
const launch = atom({ plugin: 'switchboard', key: 'launch' } as const, null, { shape: 'launch-1' })
const launchPick = atom({ plugin: 'switchboard', key: 'launchPick' } as const, null)
const launchNote = atom({ plugin: 'switchboard', key: 'launchNote' } as const, null)
const isLauncherHidden = atom({ plugin: 'switchboard', key: 'isLauncherHidden' } as const, false)

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
    newSession: 'Нова сесія:', launching: 'Відкриваю', opened: 'Відкрито', model: 'модель',
    launcherCommand: 'Сховати чи показати рядок запуску нових сесій над смугою квот (запам\'ятовується)',
    launcherHidden: 'Рядок запуску сховано.', launcherShown: 'Рядок запуску показано.', providersReading: 'читаю провайдерів…',
  },
  en: {
    d: 'd', h: 'h', min: 'min', decimal: '.', skip: '❌ the router skips it: ', burns: '⚠️ burns first',
    pace: '⚠️ spent fast: the router takes a calmer one', stale: '⚠️ stale numbers', code: 'exit',
    command: 'Hide or show the band of quotas and sessions above the prompt (remembered)',
    hidden: 'The quota band is hidden.', shown: 'The quota band is shown.', reading: 'Quotas: reading…',
    working: ['session working', 'sessions working', 'sessions working'], idle: ['idle', 'idle', 'idle'],
    claudeMissing: '⚠️ Claude: no numbers', claudeOld: (age: string) => `⚠️ Claude: numbers ${age} old`,
    heads: ['Provider', 'Model', 'Left', 'Resets', 'State'], noQuotas: 'no quotas', updated: 'updated',
    newSession: 'New session:', launching: 'Opening', opened: 'Opened', model: 'model',
    launcherCommand: 'Hide or show the row that opens new sessions above the quota band (remembered)',
    launcherHidden: 'The launcher row is hidden.', launcherShown: 'The launcher row is shown.', providersReading: 'reading providers…',
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

const LAUNCH_REFRESH_MS = 30 * 60_000
let isListing = false

// The providers and models the router finds here, through launch.py list (the router's inventory, no model call).
async function listProviders($: EngineInterface): Promise<void> {
  if (isListing) return
  isListing = true
  try {
    const ran = await $.process.run(['python', `${await rootOf($)}/mods/launcher/launch.py`, 'list', '--lang', lang], { timeoutMs: 120_000 })
    if (ran.exitCode !== 0) {
      const last = ran.stderr.trim().split('\n').pop()?.trim().slice(0, 120) ?? ''
      await update($, launchNote, () => `launch.py: ${w().code} ${ran.exitCode}${last ? `: ${last}` : ''}`)
      return
    }
    const parsed = JSON.parse(ran.stdout.trim()) as { providers: LaunchProvider[] }
    await update($, launch, () => parsed.providers)
  } catch (err) {
    await update($, launchNote, () => `launch.py: ${String(err).slice(0, 80)}`).catch(() => undefined)
  } finally {
    isListing = false
  }
}

async function openSession($: EngineInterface, p: LaunchProvider, model: string, client: { id: string; label: string }): Promise<void> {
  const said = `${p.label} · ${model} · ${client.label}`
  await update($, launchNote, () => `${w().launching}: ${said}…`)
  try {
    const cwd = await $.session.cwd()
    const ran = await $.process.run(
      ['python', `${await rootOf($)}/mods/launcher/launch.py`, 'open', p.id, model, client.id, '--cwd', cwd],
      { timeoutMs: 60_000 },
    )
    if (ran.exitCode !== 0) {
      const last = ran.stderr.trim().split('\n').pop()?.trim().slice(0, 120) ?? ''
      await update($, launchNote, () => `⚠️ ${said}: ${w().code} ${ran.exitCode}${last ? `: ${last}` : ''}`)
      return
    }
    await update($, launchNote, () => `✅ ${w().opened}: ${said}`)
  } catch (err) {
    await update($, launchNote, () => `⚠️ ${said}: ${String(err).slice(0, 80)}`).catch(() => undefined)
  }
}

export const register: Register = on => {

  on('session.start', async ($, e, next) => {
    const asked = ((await $.env.get('ASK_LANG')) ?? '').trim().toLowerCase()
    lang = asked === 'uk' || asked === 'en' ? asked : asked ? 'en' : DEFAULT_LANG
    await $.command.register({ name: 'limits-band', description: w().command })
    const stored = await $.store.get('isHidden')
    await update($, isHidden, () => stored === true)
    await $.command.register({ name: 'launcher', description: w().launcherCommand })
    const launcherStored = await $.store.get('isLauncherHidden')
    await update($, isLauncherHidden, () => launcherStored === true)
    void refresh($)
    $.clock.every(REFRESH_MS, () => void refresh($))
    void listProviders($)
    $.clock.every(LAUNCH_REFRESH_MS, () => void listProviders($))

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

  on('command.run', { command: 'launcher' }, async $ => {
    const hidden = !(await read($, isLauncherHidden))
    await update($, isLauncherHidden, () => hidden)
    await $.store.set('isLauncherHidden', hidden)
    if (!hidden) void listProviders($)

    return { text: hidden ? w().launcherHidden : w().launcherShown }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const launcherRow = (await read($, isLauncherHidden)) ? null : await drawLauncher($, e)
    const bandRow = (await read($, isHidden)) ? null : await drawBand($, e)
    if (launcherRow === null && bandRow === null) return next(e)
    const { Box } = $.ui.resolve(e)

    return (
      <Box key="switchboard" flexDirection="column">
        {launcherRow}
        {bandRow}
      </Box>
    )
  })
}


// The launcher: one coloured button per provider; under the picked one, its models and a button per client.
async function drawLauncher($: EngineInterface, e: RenderInput<'AbovePrompt'>) {
  const list = await read($, launch)
  const pick = await read($, launchPick)
  const note = await read($, launchNote)
  const { Box, Text, Button, Select } = $.ui.resolve(e)
  const picked = list?.find(p => p.id === pick?.provider) ?? null
  const model = picked ? (picked.models.includes(pick?.model ?? '') ? (pick?.model as string) : picked.models[0]) : null

  return (
    // A graphical surface draws Buttons taller than a line of text: without a gap between the rows they overlap.
    <Box key="launcher" flexDirection="column" rowGap={e.surface === 'terminal' ? 0 : 'sm'}>
      <Box flexDirection="row" flexWrap="wrap" columnGap={1} rowGap={e.surface === 'terminal' ? 0 : 'xs'} alignItems="center">
        <Text dimColor>{w().newSession}</Text>
        {list === null ? <Text dimColor>{w().providersReading}</Text> : null}
        {(list ?? []).map(p => (
          <Button
            key={`provider-${p.id}`}
            variant={picked?.id === p.id ? 'primary' : undefined}
            onPress={() => void update($, launchPick, () => ({ provider: p.id, model: p.models[0] ?? '' }))}
          >
            <Text color={p.color} bold>● </Text>
            {p.label}
          </Button>
        ))}
      </Box>
      {picked && model ? (
        <Box flexDirection="row" flexWrap="wrap" columnGap={1} rowGap={e.surface === 'terminal' ? 0 : 'xs'} alignItems="center">
          <Select
            key={`model-${picked.id}`}
            label={`${w().model}: `}
            options={picked.models.map(m => ({ value: m, label: m }))}
            value={model}
            onSelect={value => void update($, launchPick, () => ({ provider: picked.id, model: value }))}
          />
          {picked.clients.map(c => (
            <Button key={`open-${picked.id}-${c.id}`} onPress={() => void openSession($, picked, model, c)}>
              <Text color={picked.color} bold>▶ </Text>
              {c.label}
            </Button>
          ))}
        </Box>
      ) : null}
      {note ? <Text dimColor wrap="truncate-end">{note}</Text> : null}
    </Box>
  )
}

async function drawBand($: EngineInterface, e: RenderInput<'AbovePrompt'>) {
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
}
