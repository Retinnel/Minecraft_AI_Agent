'use strict'
// Белый список задач. Всё, чего здесь нет, помощник не делает: отчёт «unsupported».

const { mine } = require('./mine')
const { craft } = require('./craft')
const { smelt } = require('./smelt')
const { place } = require('./place')
const { take } = require('./take')
const { deliver } = require('./deliver')

const HANDLERS = {
  mine,
  craft,
  smelt,
  place_station: place,
  take_from_container: take,
  deliver
}

module.exports = { HANDLERS }
