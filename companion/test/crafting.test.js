'use strict'
const test = require('node:test')
const assert = require('node:assert')
const { layout, gridSlots, CraftError } = require('../src/crafting')

const P = 36 // любой номер предмета: раскладке важна только форма

test('форма кирки на верстаке: три сверху, две по центру', () => {
  const recipe = { inShape: [[{ id: P }, { id: P }, { id: P }], [{ id: -1 }, { id: 7 }, { id: -1 }], [{ id: -1 }, { id: 7 }, { id: -1 }]] }
  const cells = layout(recipe, 3)
  assert.deepStrictEqual(cells.map(c => c.slot), [1, 2, 3, 5, 8])
  assert.deepStrictEqual(cells.filter(c => c.id === 7).map(c => c.slot), [5, 8])
})

test('палка в своей сетке 2×2: столбик', () => {
  const cells = layout({ inShape: [[{ id: P }], [{ id: P }]] }, 2)
  assert.deepStrictEqual(cells.map(c => c.slot), [1, 3])
})

test('бесформенный рецепт раскладывается по порядку', () => {
  const cells = layout({ ingredients: [{ id: 1 }, { id: 2 }, { id: 3 }] }, 2)
  assert.deepStrictEqual(cells.map(c => c.slot), [1, 2, 3])
})

test('рецепт 3×3 в сетку 2×2 не помещается', () => {
  const recipe = { inShape: [[{ id: P }, { id: P }, { id: P }]] }
  assert.throws(() => layout(recipe, 2), CraftError)
})

test('сетка: своя 2×2 и верстак 3×3', () => {
  assert.deepStrictEqual(gridSlots({ type: 'minecraft:inventory' }), [1, 2, 3, 4])
  assert.strictEqual(gridSlots({ type: 'minecraft:crafting' }).length, 9)
})
