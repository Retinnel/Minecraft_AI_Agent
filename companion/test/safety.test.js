'use strict'
const test = require('node:test')
const assert = require('node:assert')
const { assess } = require('../src/safety')

test('спокойно — опасности нет', () => {
  assert.strictEqual(assess({ health: 20, food: 20, hostiles: [], lavaDistance: null }), null)
})

test('мало здоровья — пауза', () => {
  assert.match(assess({ health: 4, food: 20, minHealth: 6 }), /здоровья/)
})

test('голод — пауза', () => {
  assert.match(assess({ health: 20, food: 3 }), /голоден/)
})

test('враждебный моб близко, а далёкий не мешает', () => {
  assert.match(assess({ health: 20, food: 20, hostiles: [{ name: 'zombie', distance: 5 }] }), /zombie/)
  assert.strictEqual(assess({ health: 20, food: 20, hostiles: [{ name: 'zombie', distance: 20 }] }), null)
})

test('лава рядом', () => {
  assert.match(assess({ health: 20, food: 20, lavaDistance: 1.5 }), /лава/)
})
