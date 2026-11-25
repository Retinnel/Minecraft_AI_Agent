'use strict'
// Отдать сделанное: подойти к игроку и бросить ему под ноги или положить в сундук.

const { goals } = require('mineflayer-pathfinder')
const { TaskFailed } = require('../job')
const { resync, strip, count, goto } = require('../world')
const { openAt, remember } = require('./take')

function nearestPlayer (bot, name) {
  const others = Object.values(bot.players)
    .filter(player => player.username !== bot.username && player.entity)
    .filter(player => !name || player.username === name)
  others.sort((a, b) => a.entity.position.distanceTo(bot.entity.position) - b.entity.position.distanceTo(bot.entity.position))
  return others[0] || null
}

async function toPlayer (ctx, task, job, item) {
  const { bot } = ctx
  for (let attempt = 0; attempt < 4; attempt++) {
    const player = nearestPlayer(bot, task.where && task.where !== 'player' ? task.where : null) || nearestPlayer(bot)
    if (!player) throw new TaskFailed('not_found', 'игрока не видно')
    const p = player.entity.position
    if (bot.entity.position.distanceTo(p) > 3) {
      await goto(bot, new goals.GoalNear(p.x, p.y, p.z, 2), job)
      continue
    }
    await bot.lookAt(p.offset(0, 1.2, 0), true)
    await bot.toss(item.id, null, Math.min(task.count, count(bot, item.name)))
    return
  }
  throw new TaskFailed('blocked', 'не догнал игрока')
}

async function toChest (ctx, task, job, item) {
  const { bot } = ctx
  if (!task.position) throw new TaskFailed('not_found', 'не знаю, где сундук')
  const { block, window } = await openAt(ctx, task.position, job)
  try {
    await window.deposit(item.id, null, Math.min(task.count, count(bot, item.name)))
    remember(ctx, block, window)
  } finally {
    window.close()
    await bot.waitForTicks(2)
    await resync(bot)
  }
}

async function deliver (ctx, task, job) {
  const { bot } = ctx
  const item = bot.registry.itemsByName[strip(task.item)]
  if (!item || count(bot, item.name) === 0) throw new TaskFailed('not_found', 'нечего отдавать')
  const before = count(bot, item.name)
  if (task.station === 'chest') await toChest(ctx, task, job, item)
  else await toPlayer(ctx, task, job, item)
  if (count(bot, item.name) >= before) throw new TaskFailed('blocked', 'предмет остался у помощника')
}

module.exports = { deliver, nearestPlayer }
