'use strict'
// Связь с сервером ассистента. Помощник ничего не решает сам: спрашивает следующую задачу
// (/next), отчитывается о ней (/report) своим инвентарём и шлёт увиденное в память мира.

class Assistant {
  constructor (baseUrl, { fetchImpl = globalThis.fetch, timeoutMs = 15000 } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, '') + '/api/companion'
    this.fetch = fetchImpl
    this.timeoutMs = timeoutMs
  }

  async post (path, body) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), this.timeoutMs)
    try {
      const response = await this.fetch(this.baseUrl + path, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body || {}),
        signal: controller.signal
      })
      if (!response.ok) {
        const text = await response.text().catch(() => '')
        throw new Error(`${path}: HTTP ${response.status} ${text.slice(0, 200)}`)
      }
      return await response.json()
    } finally {
      clearTimeout(timer)
    }
  }

  hello (snapshot) { return this.post('/hello', snapshot) }
  next (snapshot) { return this.post('/next', snapshot) }
  report (report) { return this.post('/report', report) }
  observations (payload) { return this.post('/observations', payload) }
  container (payload) { return this.post('/containers', payload) }
}

module.exports = { Assistant }
