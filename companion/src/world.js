'use strict'
// Что помощник знает о себе и о том, что рядом: инвентарь, станции, позиция, путь.

const { goals } = require('mineflayer-pathfinder')
const { TaskFailed } = require('./job')

const NS = 'minecraft:'
const STATION_BLOCKS = { crafting_table: 'crafting_table', furnace: 'furnace' }
// Станция «рядом», если до неё можно дойти за несколько шагов: план не ставит новую.
const STATION_RADIUS = 16

const strip = name => String(name || '').replace(/^minecraft:/, '')
const full = name => (String(name).includes(':') ? String(name) : NS + name)

function inventory (bot) {
  const counts = {}
  for (const item of bot.inventory.items()) {
    counts[full(item.name)] = (counts[full(item.name)] || 0) + item.count
  }
  return counts
}

function count (bot, name) {
  const wanted = strip(name)
  return bot.inventory.items().filter(item => item.name === wanted)
    .reduce((sum, item) => sum + item.count, 0)
}

function position (bot) {
  const p = bot.entity.position
  return { x: Math.floor(p.x), y: Math.floor(p.y), z: Math.floor(p.z), dimension: dimension(bot) }
}

function dimension (bot) {
  const name = String(bot.game?.dimension || 'overworld')
  return name.includes(':') ? name : NS + name.replace(/^the_/, 'the_')
}

function findStation (bot, station, radius = STATION_RADIUS) {
  const block = bot.registry.blocksByName[STATION_BLOCKS[station] || station]
  if (!block) return null
  return bot.findBlock({ matching: block.id, maxDistance: radius })
}

function stations (bot) {
  return Object.keys(STATION_BLOCKS).filter(name => findStation(bot, name))
}

function snapshot (bot, extra = {}) {
  return {
    username: bot.username,
    inventory: inventory(bot),
    stations: stations(bot),
    position: position(bot),
    health: bot.health,
    food: bot.food,
    ...extra
  }
}

// Путь к цели; прерывание задачи останавливает поиск пути.
async function goto (bot, goal, job) {
  try {
    await job.race(bot.pathfinder.goto(goal), () => bot.pathfinder.setGoal(null))
  } catch (error) {
    if (job.stopped) throw job.stopped
    throw new TaskFailed('blocked', `не дойти: ${error.message}`)
  }
}

async function approach (bot, blockPosition, job, range = 3) {
  if (bot.entity.position.distanceTo(blockPosition.offset(0.5, 0.5, 0.5)) <= range) return
  await goto(bot, new goals.GoalNear(blockPosition.x, blockPosition.y, blockPosition.z, Math.max(1, range - 1)), job)
}

// Свежая модель инвентаря от сервера. После окон сундука и печи номер состояния у
// mineflayer устаревает, и сервер молча отвергает щелчки; запрос с заведомо неверным
// номером возвращает окно целиком.
async function resync (bot) {
  if (typeof bot._syncWindow !== 'function') return
  try {
    await Promise.race([bot._syncWindow(bot.inventory), new Promise(resolve => setTimeout(resolve, 1000))])
  } catch (_) {}
}

module.exports = {
  resync, NS, STATION_BLOCKS, STATION_RADIUS, strip, full, inventory, count, position, snapshot,
  findStation, stations, goto, approach
}
