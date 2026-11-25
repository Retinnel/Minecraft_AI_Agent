'use strict'
// Добыча: найти блок (сначала там, где его видела память мира), подойти, взять лучший
// инструмент, выкопать, подобрать выпавшее. Считается то, что выпадает из блоков задачи:
// бревно любой породы, булыжник из камня.

const { goals } = require('mineflayer-pathfinder')
const { Vec3 } = require('vec3')
const { TaskFailed } = require('../job')
const { bestTool, dropsOf } = require('../tools')
const { strip, count, goto } = require('../world')

const SEARCH_RADIUS = 48
const MAX_FAILED_BLOCKS = 10
const MAX_WANDERS = 5
const REACH = 4.2

const key = p => `${p.x},${p.y},${p.z}`

function candidates (bot, ids, failed, preferred) {
  const origin = bot.entity.position
  const found = bot.findBlocks({ matching: ids, maxDistance: SEARCH_RADIUS, count: 120 })
  return found
    .filter(p => !failed.has(key(p)))
    .map(p => {
      const block = bot.blockAt(p)
      // Выше головы без лестницы не дотянуться, глубоко под ногами — долго копать.
      const climb = Math.max(0, p.y - origin.y - 2)
      const depth = Math.max(0, origin.y - p.y - 3)
      const exposed = isExposed(bot, p) ? 0 : 6
      const penalty = block && preferred.has(block.type) ? 0 : 20
      return { p, score: p.distanceTo(origin) + climb * 4 + depth * 1.5 + exposed + penalty }
    })
    .sort((a, b) => a.score - b.score)
    .map(entry => entry.p)
}

function isExposed (bot, p) {
  for (const [dx, dy, dz] of [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]]) {
    const near = bot.blockAt(p.offset(dx, dy, dz))
    if (near && (near.name === 'air' || near.name === 'cave_air')) return true
  }
  return false
}

async function wander (bot, job, attempt) {
  // Ничего подходящего рядом — уйти в сторону и поискать снова.
  const angle = (attempt * 137.5 + Math.random() * 40) * Math.PI / 180
  const distance = 24 + attempt * 8
  const p = bot.entity.position
  const goal = new goals.GoalXZ(Math.floor(p.x + Math.cos(angle) * distance), Math.floor(p.z + Math.sin(angle) * distance))
  try {
    await goto(bot, goal, job)
  } catch (error) {
    if (job.stopped) throw error
  }
}

async function collectDrops (bot, near, job) {
  await job.sleep(250)
  for (let pass = 0; pass < 3; pass++) {
    const drops = Object.values(bot.entities).filter(entity =>
      entity.name === 'item' && entity.position && entity.position.distanceTo(near) < 6)
    if (!drops.length) return
    const target = drops.sort((a, b) => a.position.distanceTo(bot.entity.position) - b.position.distanceTo(bot.entity.position))[0]
    try {
      await goto(bot, new goals.GoalNear(target.position.x, target.position.y, target.position.z, 0.6), job)
    } catch (error) {
      if (job.stopped) throw error
      return
    }
    await job.sleep(300)
  }
}

async function digAt (ctx, position, ids, job) {
  const { bot } = ctx
  const block = bot.blockAt(position)
  if (!block || !ids.includes(block.type)) return false
  const tool = bestTool(block, bot.inventory.items(), bot.registry.materials)
  if (!tool.canHarvest) throw new TaskFailed('no_tool', `нечем добыть ${block.name}`)
  if (bot.entity.position.offset(0, 1.6, 0).distanceTo(position.offset(0.5, 0.5, 0.5)) > REACH) {
    await goto(bot, new goals.GoalLookAtBlock(position, bot.world, { reach: REACH }), job)
  }
  job.check()
  const fresh = bot.blockAt(position)
  if (!fresh || !ids.includes(fresh.type)) return false
  if (tool.item) await bot.equip(tool.item, 'hand')
  if (!bot.canDigBlock(fresh)) throw new TaskFailed('blocked', `не достать ${fresh.name}`)
  await job.race(bot.dig(fresh, true), () => bot.stopDigging())
  ctx.observer?.removed(position, fresh.name)
  await collectDrops(bot, position.offset(0.5, 0.5, 0.5), job)
  return true
}

// Шахта вниз: камень обычно в нескольких блоках под травой, а путь «с копанием» к
// невидимому блоку ищется долго. Копаем под ногами до нужного блока, затем стенки шахты
// на уровне ног и головы — несколько блоков без единого шага.
const UNSAFE_BELOW = new Set(['lava', 'water'])

