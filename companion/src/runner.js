'use strict'
// Цикл помощника: спросить задачу → исполнить с пределом времени и монитором опасности →
// отчитаться инвентарём. Решает сервер (план, повторы, обход, пауза); здесь только руки.

const { Job, Interrupted, TaskFailed } = require('./job')
const { HANDLERS } = require('./tasks')
const safety = require('./safety')
const world = require('./world')

const MONITOR_MS = 500

class Runner {
  constructor ({ bot, assistant, observer, config, log, handlers = HANDLERS, danger = null }) {
    this.bot = bot
    this.assistant = assistant
    this.observer = observer
    this.config = config
    this.log = log || (() => {})
    this.handlers = handlers
    // Проверка опасности подменяется в тестах; по умолчанию — снимок из игры.
    this.danger = danger || (() => safety.assess(safety.snapshot(bot, config.minHealth)))
    this.runId = null
    this.runStatus = null
    this.job = null
    this.closed = false
  }

  // Пульс от сервера: если запуск остановлен («помощник, стоп»), текущая задача обрывается.
  onHello (reply) {
    this.runId = reply?.run_id || null
    this.runStatus = reply?.run_status || null
    if (this.job && this.runStatus !== 'running') {
      this.job.abort('interrupted', 'player_input', 'запуск остановлен')
    }
  }

  async step () {
    const reply = await this.assistant.next(world.snapshot(this.bot, { run_id: this.runId }))
    if (reply?.status === 'running' && reply.task) {
      await this.execute(reply.task)
      return true
    }
    return false
  }

  async execute (task) {
    const job = new Job()
    this.job = job
    const started = Date.now()
    const timer = setTimeout(() => job.abort('failed', 'timeout', 'время задачи вышло'),
      Math.max(1, task.timeout_s || 60) * 1000)
    const monitor = setInterval(() => {
      const why = this.danger()
      if (why) job.abort('interrupted', 'danger', why)
    }, MONITOR_MS)
    const ctx = { bot: this.bot, observer: this.observer, log: this.log, config: this.config }
    let status = 'done'
    let reason = null
    this.log(`задача ${task.task_id}: ${task.goal_step || task.kind}`)
    try {
      const handler = this.handlers[task.kind]
      if (!handler) throw new TaskFailed('unsupported', `вид задачи ${task.kind}`)
      const early = this.danger()
      if (early) throw new Interrupted('interrupted', 'danger', early)
      await handler(ctx, task, job)
      if (job.stopped) throw job.stopped
    } catch (error) {
      if (error instanceof Interrupted) {
        status = error.status
        reason = error.reason
      } else if (error instanceof TaskFailed) {
        status = 'failed'
        reason = error.reason
      } else {
        status = 'failed'
        reason = 'blocked'
      }
      this.log(`задача ${task.task_id}: ${status} (${reason}) — ${error.message}`)
    } finally {
      clearTimeout(timer)
      clearInterval(monitor)
      this.job = null
      try {
        this.bot.pathfinder?.setGoal(null)
        this.bot.clearControlStates?.()
      } catch (_) {}
    }
    if (status === 'done') this.log(`задача ${task.task_id}: готово за ${Math.round((Date.now() - started) / 1000)} с`)
    const report = {
      run_id: this.runId,
      task_id: task.task_id,
      status,
      reason,
      ...world.snapshot(this.bot),
      elapsed_ms: Date.now() - started
    }
    return this.assistant.report(report)
  }

  async loop (sleep = ms => new Promise(resolve => setTimeout(resolve, ms))) {
    while (!this.closed) {
      let busy = false
      try {
        busy = await this.step()
      } catch (error) {
        this.log(`связь с сервером: ${error.message}`)
      }
      if (!busy) await sleep(this.config.idleMs)
    }
  }
}

module.exports = { Runner }
