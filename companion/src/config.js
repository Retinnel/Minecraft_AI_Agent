'use strict'
// Настройки помощника из окружения. Сервер ассистента передаёт их при запуске процесса
// (api/companion.py), при ручном запуске годятся умолчания: мир на этой же машине.

function intFrom (value, fallback, low, high) {
  const number = Number.parseInt(value, 10)
  if (!Number.isFinite(number)) return fallback
  return Math.min(high, Math.max(low, number))
}

function loadConfig (env = process.env) {
  return {
    assistantUrl: (env.ASSISTANT_URL || 'http://127.0.0.1:8000').replace(/\/+$/, ''),
    host: env.MC_HOST || '127.0.0.1',
    port: intFrom(env.MC_PORT, 25565, 1, 65535),
    username: (env.MC_USERNAME || 'Helper').slice(0, 16),
    version: env.MC_VERSION || '1.20.1',
    minHealth: intFrom(env.MIN_HEALTH, 6, 1, 20),
    scanRadius: intFrom(env.SCAN_RADIUS, 24, 4, 64),
    // Как часто спрашивать задачу, когда её нет, и слать пульс.
    idleMs: intFrom(env.IDLE_MS, 2000, 200, 60000),
    helloMs: intFrom(env.HELLO_MS, 2000, 500, 60000),
    scanMs: intFrom(env.SCAN_MS, 5000, 1000, 120000)
  }
}

module.exports = { loadConfig, intFrom }
