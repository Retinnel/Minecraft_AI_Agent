'use strict'
const test = require('node:test')
const assert = require('node:assert')
const { Observer, fresh } = require('../src/observe')

test('новое — только то, чего не было или что сменилось', () => {
  const seen = new Map([['1,2,3', 'minecraft:iron_ore']])
  const current = [
    { id: 'minecraft:iron_ore', x: 1, y: 2, z: 3 },
    { id: 'minecraft:coal_ore', x: 4, y: 5, z: 6 }
  ]
  assert.deepStrictEqual(fresh(seen, current).map(e => e.id), ['minecraft:coal_ore'])
})

test('увиденное уходит в память один раз, выкопанное — как убранное', async () => {
  const sent = []
  const assistant = { observations: async payload => { sent.push(payload); return { ok: true } } }
  const observer = new Observer({ bot: null, assistant, radius: 16 })
  let blocks = [{ id: 'minecraft:iron_ore', x: 1, y: 2, z: 3 }]
  observer.scan = () => blocks
  await observer.tick()
  await observer.tick()
  assert.strictEqual(sent.length, 1)
  assert.strictEqual(sent[0].source, 'companion')
  observer.removed({ x: 1, y: 2, z: 3 }, 'iron_ore')
  blocks = []
  await observer.tick()
  assert.deepStrictEqual(sent[1].removed, [{ x: 1, y: 2, z: 3 }])
})

test('сбой связи — наблюдения повторятся', async () => {
  let fail = true
  const sent = []
  const assistant = { observations: async payload => { if (fail) throw new Error('нет связи'); sent.push(payload) } }
  const observer = new Observer({ bot: null, assistant, radius: 16 })
  observer.scan = () => [{ id: 'minecraft:coal_ore', x: 0, y: 0, z: 0 }]
  await observer.tick()
  fail = false
  await observer.tick()
  assert.strictEqual(sent.length, 1)
})
