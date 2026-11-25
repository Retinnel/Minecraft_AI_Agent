'use strict'
const test = require('node:test')
const assert = require('node:assert')
const mcData = require('minecraft-data')('1.20.1')
const { bestTool, dropsOf } = require('../src/tools')

const item = name => ({ name, type: mcData.itemsByName[name].id })

test('камень: берётся лучшая кирка из имеющихся', () => {
  const choice = bestTool(mcData.blocksByName.stone, [item('wooden_pickaxe'), item('stone_pickaxe'), item('oak_planks')], mcData.materials)
  assert.strictEqual(choice.item.name, 'stone_pickaxe')
  assert.ok(choice.canHarvest)
})

test('железная руда деревянной киркой не добывается', () => {
  const choice = bestTool(mcData.blocksByName.iron_ore, [item('wooden_pickaxe')], mcData.materials)
  assert.strictEqual(choice.canHarvest, false)
})

test('бревно можно рукой, топор быстрее', () => {
  assert.strictEqual(bestTool(mcData.blocksByName.oak_log, [], mcData.materials).canHarvest, true)
  const choice = bestTool(mcData.blocksByName.oak_log, [item('wooden_axe'), item('wooden_pickaxe')], mcData.materials)
  assert.strictEqual(choice.item.name, 'wooden_axe')
})

test('выпадение: камень даёт булыжник, руда — необработанное железо', () => {
  assert.deepStrictEqual([...dropsOf(['stone'], mcData)], ['cobblestone'])
  assert.deepStrictEqual([...dropsOf(['iron_ore'], mcData)], ['raw_iron'])
  assert.ok(dropsOf(['oak_log', 'birch_log'], mcData).has('birch_log'))
})
