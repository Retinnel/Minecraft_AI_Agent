'use strict'
// Плавка: топливо и сырьё в ближайшую печь, ждать, забрать результат.

const { TaskFailed } = require('../job')
const { resync, strip, count, findStation, approach } = require('../world')

const POLL_MS = 500

async function smelt (ctx, task, job) {
  const { bot } = ctx
  const output = bot.registry.itemsByName[strip(task.item)]
  const input = bot.registry.itemsByName[strip(task.input)]
  const fuel = task.fuel ? bot.registry.itemsByName[strip(task.fuel)] : null
  if (!output || !input) throw new TaskFailed('unsupported', 'неизвестное сырьё или результат')
  const block = findStation(bot, 'furnace')
  if (!block) throw new TaskFailed('not_found', 'рядом нет печи')
  await approach(bot, block.position, job, 3.5)
  const before = count(bot, output.name)
  const target = before + Math.max(1, task.count)
  const furnace = await job.race(bot.openFurnace(bot.blockAt(block.position)))
  try {
    const current = furnace.fuelItem()
    if (fuel && task.fuel_count > 0) {
      if (current && current.type !== fuel.id) await furnace.takeFuel()
      if (count(bot, fuel.name) < task.fuel_count) throw new TaskFailed('not_found', `нет топлива ${fuel.name}`)
      await furnace.putFuel(fuel.id, null, task.fuel_count)
    }
    const queued = furnace.inputItem()
    if (queued && queued.type !== input.id) await furnace.takeInput()
    const have = count(bot, input.name)
    if (!have) throw new TaskFailed('not_found', `нет сырья ${input.name}`)
    await furnace.putInput(input.id, null, Math.min(have, task.count))
    let idle = 0
    while (count(bot, output.name) < target) {
      await job.sleep(POLL_MS)
      const ready = furnace.outputItem()
      if (ready) {
        await furnace.takeOutput()
        idle = 0
        continue
      }
      // Печь погасла и сырьё не убывает — топлива не хватило.
      if (!furnace.inputItem()) break
      idle = furnace.fuel > 0 || furnace.progress > 0 ? 0 : idle + 1
      if (idle > 20) throw new TaskFailed('blocked', 'печь не горит')
    }
  } finally {
    furnace.close()
    await bot.waitForTicks(2)
    await resync(bot)
  }
  if (count(bot, output.name) <= before) throw new TaskFailed('blocked', 'ничего не переплавилось')
}

module.exports = { smelt }
