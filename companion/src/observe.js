'use strict'
// Наблюдатель: руды и прочее из списка сервера, которые помощник видит вокруг, и сундуки,
// которые он открывал, уходят в память мира (источник «companion»). Шлётся только
// изменившееся: новые блоки и места, где блока больше нет.

const { full } = require('./world')

const DEFAULT_SCAN = [
  'coal_ore', 'deepslate_coal_ore', 'iron_ore', 'deepslate_iron_ore', 'copper_ore',
  'deepslate_copper_ore', 'gold_ore', 'deepslate_gold_ore', 'redstone_ore', 'lapis_ore',
  'diamond_ore', 'deepslate_diamond_ore', 'emerald_ore'
]
const MAX_PER_SCAN = 256

const key = entry => `${entry.x},${entry.y},${entry.z}`

// Что нового по сравнению с прошлым снимком: добавленные блоки (или сменившие вид).
function fresh (previous, current) {
  return current.filter(entry => previous.get(key(entry)) !== entry.id)
}

class Observer {
  constructor ({ bot, assistant, radius, log }) {
    this.bot = bot
    this.assistant = assistant
    this.radius = radius
    this.log = log || (() => {})
    this.scanList = DEFAULT_SCAN.map(full)
    this.seen = new Map()
    this.gone = []
  }

  useScanList (blocks) {
    if (Array.isArray(blocks) && blocks.length) this.scanList = blocks.map(full)
  }

  scan () {
    const bot = this.bot
    const ids = this.scanList
      .map(name => bot.registry.blocksByName[name.replace(/^minecraft:/, '')]?.id)
      .filter(id => id !== undefined)
    if (!ids.length) return []
    const dimension = String(bot.game?.dimension || 'overworld')
    return bot.findBlocks({ matching: ids, maxDistance: this.radius, count: MAX_PER_SCAN }).map(p => ({
      id: full(bot.blockAt(p)?.name || ''), x: p.x, y: p.y, z: p.z,
      dimension: dimension.includes(':') ? dimension : 'minecraft:' + dimension
    })).filter(entry => entry.id !== 'minecraft:')
  }

  removed (position, name) {
    const entry = { x: position.x, y: position.y, z: position.z }
    this.seen.delete(key(entry))
    if (this.scanList.includes(full(name))) this.gone.push(entry)
  }

  container (payload) {
    this.assistant.container({ ...payload, source: 'companion' })
      .catch(error => this.log(`память: сундук не записан (${error.message})`))
  }

  async tick () {
    const current = this.scan()
    const added = fresh(this.seen, current)
    const removed = this.gone.splice(0)
    if (!added.length && !removed.length) return { added: 0, removed: 0 }
    for (const entry of added) this.seen.set(key(entry), entry.id)
    try {
      await this.assistant.observations({ blocks: added, removed, source: 'companion' })
    } catch (error) {
      this.log(`память: наблюдения не записаны (${error.message})`)
      for (const entry of added) this.seen.delete(key(entry))
      this.gone.push(...removed)
    }
    return { added: added.length, removed: removed.length }
  }
}

module.exports = { Observer, fresh, DEFAULT_SCAN }
