'use strict'
const test = require('node:test')
const assert = require('node:assert')
const { Runner } = require('../src/runner')
const { TaskFailed } = require('../src/job')

function fakeBot (items = []) {
  return {
    username: 'Helper',
    health: 20,
    food: 20,
    game: { dimension: 'overworld' },
    entity: { position: { x: 1.5, y: 64, z: 2.5 } },
    inventory: { items: () => items },
    registry: { blocksByName: {} },
    findBlock: () => null,
    pathfinder: { stop () {}, setGoal () {} },
    clearControlStates () {}
  }
}

function fakeAssistant (tasks) {
  const reports = []
  return {
    reports,
    next: async () => (tasks.length ? { status: 'running', task: tasks.shift() } : { status: 'idle' }),
    report: async report => { reports.push(report); return { status: 'running' } }
  }
}

const config = { minHealth: 6, idleMs: 10 }
const task = (kind, extra = {}) => ({ task_id: 't1', kind, item: 'minecraft:stick', count: 1, timeout_s: 5, ...extra })

test('задача исполнена — отчёт done с инвентарём', async () => {
  const assistant = fakeAssistant([task('craft')])
  const bot = fakeBot([{ name: 'stick', count: 4 }])
  const runner = new Runner({ bot, assistant, config, handlers: { craft: async () => {} }, danger: () => null })
  assert.strictEqual(await runner.step(), true)
  assert.strictEqual(assistant.reports[0].status, 'done')
  assert.deepStrictEqual(assistant.reports[0].inventory, { 'minecraft:stick': 4 })
})

test('неизвестный вид задачи — unsupported', async () => {
  const assistant = fakeAssistant([task('build_house')])
  const runner = new Runner({ bot: fakeBot(), assistant, config, handlers: {}, danger: () => null })
  await runner.step()
  assert.strictEqual(assistant.reports[0].status, 'failed')
  assert.strictEqual(assistant.reports[0].reason, 'unsupported')
})

test('неудача задачи передаёт причину', async () => {
  const assistant = fakeAssistant([task('mine')])
  const handlers = { mine: async () => { throw new TaskFailed('no_tool', 'нечем') } }
  const runner = new Runner({ bot: fakeBot(), assistant, config, handlers, danger: () => null })
  await runner.step()
  assert.strictEqual(assistant.reports[0].reason, 'no_tool')
})

test('опасность прерывает задачу — interrupted/danger', async () => {
  const assistant = fakeAssistant([task('mine')])
  let dangerous = false
  const handlers = { mine: async (ctx, t, job) => { dangerous = true; await job.sleep(5000) } }
  const runner = new Runner({ bot: fakeBot(), assistant, config, handlers, danger: () => (dangerous ? 'рядом лава' : null) })
  await runner.step()
  assert.strictEqual(assistant.reports[0].status, 'interrupted')
  assert.strictEqual(assistant.reports[0].reason, 'danger')
})

test('время задачи вышло — failed/timeout', async () => {
  const assistant = fakeAssistant([task('mine', { timeout_s: 1 })])
  const handlers = { mine: async (ctx, t, job) => { await job.sleep(5000) } }
  const runner = new Runner({ bot: fakeBot(), assistant, config, handlers, danger: () => null })
  await runner.step()
  assert.strictEqual(assistant.reports[0].reason, 'timeout')
})

test('«помощник, стоп» с сервера обрывает задачу', async () => {
  const assistant = fakeAssistant([task('mine')])
  const handlers = {
    mine: async (ctx, t, job) => {
      setTimeout(() => runner.onHello({ run_id: 'r1', run_status: 'stopped' }), 50)
      await job.sleep(5000)
    }
  }
  const runner = new Runner({ bot: fakeBot(), assistant, config, handlers, danger: () => null })
  await runner.step()
  assert.strictEqual(assistant.reports[0].reason, 'player_input')
})