async function shaft (ctx, ids, job, enough, maxDepth = 12) {
  const { bot } = ctx
  for (let depth = 0; depth < maxDepth && !enough(); depth++) {
    job.check()
    const feet = bot.entity.position.floored()
    const below = bot.blockAt(feet.offset(0, -1, 0))
    const under = bot.blockAt(feet.offset(0, -2, 0))
    if (!below || below.name === 'bedrock') break
    if (under && UNSAFE_BELOW.has(under.name)) throw new TaskFailed('blocked', 'под шахтой лава или вода')
    if (await walls(ctx, feet, ids, job, enough)) continue
    if (below.boundingBox === 'block') {
      const tool = bestTool(below, bot.inventory.items(), bot.registry.materials)
      if (!tool.canHarvest && ids.includes(below.type)) throw new TaskFailed('no_tool', `нечем добыть ${below.name}`)
      if (tool.item) await bot.equip(tool.item, 'hand')
      if (!bot.canDigBlock(below)) break
      await job.race(bot.dig(below, true), () => bot.stopDigging())
    }
    // Ждём, пока помощник опустится на дно.
    for (let tick = 0; tick < 20 && bot.entity.position.y > feet.y - 0.5; tick++) await bot.waitForTicks(1)
    await collectDrops(bot, bot.entity.position, job)
  }
  await walls(ctx, bot.entity.position.floored(), ids, job, enough)
}

async function walls (ctx, feet, ids, job, enough) {
  let dug = false
  for (const dy of [0, 1, -1]) {
    for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      if (enough()) return dug
      if (dy === -1 && (dx !== 0 || dz !== 0)) continue
      const p = feet.offset(dx, dy, dz)
      const block = ctx.bot.blockAt(p)
      if (block && ids.includes(block.type)) {
        try {
          if (await digAt(ctx, p, ids, job)) dug = true
        } catch (error) {
          if (job.stopped || error.reason === 'no_tool') throw job.stopped || error
        }
      }
    }
  }
  return dug
}

async function mine (ctx, task, job) {
  const { bot } = ctx
  const names = (task.blocks && task.blocks.length ? task.blocks : [task.item]).map(strip)
  const ids = names.map(name => bot.registry.blocksByName[name]?.id).filter(id => id !== undefined)
  if (!ids.length) throw new TaskFailed('unsupported', `неизвестные блоки: ${names.join(', ')}`)
  const wanted = strip(task.item)
  const drops = dropsOf(names, bot.registry)
  drops.add(wanted)
  // Предпочтительны блоки, из которых выпадает ровно нужный предмет (камень, а не сланец).
  const preferred = new Set(ids.filter(id => dropsOf([bot.registry.blocks[id].name], bot.registry).has(wanted)))
  const start = {}
  for (const name of drops) start[name] = count(bot, name)
  const gained = () => [...drops].reduce((sum, name) => sum + Math.max(0, count(bot, name) - start[name]), 0)

  const failed = new Set()
  let remembered = task.position ? new Vec3(task.position.x, task.position.y, task.position.z) : null
  let wanders = 0
  let shafted = false
  while (gained() < task.count) {
    job.check()
    let target = null
    if (remembered) {
      // Память мира: руда, которую видел сканер или сам помощник. Идём туда.
      if (bot.entity.position.distanceTo(remembered) > 12) {
        try {
          await goto(bot, new goals.GoalNear(remembered.x, remembered.y, remembered.z, 3), job)
        } catch (error) {
          if (job.stopped) throw error
        }
      }
      const block = bot.blockAt(remembered)
      if (block && ids.includes(block.type)) target = remembered
      else if (block) ctx.observer?.removed(remembered, names[0])
      remembered = null
    }
    if (!target) {
      const found = candidates(bot, ids, failed, preferred)
      const origin = bot.entity.position
      // Открытый блок неподалёку — идём к нему; иначе, если нужное под ногами, — шахта.
      target = found.find(p => isExposed(bot, p) && p.distanceTo(origin) < 24) || null
      if (!target && !shafted && found.some(p => p.y < origin.y - 1 && p.y > origin.y - 12 &&
          Math.abs(p.x - origin.x) + Math.abs(p.z - origin.z) < 16)) {
        shafted = true
        try {
          await shaft(ctx, ids, job, () => gained() >= task.count)
        } catch (error) {
          if (job.stopped || !(error instanceof TaskFailed) || error.reason === 'no_tool') throw job.stopped || error
          ctx.log(`шахта: ${error.message}`)
        }
        continue
      }
      if (!target) target = found[0] || null
    }
    if (!target) {
      if (wanders >= MAX_WANDERS) throw new TaskFailed('not_found', `не нашёл ${names[0]}`)
      await wander(bot, job, wanders++)
      continue
    }
    try {
      const dug = await digAt(ctx, target, ids, job)
      if (!dug) failed.add(key(target))
    } catch (error) {
      if (job.stopped || !(error instanceof TaskFailed)) throw job.stopped || error
      if (error.reason === 'no_tool') throw error
      failed.add(key(target))
      ctx.log(`добыча: ${error.message}`)
      if (failed.size >= MAX_FAILED_BLOCKS) throw error
    }
  }
}

module.exports = { mine, candidates, isExposed }
