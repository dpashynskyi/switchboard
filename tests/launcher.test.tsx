import { expect, mock, test } from 'claude-code/testing'

const ABOVE = {
  plugin: 'switchboard',
  component: 'AbovePrompt',
  requestId: 'above-prompt',
  props: {
    hasSurvey: false,
    isCollapsed: false,
    isWorking: false,
    maxRows: 30,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 29 },
    view: {},
  },
} as const

const LIST = {
  providers: [
    { id: 'claude-subscription', label: 'Claude', color: '#D97757', models: ['claude-opus-5-5'],
      clients: [{ id: 'desktop', label: 'Claude desktop' }, { id: 'terminal', label: 'terminal' }] },
    { id: 'codex', label: 'Codex · GPT', color: '#10A37F', models: ['gpt-6.1-sol', 'gpt-6-astra'],
      clients: [{ id: 'terminal', label: 'terminal' }, { id: 'app', label: 'Codex app' }] },
  ],
}

test('the launcher row opens the picked provider and model in the pressed client, on every surface', async ($, on) => {
  const runs: string[][] = []
  const clock = mock.clock(on)
  mock.store(on)
  mock.env(on, { HOME: '/home' })
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('session.id', () => ({ value: 's1' }))
  on('session.cwd', () => ({ value: '/work' }))
  on('process.run', (_, e) => {
    runs.push([...e.argv])
    const stdout = e.argv.includes('list') ? JSON.stringify(LIST) : '{}'
    return { value: { exitCode: e.argv.some(a => a.endsWith('band.py')) ? 1 : 0, stdout, stderr: '',
      isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  await clock.settle()

  for (const surface of ['terminal', 'desktop'] as const) {
    runs.length = 0
    const ui = await $.ui.mount({ ...ABOVE, surface })
    expect((await ui.find({ type: 'Text', text: /New session/ }))?.text).toBe('New session:')

    await ui.press({ key: 'provider-codex' })
    await ui.press({ key: 'open-codex-terminal' })
    const opened = runs.find(r => r.includes('open'))
    expect(opened?.slice(2)).toEqual(['open', 'codex', 'gpt-6.1-sol', 'terminal', '--cwd', '/work'])
    expect((await ui.find({ type: 'Text', text: /Opened/ }))?.text).toBe('✅ Opened: Codex · GPT · gpt-6.1-sol · terminal')
    await ui.unmount()
  }
})
