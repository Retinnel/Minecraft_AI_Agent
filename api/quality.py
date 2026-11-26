"""Сводка качества для страницы `/quality`: числа из отчётов оценочного контура.

Страница ничего не меряет сама. Она читает последние отчёты `eval/reports/*.json`, которые
пишет `eval.runner`, и показывает их рядом: три конфигурации на общем наборе, ту же пару на
малой локальной модели, отдельные наборы и скорость планировщика. Так показанное на защите
совпадает с измеренным — источник у обоих один, и у каждого числа назван файл отчёта.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

router = APIRouter(prefix="/api/quality", tags=["quality"])

REPORTS_DIR = Path(__file__).resolve().parent.parent / "eval" / "reports"

GROUPS = (("typical", "типичные"), ("boundary", "граничные"), ("negative", "отрицательные"))


def _latest(pattern: str) -> Optional[Path]:
    """Последний по дате отчёт с таким окончанием имени (дата стоит в начале имени)."""
    found = sorted(REPORTS_DIR.glob(pattern))
    return found[-1] if found else None


def _read(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _run(pattern: str, title: str, note: str) -> Optional[Dict[str, Any]]:
    """Одна конфигурация на одном наборе: сколько совпало, сколько недопустимых, время."""
    path = _latest(pattern)
    data = _read(path)
    if not data or not isinstance(data.get("summary"), dict):
        return None
    summary = data["summary"]
    total = int(summary.get("total") or 0)
    passed = summary.get("passed") or 0
    unsafe = summary.get("unsafe") or 0
    model = data.get("model") or {}
    groups = []
    for key, name in GROUPS:
        group = (summary.get("by_group") or {}).get(key) or {}
        if group.get("total"):
            groups.append({"key": key, "title": name, "passed": group.get("pass", 0),
                           "total": group["total"]})
    latency = summary.get("latency_ms") or {}
    return {
        "title": title, "note": note, "report": path.name, "date": path.name[:10],
        "model": model.get("модель"), "repeat": summary.get("repeat") or 1,
        "passed": passed, "total": total, "unsafe": unsafe,
        "passed_range": summary.get("passed_range"), "unsafe_range": summary.get("unsafe_range"),
        "accuracy": round(passed / total, 4) if total else None,
        "unsafe_rate": round(unsafe / total, 4) if total else None,
        "groups": groups,
        "latency_median_ms": round(latency["median"]) if latency.get("median") is not None else None,
    }


def _present(items: List[Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return [item for item in items if item]


def summary() -> Dict[str, Any]:
    planner = _read(_latest("*-planner-coverage.json")) or {}
    return {
        # Сравнение трёх конфигураций на одной модели: берутся отчёты одного дня замера.
        "comparison": _present([
            _run("2026-11-11-llm_only.json", "Только языковая модель",
                 "модель сама называет идентификаторы, базы знаний и проверок нет"),
            _run("2026-11-11-symbolic.json", "Только символьная схема",
                 "ключевые слова и нечёткое сопоставление со справочником"),
            _run("2026-11-11-hybrid.json", "Гибрид",
                 "модель — намерение, онтология — объект, решатель — допустимость"),
        ]),
        "latest": _run("*-hybrid.json", "Гибрид сейчас", "последний прогон общего набора"),
        "weak_model": _present([
            _run("*-llm_only-qwen4b.json", "Только языковая модель",
                 "локальная модель с четырьмя миллиардами параметров"),
            _run("*-hybrid-qwen4b.json", "Гибрид", "та же локальная модель"),
        ]),
        "extra_sets": _present([
            _run("*-hybrid-memory.json", "Набор памяти мира", "гибрид, отдельный набор реплик"),
            _run("*-hybrid-autopilot.json", "Набор «Сделай сам»", "гибрид, отдельный набор реплик"),
        ]),
        "planner": {
            "targets": planner.get("targets"), "feasible": planner.get("feasible"),
            "ms_median": planner.get("ms_median"), "ms_p95": planner.get("ms_p95"),
            "ms_max": planner.get("ms_max"),
            "report": (_latest("*-planner-coverage.json") or Path("")).name or None,
        } if planner else None,
    }


@router.get("/")
async def get_quality() -> Dict[str, Any]:
    return summary()
