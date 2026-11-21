import { expect, mock, test } from 'claude-code/testing'

type Repo = Record<string, unknown>

const repo = (over: Repo): Repo => ({
  name: 'switchboard', path: '/home/user/switchboard', state: 'current', note: null, why: null, detail: [],
  from: 'aaa', to: 'bbb', subjects: [], changed: [], needs_new_session: false, ...over,
})

function engine(on: Parameters<Parameters<typeof test>[1]>[1], answers: object[], store: Record<string, unknown> = {}) {
  const clock = mock.clock(on)
  mock.store(on, store)
  mock.env(on, { HOME: '/home/user', ASK_LANG: 'uk' })
  const toasts: string[] = []
  const updates: string[][] = []
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 's1' }))
  on('ui.toast', (_, e) => (toasts.push(e.text), { value: undefined }))
  on('process.run', (_, e) => {
    const isUpdate = e.argv.some(a => a.endsWith('update.py'))
    if (isUpdate) updates.push([...e.argv])
    const answer = isUpdate ? answers[Math.min(updates.length, answers.length) - 1] : null
    return { value: { exitCode: isUpdate ? 0 : 1, stdout: answer ? JSON.stringify(answer) : '', stderr: '',
      isStdoutTruncated: false, isStderrTruncated: false } }
  })

  return { clock, toasts, updates }
}

test('an update at the start is said once, with what came and whether the mod needs a new session', async ($, on) => {
  const updated = { checked: true, reason: null, copies: [{ path: '/home/user/.config/ask/roles.json', state: 'updated' }], repos: [
    repo({ state: 'updated', subjects: ['band: one toggle', 'roles: GPT-6.2'], needs_new_session: true }),
    repo({ name: 'ai-workflow', path: '/home/user/ai-workflow', state: 'current' }),
  ] }
  const later = { checked: false, reason: 'checked recently', repos: [], copies: [] }
  const { clock, toasts, updates } = engine(on, [updated, later])
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.settle()

  expect(updates[0]).toEqual(['python', '/home/user/switchboard/tools/update.py'])
  expect(toasts).toEqual([
    'switchboard оновлено (2 зміни): band: one toggle; roles: GPT-6.2. частина змін — у самому моді: діє з нової сесії',
    'оновлено копію /home/user/.config/ask/roles.json',
  ])

  await clock.advance(15 * 60_000)
  await clock.settle()
  expect(updates.length).toBe(2)
  expect(toasts.length).toBe(2)
})

test('an update held back by local edits is said once per new head, a clone on a branch not at all', async ($, on) => {
  const waits = { checked: true, reason: null, copies: [], repos: [
    repo({ state: 'skipped', why: 'clash', detail: ['skills/ask/roles.json'], note: 'local edits in files the update changes: skills/ask/roles.json' }),
    repo({ name: 'ai-workflow', path: '/home/user/ai-workflow', state: 'skipped', why: 'branch', detail: ['feature'], note: 'on feature, not main' }),
  ] }
  const { clock, toasts } = engine(on, [waits, waits])
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.settle()
  expect(toasts).toEqual(['switchboard: оновлення чекає, бо є локальні зміни у файлах, які воно міняє: skills/ask/roles.json'])

  await clock.advance(15 * 60_000)
  await clock.settle()
  expect(toasts.length).toBe(1)
})

test('the command checks now and says every repository, or that all is current', async ($, on) => {
  const idle = { checked: false, reason: 'checked recently', repos: [], copies: [] }
  const current = { checked: true, reason: null, copies: [], repos: [repo({})] }
  const branch = { checked: true, reason: null, copies: [], repos: [repo({ state: 'skipped', why: 'branch', detail: ['feature'], note: 'on feature, not main' })] }
  const { clock, updates } = engine(on, [idle, current, branch])
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.settle()

  const first = await $.command.run({ command: 'switchboard-update', args: '' })
  expect(updates[1]).toEqual(['python', '/home/user/switchboard/tools/update.py', '--force'])
  expect(JSON.stringify(first)).toContain('на останньому main')
  const second = await $.command.run({ command: 'switchboard-update', args: '' })
  expect(JSON.stringify(second)).toContain('клон на гілці feature, а не на main')
})
