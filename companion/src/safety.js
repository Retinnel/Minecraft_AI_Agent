'use strict'
// Монитор опасности. Чистая функция над снимком состояния: что делать решает сервер,
// помощник лишь прерывает задачу и отчитывается «interrupted: danger».

const HOSTILE_RADIUS = 8
const LAVA_RADIUS = 2
const MIN_FOOD = 6

function assess ({ health, food, hostiles = [], lavaDistance = null, minHealth = 6 }) {
  if (typeof health === 'number' && health < minHealth) return `мало здоровья (${Math.round(health)})`
  if (typeof food === 'number' && food < MIN_FOOD) return `голоден (${food})`
  const near = hostiles.filter(mob => mob.distance <= HOSTILE_RADIUS)
  if (near.length) return `рядом враждебный моб: ${near[0].name}`
  if (lavaDistance !== null && lavaDistance <= LAVA_RADIUS) return 'рядом лава'
  return null
}

// Снимок из бота; отделён от assess, чтобы assess проверялся без игры.
function snapshot (bot, minHealth) {
  const hostiles = []
  for (const entity of Object.values(bot.entities)) {
    if (entity === bot.entity || entity.type !== 'hostile' || !entity.position) continue
    hostiles.push({ name: entity.name, distance: entity.position.distanceTo(bot.entity.position) })
  }
  let lavaDistance = null
  const lava = bot.registry.blocksByName.lava
  if (lava) {
    const found = bot.findBlock({ matching: lava.id, maxDistance: LAVA_RADIUS + 1 })
    if (found) lavaDistance = found.position.distanceTo(bot.entity.position)
  }
  return { health: bot.health, food: bot.food, hostiles, lavaDistance, minHealth }
}

module.exports = { assess, snapshot, HOSTILE_RADIUS, LAVA_RADIUS, MIN_FOOD }
