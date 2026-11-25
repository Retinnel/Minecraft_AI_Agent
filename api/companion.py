"""Помощник: отдельный игрок в мире игрока, который исполняет тот же план, что автопилот.

Помощник — процесс Node (`companion/`), заходит в мир, открытый для сети, вторым игроком.
Решения здесь, на сервере: план строит решатель, допуск даёт валидатор, задачи по одной
выдаёт `solver/executor.py` через тот же класс запуска `api.autopilot.Autopilot`, что и
для автопилота мода. Процесс помощника — только руки: исполняет задачу из белого списка
и отчитывается своим инвентарём. Увиденное им (руды, сундуки) пишется в память мира
с источником «companion».

    GET  /api/companion/state          процесс, связь, снимок помощника, запуск
    POST /api/companion/launch         запустить процесс помощника (host, port, username)
    POST /api/companion/shutdown       остановить процесс
    POST /api/companion/hello          пульс помощника: позиция, здоровье, инвентарь
    POST /api/companion/next           следующая задача (тело — инвентарь и станции рядом)
    POST /api/companion/report         отчёт о задаче
    POST /api/companion/observations   увиденные блоки → память мира
    POST /api/companion/containers     открытый сундук → память мира
    POST /api/companion/start|confirm|stop   управление запуском без чата
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from api.autopilot import COMPANION, DEFAULTS as AUTOPILOT_DEFAULTS, Autopilot
from models.autopilot import TaskReport

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/companion", tags=["companion"])

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "settings.json"
COMPANION_DIR = ROOT / "companion"
LOG_PATH = ROOT / "data" / "companion.log"
STATE_FILE = "companion.json"

# Пульс приходит раз в 2 секунды; дольше 10 секунд тишины — помощник не в игре.
HEARTBEAT_TIMEOUT_S = 10.0
DELIVERY_MODES = ("drop", "chest", "none")

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "username": "Helper",
    "host": "127.0.0.1",
    "port": 25565,
    "version": "1.20.1",
    "delivery": "drop",          # drop — бросить рядом с игроком, chest — в сундук, none — оставить у себя
    "delivery_chest": None,      # метка места из памяти («сундук помощника») или "x y z"
    "require_confirmation": True,
    "max_effort": 30,
    "max_run_minutes": 15,
    # Помощник сам ходит к верстаку и печи, поэтому крафту и плавке нужен запас на дорогу.
    "timeouts_s": {"mine": 180, "craft": 60, "smelt": 150, "place_station": 30,
                   "take_from_container": 90, "deliver": 120},
    "allowed_kinds": list(AUTOPILOT_DEFAULTS["allowed_kinds"]),
    "use_baritone": False,
    "min_health": 6,
    "scan_radius": 24,
}


def settings(path: Optional[Path] = None) -> Dict[str, Any]:
    """Раздел "companion" из settings.json; неверные значения заменяются умолчаниями."""
    try:
        raw = json.loads(Path(path or SETTINGS_PATH).read_text(encoding="utf-8")).get("companion") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        raw = {}
    result = json.loads(json.dumps(DEFAULTS))
    if not isinstance(raw, dict):
        return result
    for key in ("enabled", "require_confirmation"):
        if isinstance(raw.get(key), bool):
            result[key] = raw[key]
    for key in ("username", "host", "version"):
        if isinstance(raw.get(key), str) and raw[key].strip():
            result[key] = raw[key].strip()[:40]
    for key, (low, high) in (("port", (1, 65535)), ("max_effort", (1, 200)),
                             ("max_run_minutes", (1, 120)), ("min_health", (1, 20)),
                             ("scan_radius", (4, 64))):
        try:
            result[key] = min(high, max(low, int(raw.get(key, result[key]))))
        except (TypeError, ValueError):
            pass
    if raw.get("delivery") in DELIVERY_MODES:
        result["delivery"] = raw["delivery"]
    if isinstance(raw.get("delivery_chest"), str) and raw["delivery_chest"].strip():
        result["delivery_chest"] = raw["delivery_chest"].strip()[:80]
    if isinstance(raw.get("timeouts_s"), dict):
        for kind, value in raw["timeouts_s"].items():
            try:
                result["timeouts_s"][str(kind)] = min(900, max(5, int(value)))
            except (TypeError, ValueError):
                continue
    if isinstance(raw.get("allowed_kinds"), list):
        from solver.executor import TASK_KINDS
        result["allowed_kinds"] = [kind for kind in raw["allowed_kinds"] if kind in TASK_KINDS]
    return result


_service = Autopilot(executor=COMPANION, state_file=STATE_FILE, config=lambda: settings())


def shared() -> Autopilot:
    return _service


# --- связь с помощником ---

class Link:
    """Последний пульс помощника: по нему видно, в игре ли он и что у него в инвентаре."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.snapshot: Dict[str, Any] = {}
        self.seen_at: Optional[float] = None

    def beat(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            self.snapshot = dict(payload)
            self.seen_at = self._clock()

    def connected(self) -> bool:
        with self._lock:
            return self.seen_at is not None and self._clock() - self.seen_at < HEARTBEAT_TIMEOUT_S

    def position(self):
        from memory.model import Position
        return Position.parse(self.snapshot.get("position") or {})

    def reset(self) -> None:
        with self._lock:
            self.snapshot, self.seen_at = {}, None


_link = Link()


def link() -> Link:
    return _link


# --- процесс помощника ---

class Process:
    """Запуск и остановка процесса Node. Помощника можно запускать и вручную (`npm start`)."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._log = None

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def pid(self) -> Optional[int]:
        return self._proc.pid if self.running() else None

    def command(self) -> List[str]:
        node = shutil.which("node") or "node"
        return [node, str(COMPANION_DIR / "src" / "index.js")]

    def environment(self, config: Dict[str, Any], server_url: str) -> Dict[str, str]:
        env = dict(os.environ)
        env.update({"ASSISTANT_URL": server_url, "MC_HOST": str(config["host"]),
                    "MC_PORT": str(config["port"]), "MC_USERNAME": str(config["username"]),
                    "MC_VERSION": str(config["version"]), "MIN_HEALTH": str(config["min_health"]),
                    "SCAN_RADIUS": str(config["scan_radius"])})
        return env

    def launch(self, config: Dict[str, Any], server_url: str) -> Dict[str, Any]:
        if self.running():
            return {"ok": True, "pid": self._proc.pid, "already": True}
        if not (COMPANION_DIR / "node_modules").exists():
            return {"ok": False, "reason": "Нет зависимостей помощника: выполни «npm install» в "
                                           "папке companion."}
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(LOG_PATH, "a", encoding="utf-8")
        try:
            self._proc = subprocess.Popen(self.command(), cwd=str(COMPANION_DIR),
                                          env=self.environment(config, server_url),
                                          stdout=self._log, stderr=subprocess.STDOUT)
        except OSError as error:
            return {"ok": False, "reason": f"Не удалось запустить node: {error}"}
        return {"ok": True, "pid": self._proc.pid}

    def shutdown(self) -> bool:
        if not self.running():
            return False
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        if self._log:
            self._log.close()
        _link.reset()
        return True


_process = Process()


def process() -> Process:
    return _process


# --- для чата и проверки запуска ---

def context() -> Dict[str, Any]:
    """Сведения о помощнике для конвейера: включён ли, в игре ли, как отдаёт."""
    config = settings()
    return {"enabled": config["enabled"], "connected": _link.connected(),
            "name": _link.snapshot.get("username") or config["username"],
            "delivery": config["delivery"], "max_effort": config["max_effort"],
            "inventory": dict(_link.snapshot.get("inventory") or {}),
            "position": _link.snapshot.get("position")}


def delivery(memory_store=None) -> Dict[str, Any]:
    """Как отдать сделанное. Сундук берётся из памяти мира по метке или по координатам."""
    config = settings()
    mode = config["delivery"]
    if mode != "chest":
        return {"mode": mode}
    target = config.get("delivery_chest")
    position = _parse_coordinates(target)
    where = target
    if position is None and memory_store is not None and target:
        from memory.model import PLACE, CONTAINER
        wanted = target.lower()
        for fact in memory_store.facts(PLACE) + memory_store.facts(CONTAINER):
            if fact.position is not None and wanted in str(fact.label or "").lower():
                position = fact.position.to_dict()
                where = fact.label
                break
    if position is None:
        # Сундук не найден — отдаём в руки, чтобы сделанное не осталось неизвестно где.
        return {"mode": "drop", "where": None, "note": f"сундук «{target}» не найден в памяти"}
    return {"mode": "chest", "position": position, "where": where}


def _parse_coordinates(text: Optional[str]) -> Optional[Dict[str, Any]]:
    parts = str(text or "").replace(",", " ").split()
    if len(parts) != 3:
        return None
    try:
        x, y, z = (int(float(part)) for part in parts)
    except ValueError:
        return None
    return {"x": x, "y": y, "z": z, "dimension": "minecraft:overworld"}


def _memory():
    from api.advice import _world_memory
    return _world_memory("Minecraft")


def _kb():
    from api.memory import _kb as kb
    return kb()


# --- HTTP ---

@router.get("/state")
async def get_state() -> Dict[str, Any]:
    run = shared().state()
    config = settings()
    return {"enabled": config["enabled"], "username": config["username"],
            "host": config["host"], "port": config["port"], "delivery": config["delivery"],
            "process": {"running": _process.running(), "pid": _process.pid()},
            "connected": _link.connected(), "bot": _link.snapshot,
            "run": run.model_dump() if run else None, "brief": shared().brief()}


@router.post("/launch")
async def post_launch(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = settings()
    payload = payload if isinstance(payload, dict) else {}
    for key in ("host", "username"):
        if isinstance(payload.get(key), str) and payload[key].strip():
            config[key] = payload[key].strip()[:40]
    if payload.get("port") is not None:
        try:
            config["port"] = min(65535, max(1, int(payload["port"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="port — число")
    server_url = str(payload.get("server_url") or "http://127.0.0.1:8000")
    return _process.launch(config, server_url)


@router.post("/shutdown")
async def post_shutdown() -> Dict[str, Any]:
    stopped = shared().stop("помощник выключен")
    return {"ok": _process.shutdown(), "run": stopped.model_dump() if stopped else None}


@router.post("/hello")
async def post_hello(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    _link.beat(payload)
    run = shared().state()
    from memory.adapters.minecraft import SCAN_BLOCKS
    return {"ok": True, "run_id": run.run_id if run else None,
            "run_status": run.status if run else None, "scan": list(SCAN_BLOCKS)}


def _body_position(payload: Dict[str, Any]):
    from memory.model import Position
    return Position.parse(payload.get("position") or {}) or _link.position()


@router.post("/next")
async def post_next(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    run = shared().state()
    if run is not None and payload.get("run_id") and run.run_id != payload["run_id"]:
        return {"status": "idle"}
    inventory = {str(key): int(value) for key, value in (payload.get("inventory") or {}).items()
                 if isinstance(value, (int, float))}
    stations = [str(name) for name in payload.get("stations") or []]
    return shared().next(inventory, stations=stations, memory=_memory(),
                         position=_body_position(payload))


@router.post("/report")
async def post_report(report: TaskReport) -> Dict[str, Any]:
    if report.status not in ("done", "failed", "interrupted"):
        raise HTTPException(status_code=400, detail="status: done | failed | interrupted")
    from memory.model import Position
    position = Position.parse(report.position or {}) or _link.position()
    return shared().report(report, memory=_memory(), position=position)


@router.post("/observations")
async def post_observations(payload: Dict[str, Any]) -> Dict[str, Any]:
    from memory.adapters.minecraft import observations
    store = _memory()
    if store is None:
        return {"ok": False, "disabled": True}
    payload = dict(payload if isinstance(payload, dict) else {}, source=COMPANION)
    result = observations(store, payload, _kb())
    if result["added"] or result["updated"] or result["stale"]:
        store.save()
    return {"ok": True, **result}


@router.post("/containers")
async def post_container(payload: Dict[str, Any]) -> Dict[str, Any]:
    from memory.adapters.minecraft import container
    store = _memory()
    if store is None:
        return {"ok": False, "disabled": True}
    payload = dict(payload if isinstance(payload, dict) else {}, source=COMPANION)
    fact = container(store, payload, _kb())
    if fact is None:
        raise HTTPException(status_code=400, detail="Не хранилище или нет координат.")
    store.save()
    return {"ok": True, "fact_id": fact.id}


@router.post("/start")
async def post_start(payload: Dict[str, Any]) -> Dict[str, Any]:
    from api.advice import autopilot_start
    payload = dict(payload if isinstance(payload, dict) else {}, executor=COMPANION)
    return await autopilot_start(payload)


@router.post("/confirm")
async def post_confirm(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    run = shared().confirm((payload or {}).get("run_id"))
    if run is None:
        raise HTTPException(status_code=409, detail="Нет запуска, ждущего подтверждения.")
    return {"run": run.model_dump()}


@router.post("/stop")
async def post_stop(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    run = shared().stop(payload.get("reason"), payload.get("run_id"))
    return {"run": run.model_dump() if run else None}


def shutdown_on_exit() -> None:
    """При остановке сервера процесс помощника не должен остаться висеть."""
    try:
        _process.shutdown()
    except Exception as error:  # pragma: no cover — завершение процесса
        logger.warning(f"[Companion] Процесс не остановлен: {error}")
