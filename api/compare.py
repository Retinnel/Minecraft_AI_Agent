"""Сравнение конфигураций на живой реплике — для демонстрации на защите.

Одна и та же реплика уходит в две конфигурации сразу:

    A — та же языковая модель с теми же инструментами, но без базы знаний;
    C — гибрид: модель определяет намерение, онтология — предмет, решатель — допустимость.

Используются буквально те же конфигурации, что и в оценочном контуре (eval/configs),
а не их повторная реализация: иначе показанное на защите расходилось бы с измеренным.

Отдельно считается то, ради чего всё и строилось, — недопустимые действия. Признаёт их
не эта страница, а валидатор: предложение конфигурации A проверяется теми же правилами
базы знаний, по которым работает гибрид. Поэтому счётчик показывает не мнение, а вывод.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/compare", tags=["compare"])


class CompareRequest(BaseModel):
    utterance: str


def _judge(prediction) -> Dict[str, Any]:
    """Был бы допустим результат конфигурации A по правилам базы знаний.

    Проверка идёт тем же валидатором, что и в гибриде: сравнивается не качество
    формулировок, а то, прошло ли бы действие контроль.
    """
    from kb.graph import KnowledgeBase
    from solver.validator import Validator

    if prediction.outcome != "act":
        return {"allowed": True, "reason": None}

    validator = Validator(KnowledgeBase.shared())
    target = prediction.item_id or prediction.entity_id
    verdict = validator.validate(prediction.intent or "give_item", target, prediction.count or 1)
    if verdict.allowed and verdict.count == (prediction.count or 1):
        return {"allowed": True, "reason": None}
    # Обрезание количества тоже считается расхождением: команда ушла бы с числом,
    # которое правилами не допускается.
    return {"allowed": False, "reason": verdict.reason or
            f"количество {prediction.count} превышает предел {verdict.count}"}


def _run_llm_only(utterance: str) -> Dict[str, Any]:
    from eval.configs import llm_only

    prediction = llm_only.predict(utterance)
    judgement = _judge(prediction)
    return {
        "outcome": prediction.outcome,
        "intent": prediction.intent,
        "target": prediction.item_id or prediction.entity_id,
        "count": prediction.count,
        "command": prediction.command,
        "error": prediction.details.get("error"),
        "verdict": judgement,
    }


def _run_hybrid(utterance: str) -> Dict[str, Any]:
    from eval.configs import hybrid

    prediction = hybrid.predict(utterance)
    details = prediction.details or {}
    stages = details.get("stages") or {}

    # Цель показывается и при отказе: «знаю и не дам» отличается от «не знаю такого»,
    # и на защите эта разница — половина смысла.
    recognised = (stages.get("resolver") or {}).get("thing")
    label = None
    if recognised:
        from kb.graph import KnowledgeBase
        label = KnowledgeBase.shared().label(recognised)

    return {
        "recognised": recognised,
        "recognised_label": label,
        "outcome": prediction.outcome,
        "intent": prediction.intent,
        "target": prediction.item_id or prediction.entity_id,
        "count": prediction.count,
        "command": prediction.command,
        "reason": details.get("reason"),
        "plan_tree": details.get("plan_tree"),
        "plan_summary": details.get("plan_summary"),
        "explanation": details.get("explanation") or [],
        "stages": stages,
        "error": details.get("error") if prediction.outcome == "error" else None,
        # Проверяется той же меркой, что и конфигурация A. Нулевая доля недопустимых
        # действий у гибрида — следствие устройства системы, но на защите она должна
        # быть видна как результат проверки, а не как утверждение докладчика.
        "verdict": _judge(prediction),
    }


@router.post("")
async def compare(request: CompareRequest) -> Dict[str, Any]:
    """Обе конфигурации на одной реплике. Вызовы идут параллельно: на защите важно время."""
    utterance = (request.utterance or "").strip()
    if not utterance:
        return {"utterance": "", "error": "пустая реплика"}

    left, right = await asyncio.gather(
        asyncio.to_thread(_run_llm_only, utterance),
        asyncio.to_thread(_run_hybrid, utterance),
    )
    return {
        "utterance": utterance,
        "llm_only": left,
        "hybrid": right,
        "illegal": {"llm_only": not left["verdict"]["allowed"],
                    "hybrid": not right["verdict"]["allowed"]},
    }


@router.get("/model")
async def model() -> Dict[str, Optional[str]]:
    """Какая модель участвует в обеих конфигурациях — её называют на защите."""
    from api.llm_config_manager import get_llm_config

    config = get_llm_config()
    active = config.models.get(config.model_type)
    return {"key": config.model_type,
            "name": getattr(active, "name", None),
            "provider": getattr(active, "provider", None)}
