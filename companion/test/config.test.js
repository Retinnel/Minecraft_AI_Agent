'use strict'
const test = require('node:test')
const assert = require('node:assert')
const { loadConfig } = require('../src/config')

test('умолчания: мир на этой машине, имя Helper', () => {
  const config = loadConfig({})
  assert.strictEqual(config.host, '127.0.0.1')
  assert.strictEqual(config.port, 25565)
  assert.strictEqual(config.username, 'Helper')
})

test('порт LAN и границы значений', () => {
  const config = loadConfig({ MC_PORT: '54321', MIN_HEALTH: '99', MC_USERNAME: 'ОченьДлинноеИмяПомощника' })
  assert.strictEqual(config.port, 54321)
  assert.strictEqual(config.minHealth, 20)
  assert.ok(config.username.length <= 16)
})
