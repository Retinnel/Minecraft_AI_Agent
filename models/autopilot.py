"""Данные режима «Сделай сам»: задача мода, отчёт о ней и состояние запуска."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# Чем закончилась задача и почему.
TASK_STATUSES = ("done", "failed", "interrupted")
TASK_REASONS = ("timeout", "not_found", "danger", "player_input", "no_tool", "blocked",
                "unsupported", "no_baritone")


class Task(BaseModel):
    """Задача мода. Виды — только из белого списка (solver/executor.TASK_KINDS)."""

    task_id: str
    kind: str
    item: str
    count: int = 1
    blocks: List[str] = Field(default_factory=list)
    recipe_id: Optional[str] = None
    runs: int = 1
    station: Optional[str] = None
    input: Optional[str] = None
    fuel: Optional[str] = None
    fuel_count: int = 0
    position: Optional[Dict[str, Any]] = None
    where: Optional[str] = None
    timeout_s: int = 60
    goal_step: str = ""


class TaskReport(BaseModel):
    """Отчёт мода о задаче: инвентарь и станции рядом — по ним строится следующий план."""

    run_id: Optional[str] = None
    task_id: Optional[str] = None
    status: str
    reason: Optional[str] = None
    inventory: Optional[Dict[str, int]] = None
    stations: List[str] = Field(default_factory=list)
    position: Optional[Dict[str, Any]] = None
    elapsed_ms: Optional[float] = None


class RunState(BaseModel):
    """Запуск «Сделай сам» — для оверлея, /chat и сохранения в папке мира."""

    run_id: str
    goal: str
    label: Optional[str] = None
    count: int = 1
    status: str = "pending_confirm"   # pending_confirm | running | paused | done | failed | stopped
    started_at: Optional[str] = None
    deadline: Optional[float] = None  # время по часам сервера (time.time)
    steps_done: int = 0
    total_estimate: int = 0
    replans: int = 0
    excluded_sources: List[str] = Field(default_factory=list)
    attempts: Dict[str, int] = Field(default_factory=dict)
    current_task: Optional[Dict[str, Any]] = None
    waiting: Optional[Dict[str, Any]] = None
    stations: List[str] = Field(default_factory=list)
    baritone: bool = False
    message: Optional[str] = None
    log: List[str] = Field(default_factory=list)
