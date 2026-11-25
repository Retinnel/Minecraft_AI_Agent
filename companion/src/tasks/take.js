'use strict'
// Взять предмет из сундука, который помнит память мира. Содержимое сундука при этом
// уходит в память с источником «companion» — так память поправляется по факту.

const { Vec3 } = require('vec3')
const { TaskFailed } = require('../job')
const { resync, strip, full, count, approach } = require('../world')

const CONTAINERS = new Set(['chest', 'trapped_chest', 'barrel'])

function contents (window) {
  const items = {}
  for (const item of window.containerItems()) items[full(item.name)] = (items[full(item.name)] || 0) + item.count
  return items
}

async function openAt (ctx, position, job) {
  const { bot } = ctx
  const where = new Vec3(position.x, position.y, position.z)
  await approach(bot, where, job, 3.5)
  const block = bot.blockAt(where)
  if (!block || !CONTAINERS.has(block.name)) {
    throw new TaskFailed('not_found', `сундука на ${where} нет`)
  }
  const window = await job.race(bot.openContainer(block))
  return { block, window }
}

function remember (ctx, block, window) {
  const p = block.position
  ctx.observer?.container({ block: full(block.name), x: p.x, y: p.y, z: p.z, items: contents(window) })
}

async function take (ctx, task, job) {
  const { bot } = ctx
  if (!task.position) throw new TaskFailed('not_found', 'не знаю, где сундук')
  const item = bot.registry.itemsByName[strip(task.item)]
  if (!item) throw new TaskFailed('unsupported', `неизвестный предмет ${task.item}`)
  const before = count(bot, item.name)
  const { block, window } = await openAt(ctx, task.position, job)
  try {
    const available = window.containerItems().filter(stack => stack.type === item.id)
      .reduce((sum, stack) => sum + stack.count, 0)
    const amount = Math.min(available, task.count)
    if (amount > 0) await window.withdraw(item.id, null, amount)
    remember(ctx, block, window)
  } finally {
    window.close()
    await bot.waitForTicks(2)
    await resync(bot)
  }
  // Взял меньше, чем просили, — план пересчитается по инвентарю; ничего — неудача.
  if (count(bot, item.name) <= before) throw new TaskFailed('not_found', `в сундуке нет ${item.name}`)
}

module.exports = { take, openAt, remember, contents }
