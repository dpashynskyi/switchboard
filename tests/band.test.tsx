import { expect, mock, test } from 'claude-code/testing'

const BAND = {
  plugin: 'switchboard',
  component: 'AbovePrompt',
  requestId: 'above-prompt',
  props: {
    hasSurvey: false,
    isWorking: false,
    maxRows: 10,
    bodyColumns: 160,
    scroll: { offset: 0, bodyRows: 9 },
    view: {},
  },
} as const

const SAMPLE = {
  at: '23:40',
  errors: [],
  pools: [
    { name: 'Claude', provider: 'Anthropic', model: 'Claude', left_pct: 14, resets: '17 год', burns_first: true, skip: null, pace_over: false, stale: false },
    { name: 'ChatGPT', provider: 'OpenAI', model: 'GPT', left_pct: 97, resets: '5,9 д', burns_first: false, skip: null, pace_over: false, stale: false },
    { name: 'agy Gemini', provider: 'Google', model: 'Gemini', left_pct: 44, resets: '5,0 д', burns_first: false, skip: null, pace_over: true, stale: false },
  ],
  claude: { status: 'ok', age_s: 7300 },
  sessions: { working: 2, idle: 1 },
}

function engine(on: Parameters<Parameters<typeof test>[1]>[1], stdout: string, exitCode = 0, runs = { count: 0 }) {
  const clock = mock.clock(on)
  mock.store(on)
  mock.env(on, { USERPROFILE: 'C:/Users/someone', ASK_LANG: 'uk' })
  on('command.register', () => ({ value: { command: 'limits-band' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'cli-1' }))
  on('process.run', () => (runs.count++, {
    value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  }))

  return clock
}

test('the band shows the pools, the marks, the sessions and the stale Claude numbers', async ($, on) => {
  const clock = engine(on, JSON.stringify(SAMPLE))
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  // The first reading starts unawaited at the session's start; let it finish.
  await clock.settle()

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface })
    for (const cell of ['Провайдер', 'Модель', 'Anthropic', 'Claude', 'OpenAI', 'Google', 'Gemini', 'GPT', 'Лишилось', '14%', '17 год', '⚠️ згорить першим', '97%', '✅', '⚠️ витрачається швидко: роутер бере спокійніший']) {
      expect((await ui.find({ type: 'Text', text: cell }))?.text).toBe(cell)
    }
    const line = await ui.find({ type: 'Text', text: /сесії/ })
    expect(line?.text).toBe(
      'Anthropic 14% ⚠️ · OpenAI 97% · Google 44% ⚠️   │   2 сесії працюють, 1 вільна   │   ⚠️ Claude: цифри 2 год тому',
    )
    expect((await ui.find({ type: 'Text', text: /оновлено/ }))?.text).toBe('оновлено 23:40')
    await ui.unmount()
  }
})

test('the command hides the band and shows it again', async ($, on) => {
  const clock = engine(on, JSON.stringify(SAMPLE))
  // Hidden, the band hands the site on; this hook stands for the engine's own drawing.
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)

    return <Text>engine</Text>
  })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  // The first reading starts unawaited at the session's start; let it finish.
  await clock.settle()

  const hidden = await $.command.run({ command: 'limits-band', args: '' })
  expect(JSON.stringify(hidden)).toContain('сховано')
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ type: 'Text' }))?.text).toBe('engine')
  await ui.unmount()

  const shown = await $.command.run({ command: 'limits-band', args: '' })
  expect(JSON.stringify(shown)).toContain('показано')
})

test('a failed script is said in the band', async ($, on) => {
  const clock = engine(on, '', 1)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  // The first reading starts unawaited at the session's start; let it finish.
  await clock.settle()

  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ type: 'Text' }))?.text).toBe('band.py: код 1')
  await ui.unmount()
})

test('the script runs once at the start and then once a minute, no oftener', async ($, on) => {
  const runs = { count: 0 }
  const clock = engine(on, JSON.stringify(SAMPLE), 0, runs)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.settle()
  expect(runs.count).toBe(1)

  await clock.advance(59_000)
  expect(runs.count).toBe(1)
  await clock.advance(1_000)
  expect(runs.count).toBe(2)
  await clock.advance(60_000)
  expect(runs.count).toBe(3)
})

test('the collapsed line puts the pools of a provider together and counts sessions in Ukrainian', async ($, on) => {
  const google2 = { ...SAMPLE.pools[2]!, model: 'Claude і GPT', left_pct: 48, pace_over: false, skip: 'нижче порогу' }
  const sample = { ...SAMPLE, pools: [...SAMPLE.pools, google2], claude: { status: 'ok', age_s: 60 },
    sessions: { working: 1, idle: 11 } }
  const clock = engine(on, JSON.stringify(sample))
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.settle()

  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: /сесія/ }))?.text).toBe(
    'Anthropic 14% ⚠️ · OpenAI 97% · Google 44% ⚠️ / 48% ❌   │   1 сесія працює, 11 вільних',
  )
  await ui.unmount()
})
