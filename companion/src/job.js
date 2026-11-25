'use strict'
// Задача в работе: её можно прервать (опасность, стоп, время вышло) из монитора, и тогда
// любое ожидание внутри задачи (путь, копание, печь) обрывается с причиной.

class Interrupted extends Error {
  constructor (status, reason, detail) {
    super(detail || reason)
    this.status = status // failed | interrupted
    this.reason = reason // timeout | danger | player_input | ...
  }
}

class TaskFailed extends Error {
  constructor (reason, detail) {
    super(detail || reason)
    this.reason = reason // not_found | no_tool | blocked | unsupported
  }
}

class Job {
  constructor () {
    this.stopped = null
    this._listeners = []
  }

  abort (status, reason, detail) {
    if (this.stopped) return
    this.stopped = new Interrupted(status, reason, detail)
    for (const listener of this._listeners.splice(0)) listener(this.stopped)
  }

  check () {
    if (this.stopped) throw this.stopped
  }

  // Обещание, которое отклоняется при прерывании: для гонки с долгими действиями.
  get aborted () {
    if (!this._aborted) {
      this._aborted = new Promise((resolve, reject) => {
        if (this.stopped) return reject(this.stopped)
        this._listeners.push(reject)
      })
      this._aborted.catch(() => {})
    }
    return this._aborted
  }

  async race (promise, onAbort) {
    try {
      return await Promise.race([promise, this.aborted])
    } catch (error) {
      if (this.stopped && onAbort) {
        try { onAbort() } catch (_) {}
      }
      throw error
    }
  }

  async sleep (ms) {
    await this.race(new Promise(resolve => setTimeout(resolve, ms)))
  }
}

module.exports = { Job, Interrupted, TaskFailed }
