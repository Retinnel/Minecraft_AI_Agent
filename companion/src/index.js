'use strict'
// Помощник — отдельный игрок в мире, открытом для сети. Заходит без авторизации
// (offline), слушает сервер ассистента и исполняет задачи плана по одной.
// Чат и команды игры не используются: помощник только ходит, копает, крафтит и отдаёт.

const mineflayer = require('mineflayer')
const { pathfinder, Movements } = require('mineflayer-pathfinder')
const { loadConfig } = require('./config')
const { Assistant } = require('./protocol')
const { Observer } = require('./observe')
const { Runner } = require('./runner')
const world = require('./world')

// Помощник не ломает чужое: станции, сундуки, кровати, двери, стекло.
const PROTECTED = ['chest', 'trapped_chest', 'barrel', 'furnace', 'blast_furnace', 'smoker',
  'crafting_table', 'ender_chest', 'glass', 'glass_pane', 'torch', 'wall_torch', 'ladder']

function log (text) {
  process.stdout.write(`[${new Date().toLocaleTimeString('ru-RU', { hour12: false })}] ${text}\n`)
}

function movements (bot) {
  const moves = new Movements(bot)
  moves.canDig = true
  moves.allowSprinting = true
  moves.allowParkour = false
  for (const name of Object.keys(bot.registry.blocksByName)) {
    if (PROTECTED.includes(name) || name.endsWith('_bed') || name.endsWith('_door') || name.endsWith('_shulker_box')) {
      moves.blocksCantBreak.add(bot.registry.blocksByName[name].id)
    }
  }
  return moves
}

function start (config) {
  const assistant = new Assistant(config.assistantUrl)
  const bot = mineflayer.createBot({
    host: config.host,
    port: config.port,
    username: config.username,
    version: config.version,
    auth: 'offline',
    hideErrors: false
  })
  bot.loadPlugin(pathfinder)
  const timers = []
  let runner = null

  bot.once('spawn', async () => {
    // Пока чанки вокруг не пришли, путь не строится («слишком долго решал»).
    try {
      await Promise.race([bot.waitForChunksToLoad(), new Promise(resolve => setTimeout(resolve, 15000))])
    } catch (_) {}
    log(`в игре: ${bot.username} на ${config.host}:${config.port}, ${world.position(bot).x} ${world.position(bot).y} ${world.position(bot).z}`)
    bot.pathfinder.setMovements(movements(bot))
    bot.pathfinder.thinkTimeout = 10000
    const observer = new Observer({ bot, assistant, radius: config.scanRadius, log })
    runner = new Runner({ bot, assistant, observer, config, log })
    const hello = async () => {
      try {
        const reply = await assistant.hello(world.snapshot(bot))
        observer.useScanList(reply?.scan)
        runner.onHello(reply)
      } catch (error) {
        log(`пульс не дошёл: ${error.message}`)
      }
    }
    hello()
    timers.push(setInterval(hello, config.helloMs))
    timers.push(setInterval(() => observer.tick().catch(() => {}), config.scanMs))
    runner.loop()
  })

  bot.on('death', () => log('помощник погиб'))
  bot.on('kicked', reason => log(`выкинут с сервера: ${JSON.stringify(reason).slice(0, 200)}`))
  bot.on('error', error => log(`ошибка: ${error.message}`))
  bot.once('end', reason => {
    log(`отключён (${reason}); переподключение через 5 с`)
    for (const timer of timers) clearInterval(timer)
    if (runner) runner.closed = true
    if (!stopping) setTimeout(() => start(config), 5000)
  })
  current = bot
  return bot
}

let stopping = false
let current = null

function stop () {
  stopping = true
  try { current?.quit() } catch (_) {}
  setTimeout(() => process.exit(0), 500)
}

if (require.main === module) {
  process.on('SIGTERM', stop)
  process.on('SIGINT', stop)
  start(loadConfig())
}

module.exports = { start, movements, PROTECTED }
