'use strict'
// Стоящий на месте «игрок» для замеров на сервере без клиента: к нему помощник несёт
// сделанное при доставке «drop». Сам ничего не делает, только находится в мире.

const mineflayer = require('mineflayer')

const bot = mineflayer.createBot({
  host: process.env.MC_HOST || '127.0.0.1',
  port: Number(process.env.MC_PORT || 25565),
  username: process.env.MC_USERNAME || 'Player',
  version: process.env.MC_VERSION || '1.20.1',
  auth: 'offline'
})
bot.once('spawn', () => process.stdout.write(`в игре: ${bot.username}\n`))
bot.on('playerCollect', (collector, item) => {
  if (collector === bot.entity) {
    const stack = item.getDroppedItem && item.getDroppedItem()
    process.stdout.write(`подобрал: ${stack ? stack.name + ' ×' + stack.count : 'предмет'}\n`)
  }
})
bot.on('end', () => process.exit(0))
process.on('SIGTERM', () => { bot.quit(); setTimeout(() => process.exit(0), 300) })
