'use strict'
// Крафт в руках или на верстаке поблизости. Что делать — решил план; вариант рецепта игры
// подбирается под то, что лежит в инвентаре (доски любой породы), раскладка — src/crafting.js.

const { once } = require('events')
const { TaskFailed } = require('../job')
const { craftWith } = require('../crafting')
const { strip, count, findStation, approach, resync } = require('../world')

async function openTable (bot, block, job) {
  const opened = once(bot, 'windowOpen')
  await bot.lookAt(block.position.offset(0.5, 0.5, 0.5), true)
  bot.activateBlock(block)
  const [window] = await job.race(Promise.race([
    opened,
    new Promise((resolve, reject) => setTimeout(() => reject(new TaskFailed('blocked', 'верстак не открылся')), 5000))
  ]))
  await bot.waitForTicks(2)
  return window
}

async function craft (ctx, task, job) {
  const { bot } = ctx
  const item = bot.registry.itemsByName[strip(task.item)]
  if (!item) throw new TaskFailed('unsupported', `неизвестный предмет ${task.item}`)
  let table = null
  if (task.station && task.station !== 'inventory') {
    table = findStation(bot, task.station)
    if (!table) throw new TaskFailed('not_found', 'рядом нет верстака')
    await approach(bot, table.position, job, 3.5)
    table = bot.blockAt(table.position)
  }
  await resync(bot)
  const before = count(bot, item.name)
  const target = before + Math.max(1, task.count || 1)
  const recipes = bot.recipesFor(item.id, null, 1, table)
  if (!recipes.length) throw new TaskFailed('not_found', `не хватает состава для ${item.name}`)
  const recipe = recipes[0]
  const perRun = Math.max(1, recipe.result?.count || 1)
  const runs = Math.max(1, Math.ceil((target - before) / perRun))
  const window = table ? await openTable(bot, table, job) : bot.inventory
  try {
    await job.race(craftWith(bot, window, recipe, runs))
  } catch (error) {
    if (job.stopped) throw job.stopped
    ctx.log(`крафт ${item.name}: ${error.message}`)
  } finally {
    if (table && bot.currentWindow) bot.closeWindow(bot.currentWindow)
    await bot.waitForTicks(2)
    await resync(bot)
  }
  if (count(bot, item.name) <= before) {
    throw new TaskFailed('not_found', `не вышло изготовить ${item.name}`)
  }
}

module.exports = { craft }
