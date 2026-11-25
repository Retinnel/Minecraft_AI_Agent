"""Режим «Сделай сам»: запуск, подтверждение, выдача задач моду, отчёты, остановка.

Сервер ведёт запуск (`Run`), решатель строит план от инвентаря после каждого отчёта мода,
`solver/executor.py` выбирает следующую задачу. Мод исполняет только типизированные задачи
из белого списка и сообщает итог; команды игры не используются.

Состояние запуска хранится в папке мира (`autopilot.json`). После перезапуска сервера
незавершённый запуск помечается остановленным и сам не продолжается.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from models.autopilot import RunState, Task, TaskReport

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"
STATE_FILE = "autopilot.json"

PENDING = "pending_confirm"
RUNNING = "running"
PAUSED = "paused"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"
ACTIVE = (PENDING, RUNNING, PAUSED)

# Повторы одной задачи до исключения источника (B.5): «до 2 повторов».
MAX_RETRIES = 2
# Пересчётов на запуск: страховка от бесконечного цикла, если план не сходится в игре.
MAX_REPLANS = 80
MAX_LOG = 40

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "require_confirmation": True,
    "singleplayer_only": True,
    "max_effort": 30,
    "max_run_minutes": 10,
    "timeouts_s": {"mine": 120, "craft": 15, "smelt": 90},
    "allowed_kinds": ["mine", "craft", "smelt", "place_station", "take_from_container"],
    "use_baritone": True,
    "min_health": 6,
}


def settings(path: Optional[Path] = None) -> Dict[str, Any]:
    """Раздел "autopilot" из settings.json; неверные значения заменяются умолчаниями."""
    try:
        raw = json.loads(Path(path or SETTINGS_PATH).read_text(encoding="utf-8")).get("autopilot") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        raw = {}
    result = json.loads(json.dumps(DEFAULTS))
    if not isinstance(raw, dict):
        return result
    for key in ("enabled", "require_confirmation", "singleplayer_only", "use_baritone"):
        if isinstance(raw.get(key), bool):
            result[key] = raw[key]
    for key, (low, high) in (("max_effort", (1, 200)), ("max_run_minutes", (1, 60)),
                             ("min_health", (1, 20))):
        try:
            result[key] = min(high, max(low, int(raw.get(key, result[key]))))
        except (TypeError, ValueError):
            pass
    if isinstance(raw.get("timeouts_s"), dict):
        for kind, value in raw["timeouts_s"].items():
            try:
                result["timeouts_s"][str(kind)] = min(600, max(5, int(value)))
            except (TypeError, ValueError):
                continue
    if isinstance(raw.get("allowed_kinds"), list):
        from solver.executor import TASK_KINDS
        result["allowed_kinds"] = [kind for kind in raw["allowed_kinds"] if kind in TASK_KINDS]
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


PLAYER = "player"
COMPANION = "companion"

# Как исполнитель говорит о себе в объявлениях. Автопилот — это сам ассистент в теле
# игрока; помощник — отдельный игрок, поэтому в его объявлениях есть имя исполнителя.
VOICE = {
    PLAYER: {"start": "Начинаю: {goal}. Остановить — клавиша K или любое движение.",
             "prefix": ""},
    COMPANION: {"start": "Помощник начинает: {goal}. Остановить — «помощник, стоп».",
                "prefix": "Помощник: "},
}

# Последняя задача помощника: отдать сделанное игроку (бросить рядом) или положить в сундук.
TASK_DELIVER = "deliver"


class Autopilot:
    """Один запуск на мир для одного исполнителя. Потокобезопасен: чат и исполнитель
    обращаются из разных запросов.

    Исполнителей два, и план у них один и тот же: автопилот мода (тело игрока) и помощник
    (отдельный игрок, `companion/`). Помощник отличается тем, что добывает всегда — у него
    свой поиск пути, — и тем, что в конце отдаёт сделанное игроку.
    """

    def __init__(self, clock=time.time, *, executor: str = PLAYER, state_file: str = STATE_FILE,
                 config=None) -> None:
        self.kind = executor
        self._state_file = state_file
        self._config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._run: Optional[RunState] = None
        self._loaded_from: Optional[str] = None
        self._announcements: List[Dict[str, Any]] = []
        self._executor = None

    # --- зависимости ---

    def _kb(self):
        from api.advice import _assistant_pipeline
        return _assistant_pipeline().kb

    def _settings(self) -> Dict[str, Any]:
        return (self._config or settings)()

    def executor(self):
        from solver.executor import ExecutionPlanner
        config = self._settings()
        if self._executor is None:
            self._executor = ExecutionPlanner(self._kb())
        self._executor.max_effort = config["max_effort"]
        self._executor.timeouts.update(config["timeouts_s"])
        self._executor.allowed_kinds = tuple(config["allowed_kinds"])
        return self._executor

    def use_executor(self, executor) -> None:
        self._executor = executor

    # --- хранение ---

    def _path(self) -> Optional[Path]:
        from api import worlds
        directory = worlds.memory_dir()
        return Path(directory) / self._state_file if directory else None

    def _ensure_loaded(self) -> None:
        path = self._path()
        key = str(path) if path else None
        if key == self._loaded_from:
            return
        self._loaded_from = key
        self._run = None
        if path is None or not path.exists():
            return
        try:
            run = RunState(**json.loads(path.read_text(encoding="utf-8")))
        except Exception as error:
            logger.warning(f"[Autopilot] Состояние запуска не прочитано: {error}")
            return
        if run.status in ACTIVE:
            # После перезапуска сервера запуск сам не продолжается (B.6).
            run.status = STOPPED
            run.message = "Сервер перезапускался — запуск остановлен. Продолжить — «продолжай»."
            run.current_task = None
        self._run = run

    def _save(self) -> None:
        path = self._path()
        if path is None or self._run is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(self._run.model_dump_json(indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as error:
            logger.warning(f"[Autopilot] Состояние запуска не сохранено: {error}")

    def reset(self) -> None:
        with self._lock:
            self._run = None
            self._loaded_from = None
            self._announcements = []

    # --- чтение ---

    def state(self) -> Optional[RunState]:
        with self._lock:
            self._ensure_loaded()
            return self._run.model_copy(deep=True) if self._run else None

    def brief(self) -> Optional[Dict[str, Any]]:
        """Коротко для ответа чата и оверлея: {run_id, status, step, total}."""
        run = self.state()
        if run is None:
            return None
        task = run.current_task or {}
        return {"run_id": run.run_id, "status": run.status, "executor": run.executor,
                "goal": run.goal, "label": run.label,
                "step": run.steps_done, "total": max(run.total_estimate, run.steps_done),
                "current": task.get("goal_step"), "waiting": run.waiting, "message": run.message}

    def consume_announcement(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._announcements.pop(0) if self._announcements else None

    def _announce(self, text: str, *, bare: bool = False) -> None:
        if not bare:
            text = VOICE[self.kind]["prefix"] + text
        self._announcements.append({"response": text, "autopilot": True, "executor": self.kind})
        if self._run is not None:
            self._run.message = text
            self._run.log = (self._run.log + [text])[-MAX_LOG:]

    # --- жизненный цикл ---

    def start(self, goal: str, count: int, *, label: Optional[str] = None, total: int = 0,
              baritone: bool = False, delivery: Optional[Dict[str, Any]] = None) -> RunState:
        with self._lock:
            self._ensure_loaded()
            self._run = RunState(run_id=uuid.uuid4().hex[:10], goal=goal, label=label,
                                 count=max(1, int(count)), status=PENDING, total_estimate=total,
                                 baritone=bool(baritone), started_at=_now(), executor=self.kind,
                                 delivery=delivery if self.kind == COMPANION else None)
            if not self._settings()["require_confirmation"]:
                self._begin()
            self._save()
            return self._run.model_copy(deep=True)

    def confirm(self, run_id: Optional[str] = None) -> Optional[RunState]:
        with self._lock:
            self._ensure_loaded()
            run = self._run
            if run is None or run.status != PENDING or (run_id and run.run_id != run_id):
                return None
            self._begin()
            self._save()
            return run.model_copy(deep=True)

    def _begin(self) -> None:
        run = self._run
        run.status = RUNNING
        run.deadline = self._clock() + self._settings()["max_run_minutes"] * 60
        self._announce(VOICE[self.kind]["start"].format(goal=run.label or run.goal), bare=True)

    def resume(self) -> Optional[RunState]:
        """«Продолжай» после паузы, остановки игроком или перезапуска сервера."""
        with self._lock:
            self._ensure_loaded()
            run = self._run
            if run is None or run.status not in (PAUSED, STOPPED):
                return None
            run.status = RUNNING
            run.current_task = None
            run.deadline = self._clock() + self._settings()["max_run_minutes"] * 60
            self._announce(f"Продолжаю: {run.label or run.goal}. Сделано шагов: {run.steps_done}.")
            self._save()
            return run.model_copy(deep=True)

    def stop(self, reason: Optional[str] = None, run_id: Optional[str] = None) -> Optional[RunState]:
        with self._lock:
            self._ensure_loaded()
            run = self._run
            if run is None or run.status not in ACTIVE or (run_id and run.run_id != run_id):
                return None
            cancelled = run.status == PENDING
            run.status = STOPPED
            run.current_task = None
            run.waiting = None
            if cancelled:
                self._announce("Хорошо, не начинаю.")
            else:
                self._announce(f"Остановился{(': ' + reason) if reason else ''}. "
                               f"Сделано шагов: {run.steps_done}. Продолжить — «продолжай».")
            self._save()
            return run.model_copy(deep=True)

    # --- задачи ---

    def next(self, inventory: Dict[str, int], *, stations: Optional[List[str]] = None,
             memory=None, position=None) -> Dict[str, Any]:
        """Следующая задача для мода или итог; инвентарь — последний снимок от мода."""
        with self._lock:
            self._ensure_loaded()
            return self._next(inventory, stations, memory, position)

    def _next(self, inventory, stations, memory, position) -> Dict[str, Any]:
        from solver.executor import Context, Waiting

        run = self._run
        if run is None:
            return {"status": "idle"}
        if run.status != RUNNING:
            return {"status": run.status, "message": run.message}
        if run.current_task is not None:
            # Повторный опрос, пока задача не отчиталась, — та же задача.
            return {"status": RUNNING, "task": run.current_task}
        if run.deadline and self._clock() > run.deadline:
            return self._finish(FAILED, f"Время вышло ({self._settings()['max_run_minutes']} мин). "
                                        f"Сделано шагов: {run.steps_done}.")
        if run.replans >= MAX_REPLANS:
            return self._finish(FAILED, "План не сходится в игре — остановился.")
        if run.delivered:
            return self._finish(DONE, self._done_text(run, delivered=True))
        if stations is not None:
            run.stations = list(stations)

        executor = self.executor()
        context = self._context(inventory, run, memory, position)
        run.replans += 1
        plan, task = executor.next_task(run.goal, run.count, context)
        if task is None:
            if inventory.get(run.goal, 0) >= run.count:
                if self._delivers(run):
                    return self._deliver(run)
                return self._finish(DONE, self._done_text(run))
            problems = executor.check(plan, effort_limit=False)
            if not problems and run.excluded_sources:
                problems = ["другого места добычи не знаю, а прежнее не далось"]
            return self._finish(FAILED, "Дальше не могу: " + ("; ".join(problems) or
                                                              "нет подходящего способа") + ".")
        problems = executor.check(plan, effort_limit=False)
        if problems:
            return self._finish(FAILED, "Дальше не могу: " + "; ".join(problems) + ".")
        if isinstance(task, Waiting):
            label = executor._label(task.item)
            note = f" (ближайшее известное: {task.where})" if task.where else ""
            if run.waiting is None or run.waiting.get("item") != task.item:
                self._announce(f"Добыть «{label}» ×{task.count} сам не могу — без Baritone я не "
                               f"хожу по миру. Принеси, пожалуйста{note}; дальше продолжу сам.")
            run.waiting = {"item": task.item, "count": task.count, "where": task.where}
            self._save()
            return {"status": "waiting", "message": run.message, "waiting": run.waiting}
        run.waiting = None
        run.current_task = Task(**task.to_dict()).model_dump()
        total = run.steps_done + len([step for step in plan.steps if step.kind != "take"]) + 1
        run.total_estimate = max(run.total_estimate, total) if run.steps_done else total
        self._announce(f"Шаг {run.steps_done + 1} из {run.total_estimate}: {task.goal_step}"
                       + (f" — {task.where}" if task.where else "") + ".")
        self._save()
        return {"status": RUNNING, "task": run.current_task}

    def _context(self, inventory, run: RunState, memory, position):
        from solver.executor import Context

        storage, where, known = {}, {}, {}
        container_of = block_at = None
        if memory is not None:
            from solver.locator import Locator
            locator = Locator(self._kb())
            storage, where = locator.storage(memory, position)
            known = locator.known_sources(memory, position)
            container_of = lambda item: locator.container_for(memory, item, position)  # noqa: E731
            block_at = lambda blocks: locator.nearest_block(memory, blocks, position)  # noqa: E731
        return Context(inventory=dict(inventory or {}), stations=tuple(run.stations),
                       storage=storage, where=where, known=known,
                       excluded=tuple(run.excluded_sources),
                       can_mine=(self.kind == COMPANION
                                 or (run.baritone and self._settings()["use_baritone"])),
                       container_of=container_of, block_at=block_at)

    def report(self, report: TaskReport, *, memory=None, position=None) -> Dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            run = self._run
            if run is None or run.status != RUNNING:
                return {"status": run.status if run else "idle"}
            task = run.current_task or {}
            if report.task_id and task and report.task_id != task.get("task_id"):
                return {"status": RUNNING, "task": task}
            run.current_task = None
            if report.stations:
                run.stations = list(report.stations)
            inventory = dict(report.inventory or {})
            key = f"{task.get('kind')}:{task.get('item')}"
            if task.get("kind") == TASK_DELIVER:
                if report.status == "done":
                    run.delivered = True
                    return self._finish(DONE, self._done_text(run, delivered=True))
                if report.status == "failed":
                    run.attempts[key] = run.attempts.get(key, 0) + 1
                    if run.attempts[key] > MAX_RETRIES:
                        run.delivered = True
                        return self._finish(DONE, self._done_text(run) + " Отдать не вышло ("
                                            + (report.reason or "сбой") + ") — всё у помощника.")
                    return self._deliver(run)
            if report.status == "done":
                run.steps_done += 1
                run.attempts.pop(key, None)
            elif report.status == "interrupted":
                run.status = PAUSED
                why = {"player_input": "ты взял управление", "danger": "опасно",
                       "timeout": "не успел"}.get(report.reason or "", report.reason or "прервано")
                self._announce(f"Остановился: {why}. Сделано шагов: {run.steps_done}. "
                               f"Продолжить — «продолжай».")
                self._save()
                return {"status": PAUSED, "message": run.message}
            else:
                run.attempts[key] = run.attempts.get(key, 0) + 1
                if run.attempts[key] > MAX_RETRIES:
                    if task.get("kind") == "mine" and task.get("blocks"):
                        # Источник не даётся — план ищет обход без него (excluded_sources).
                        run.excluded_sources = list(dict.fromkeys(
                            run.excluded_sources + list(task["blocks"])))
                        run.attempts.pop(key, None)
                        self._announce(f"Не вышло: {task.get('goal_step')} ({report.reason or 'сбой'}). "
                                       f"Ищу другой путь.")
                    else:
                        result = self._finish(FAILED, f"Не вышло: {task.get('goal_step')} "
                                                      f"({report.reason or 'сбой'}), повторы исчерпаны.")
                        return result
            return self._next(inventory, None, memory, position)

    # --- доставка (помощник) ---

    @staticmethod
    def _delivers(run: RunState) -> bool:
        mode = (run.delivery or {}).get("mode")
        return run.executor == COMPANION and mode in ("drop", "chest") and not run.delivered

    def _deliver(self, run: RunState) -> Dict[str, Any]:
        delivery = run.delivery or {}
        label = run.label or run.goal
        if delivery.get("mode") == "chest":
            step = f"положить «{label}» ×{run.count} в сундук"
        else:
            step = f"принести тебе «{label}» ×{run.count}"
        timeout = int(self._settings().get("timeouts_s", {}).get(TASK_DELIVER, 120))
        run.current_task = {
            "task_id": f"d{run.replans}", "kind": TASK_DELIVER, "item": run.goal, "count": run.count,
            "blocks": [], "recipe_id": None, "runs": 1, "station": delivery.get("mode"),
            "input": None, "fuel": None, "fuel_count": 0, "position": delivery.get("position"),
            "where": delivery.get("where") or delivery.get("player"), "timeout_s": timeout,
            "goal_step": step,
        }
        self._announce(f"Готово, {step}" + (f" — {delivery['where']}" if delivery.get("where") else "")
                       + ".")
        self._save()
        return {"status": RUNNING, "task": run.current_task}

    def _done_text(self, run: RunState, *, delivered: bool = False) -> str:
        text = f"Готово: {run.label or run.goal} ×{run.count}. Шагов: {run.steps_done}."
        if delivered:
            mode = (run.delivery or {}).get("mode")
            text += " Положил в сундук." if mode == "chest" else " Оставил рядом с тобой."
        return text

    def _finish(self, status: str, text: str) -> Dict[str, Any]:
        run = self._run
        run.status = status
        run.current_task = None
        run.waiting = None
        self._announce(text)
        self._save()
        return {"status": status, "message": text}


_shared = Autopilot()


def shared() -> Autopilot:
    return _shared


# --- HTTP ---

def _memory():
    from api.advice import _world_memory
    return _world_memory("Minecraft")


def _position():
    from api.advice import _player_position
    return _player_position()


@router.get("/state")
async def get_state() -> Dict[str, Any]:
    run = shared().state()
    return {"run": run.model_dump() if run else None, "brief": shared().brief(),
            "enabled": settings()["enabled"]}


@router.post("/start")
async def post_start(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Запуск в обход чата идёт той же проверкой, что и «сделай сам» в чате."""
    from api.advice import autopilot_start
    return await autopilot_start(payload if isinstance(payload, dict) else {})


