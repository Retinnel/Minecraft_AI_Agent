'use strict'
// Поставить станцию (верстак, печь) рядом с собой на ровное место.

const { Vec3 } = require('vec3')
const { TaskFailed } = require('../job')
const { strip, findStation, resync } = require('../world')

const AIR = new Set(['air', 'cave_air'])

// Места вокруг: опора — твёрдый блок, над ней два блока воздуха, не там, где стоит помощник.
function spots (bot) {
  const base = bot.entity.position.floored()
  const result = []
  for (let radius = 1; radius <= 3; radius++) {
    for (let dx = -radius; dx <= radius; dx++) {
      for (let dz = -radius; dz <= radius; dz++) {
        if (Math.max(Math.abs(dx), Math.abs(dz)) !== radius) continue
        for (const dy of [0, -1, 1]) {
          const target = base.offset(dx, dy, dz)
          const below = bot.blockAt(target.offset(0, -1, 0))
          const here = bot.blockAt(target)
          const above = bot.blockAt(target.offset(0, 1, 0))
          if (!below || !here || !above) continue
          if (below.boundingBox !== 'block' || !AIR.has(here.name)) continue
          if (radius === 1 && dx === 0 && dz === 0) continue
          result.push({ reference: below, target })
        }
      }
    }
  }
  return result
}

async function place (ctx, task, job) {
  const { bot } = ctx
  const name = strip(task.item)
  const item = bot.inventory.items().find(stack => stack.name === name)
  if (!item) throw new TaskFailed('not_found', `нет ${name} в инвентаре`)
  const station = task.station || name
  const before = findStation(bot, station, 4)
  for (const spot of spots(bot)) {
    job.check()
    // Нельзя ставить в клетку, где стоит сам помощник.
    const feet = bot.entity.position.floored()
    if (spot.target.x === feet.x && spot.target.z === feet.z && Math.abs(spot.target.y - feet.y) <= 1) continue
    try {
      await bot.equip(item, 'hand')
      await bot.lookAt(spot.target.offset(0.5, 0.5, 0.5), true)
      await job.race(bot.placeBlock(spot.reference, new Vec3(0, 1, 0)))
    } catch (error) {
      if (job.stopped) throw job.stopped
      // Ответ сервера мог не дойти — проверяем по миру.
    }
    const placed = bot.blockAt(spot.target)
    const found = findStation(bot, station, 4)
    if ((placed && placed.name === name) || (found && (!before || !found.position.equals(before.position)))) {
      await bot.waitForTicks(2)
      await resync(bot)
      return
    }
  }
  throw new TaskFailed('blocked', `негде поставить ${name}`)
}

module.exports = { place, spots }
