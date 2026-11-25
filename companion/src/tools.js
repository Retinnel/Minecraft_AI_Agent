'use strict'
// Выбор инструмента для блока — по данным игры (minecraft-data): какие предметы вообще
// добывают блок (harvestTools) и с какой скоростью (materials). Чистая функция.

// Возвращает { item, speed, canHarvest }: item — предмет инвентаря или null (рука).
function bestTool (block, items, materials) {
  const required = block.harvestTools ? new Set(Object.keys(block.harvestTools).map(Number)) : null
  const speeds = {}
  for (const material of String(block.material || '').split(';')) {
    Object.assign(speeds, materials[material] || {})
  }
  let best = null
  let bestSpeed = 1
  for (const item of items) {
    const speed = speeds[item.type] || speeds[String(item.type)]
    if (required && !required.has(item.type)) continue
    if (speed && speed > bestSpeed) {
      best = item
      bestSpeed = speed
    }
  }
  if (required && !best) {
    // Подходит и предмет без скорости из списка (на всякий случай — данные бывают неполны).
    best = items.find(item => required.has(item.type)) || null
  }
  return { item: best, speed: bestSpeed, canHarvest: !required || Boolean(best) }
}

// Что выпадает из блоков задачи: по этому считается добытое (бревно любого дерева,
// булыжник из камня).
function dropsOf (blockNames, mcData) {
  const names = new Set()
  for (const name of blockNames) {
    const block = mcData.blocksByName[name]
    if (!block) continue
    for (const drop of block.drops || []) {
      const id = typeof drop === 'object' ? (drop.drop?.id ?? drop.id) : drop
      const item = mcData.items[id]
      if (item) names.add(item.name)
    }
  }
  return names
}

module.exports = { bestTool, dropsOf }