@router.post("/confirm")
async def post_confirm(payload: Dict[str, Any]) -> Dict[str, Any]:
    run = shared().confirm((payload or {}).get("run_id"))
    if run is None:
        raise HTTPException(status_code=409, detail="Нет запуска, ждущего подтверждения.")
    return {"run": run.model_dump()}


@router.get("/next")
async def get_next(run_id: Optional[str] = None, stations: Optional[str] = None) -> Dict[str, Any]:
    """stations — станции рядом с игроком через запятую (crafting_table,furnace) от мода."""
    from api.advice import _inventory_counts
    run = shared().state()
    if run is not None and run_id and run.run_id != run_id:
        return {"status": "idle"}
    nearby = None
    if stations is not None:
        nearby = [name for name in stations.split(",") if name.strip()]
    return shared().next(_inventory_counts(), stations=nearby, memory=_memory(),
                         position=_position())


@router.post("/report")
async def post_report(report: TaskReport) -> Dict[str, Any]:
    if report.status not in ("done", "failed", "interrupted"):
        raise HTTPException(status_code=400, detail="status: done | failed | interrupted")
    if report.inventory is None:
        from api.advice import _inventory_counts
        report.inventory = _inventory_counts()
    return shared().report(report, memory=_memory(), position=_position())


@router.post("/stop")
async def post_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    run = shared().stop(payload.get("reason"), payload.get("run_id"))
    return {"run": run.model_dump() if run else None}
