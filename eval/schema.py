"""Общие структуры данных для оценочного контура.

Конфигурация (символьная, LLM, гибридная) реализует функцию predict(utterance) -> Prediction.
Раннер сравнивает Prediction с разметкой из golden_set.jsonl и считает метрики.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

# Чем закончилась обработка реплики.
#   act     — система выполнила действие
#   clarify — система запросила уточнение (запрос неоднозначен)
#   reject  — система отказала (предмет не найден, действие недопустимо)
#   none    — реплика не является запросом действия (обычный разговор)
OUTCOMES = ("act", "clarify", "reject", "none")

GROUPS = ("typical", "boundary", "negative")

INTENTS = ("give_item", "remove_item", "summon_entity", "plan_goal", None)


@dataclass
class Case:
    """Один размеченный пример из золотого набора."""

    id: str
    utterance: str
    group: str
    outcome: str
    intent: Optional[str] = None
    item_id: Optional[str] = None
    entity_id: Optional[str] = None
    count: Optional[int] = None
    note: str = ""
    # Разметка спорная и требует подтверждения: поведение зависит от принятой политики,
    # а не от объективно правильного ответа.
    policy: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"{self.id}: неизвестный outcome {self.outcome!r}")
        if self.group not in GROUPS:
            raise ValueError(f"{self.id}: неизвестная group {self.group!r}")
        if self.intent not in INTENTS:
            raise ValueError(f"{self.id}: неизвестный intent {self.intent!r}")
        if self.outcome == "act" and self.intent is None:
            raise ValueError(f"{self.id}: outcome=act требует intent")


@dataclass
class Prediction:
    """Что выдала проверяемая конфигурация на одну реплику."""

    outcome: str
    intent: Optional[str] = None
    item_id: Optional[str] = None
    entity_id: Optional[str] = None
    count: Optional[int] = None
    command: Optional[str] = None
    latency_ms: float = 0.0
    # Произвольные подробности конфигурации: кандидаты резолвера, ответ модели и т.п.
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    """Проверяемая конфигурация системы."""

    key: str
    title: str
    description: str
    predict: Callable[[str], Prediction]


def load_golden_set(path: Path) -> List[Case]:
    cases: List[Case] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name}:{line_no}: некорректный JSON — {exc}") from exc
        case = Case(**payload)
        if case.id in seen:
            raise ValueError(f"{path.name}:{line_no}: дублирующийся id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    return cases


def iter_case_dicts(cases: List[Case]) -> Iterator[Dict[str, Any]]:
    for case in cases:
        yield asdict(case)
