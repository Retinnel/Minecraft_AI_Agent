'use strict'
// Крафт щелчками, которые подтверждает сервер. Каждый щелчок уходит с заведомо неверным
// номером состояния окна: сервер выполняет его по своей модели и присылает окно целиком,
// так что следующий щелчок делается уже по настоящему содержимому, а не по догадке.
// Раскладка по сетке — по рецепту из данных игры (форма или список составляющих);
// для нескольких изготовлений в каждую клетку кладётся по стольку штук, а результат
// забирается одним щелчком с Shift.

const { once } = require('events')
const Item = require('prismarine-item')

const RESULT_SLOT = 0
const SYNC_MS = 1500
const MAX_RUNS = 64

class CraftError extends Error {}

function gridWidth (window) {
  return String(window.type || '').includes('crafting') ? 3 : 2
}

function gridSlots (window) {
  const width = gridWidth(window)
  return Array.from({ length: width * width }, (_, i) => i + 1)
}

// Клетки сетки, куда что класть. Чистая функция: проверяется без игры.
function layout (recipe, width) {
  const cells = []
  if (recipe.inShape) {
    if (recipe.inShape.length > width || recipe.inShape.some(row => row.length > width)) {
      throw new CraftError('рецепт не помещается в сетку')
    }
    recipe.inShape.forEach((row, y) => row.forEach((ingredient, x) => {
      const id = typeof ingredient === 'object' && ingredient !== null ? ingredient.id : ingredient
      if (id !== -1 && id !== null && id !== undefined) cells.push({ slot: 1 + x + width * y, id })
    }))
  } else {
    (recipe.ingredients || []).forEach((ingredient, i) => {
      const id = typeof ingredient === 'object' && ingredient !== null ? ingredient.id : ingredient
      cells.push({ slot: 1 + (i % width) + width * Math.floor(i / width), id })
    })
  }
  if (cells.some(cell => cell.slot > width * width)) throw new CraftError('рецепт не помещается в сетку')
  return cells
}

async function click (bot, window, slot, mouseButton, mode) {
  const synced = once(bot, `setWindowItems:${window.id}`)
  bot._client.write('window_click', {
    windowId: window.id,
    stateId: -1,
    slot,
    mouseButton,
    mode,
    changedSlots: [],
    cursorItem: Item(bot.registry).toNotch(window.selectedItem || null)
  })
  await Promise.race([synced, new Promise((resolve, reject) =>
    setTimeout(() => reject(new CraftError('сервер не ответил на щелчок')), SYNC_MS))])
}

function carried (window) {
  return window.selectedItem || null
}

async function dropCursor (bot, window) {
  // Что в руке курсора — в первый подходящий слот инвентаря окна.
  const item = carried(window)
  if (!item) return
  let target = null
  for (let slot = window.inventoryStart; slot < window.inventoryEnd; slot++) {
    const here = window.slots[slot]
    if (!here || (here.type === item.type && here.count < here.stackSize)) { target = slot; break }
  }
  if (target === null) throw new CraftError('инвентарь полон')
  await click(bot, window, target, 0, 0)
}

async function clearGrid (bot, window) {
  for (const slot of gridSlots(window)) {
    if (window.slots[slot]) await click(bot, window, slot, 0, 1)
  }
}

// Изготовить по рецепту (объект рецепта mineflayer) runs раз в окне window.
async function craftWith (bot, window, recipe, runs) {
  runs = Math.max(1, Math.min(MAX_RUNS, runs))
  const cells = layout(recipe, gridWidth(window))
  await clearGrid(bot, window)
  for (const cell of cells) {
    for (let n = 0; n < runs; n++) {
      if (!carried(window) || carried(window).type !== cell.id) {
        await dropCursor(bot, window)
        const source = window.findInventoryItem(cell.id, null)
        if (!source) throw new CraftError('не хватает составляющих')
        await click(bot, window, source.slot, 0, 0)
      }
      await click(bot, window, cell.slot, 1, 0) // правая кнопка — положить одну штуку
    }
  }
  await dropCursor(bot, window)
  if (!window.slots[RESULT_SLOT]) {
    await clearGrid(bot, window)
    throw new CraftError('сервер не принял раскладку')
  }
  await click(bot, window, RESULT_SLOT, 0, 1) // Shift — забрать всё изготовленное
  await clearGrid(bot, window)
}

module.exports = { craftWith, layout, gridSlots, gridWidth, CraftError }
