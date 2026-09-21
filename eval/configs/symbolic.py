"""Конфигурация B — только символьная обработка, без LLM.

Это текущая система в режиме, когда языковая модель недоступна: распознавание намерения по
списку ключевых слов, резолв предмета или существа по каталогу через нечёткое сопоставление,
извлечение количества регулярным выражением.

Служит точкой отсчёта: с ней сравниваются LLM-вариант и гибрид.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.advice import (  # noqa: E402
    _extract_requested_count,
    _looks_like_action_request,
    _looks_like_remove_request,
    _looks_like_summon_request,
)
from api.entity_catalog import resolve_entity_id_from_text  # noqa: E402
from api.minecraft_catalog import resolve_item_id_from_text  # noqa: E402
from api.tool_service import execute_tool_call  # noqa: E402

from eval.schema import Config, Prediction  # noqa: E402


KEY = "symbolic"
TITLE = "B — только символьная схема"
DESCRIPTION = "Текущая система без LLM: ключевые слова, нечёткий резолв по каталогу, regex на количество."


def _intent_of(utterance: str) -> Optional[str]:
    if _looks_like_summon_request(utterance):
        return "summon_entity"
    if _looks_like_remove_request(utterance):
        return "remove_item"
    if _looks_like_action_request(utterance):
        return "give_item"
    return None


def _from_tool_result(intent: str, result: dict, count: int) -> Prediction:
    """Переводит ответ tool_service в Prediction."""
    if result.get("ok"):
        resolved_count = result.get("item_count")
        if resolved_count is None:
            resolved_count = result.get("entity_count", count)
        return Prediction(
            outcome="act",
            intent=intent,
            item_id=result.get("item_id"),
            entity_id=result.get("entity_id"),
            count=resolved_count,
            command=result.get("command"),
            details={"tool_result": result},
        )

    # tool_service различает неоднозначность и остальные ошибки только текстом сообщения,
    # поэтому опираемся на error_type и наличие кандидатов.
    error_type = result.get("error_type")
    outcome = "clarify" if error_type == "ambiguous" else "reject"
    return Prediction(
        outcome=outcome,
        intent=intent,
        details={"tool_result": result},
    )


def predict(utterance: str) -> Prediction:
    started = time.perf_counter()

    intent = _intent_of(utterance)
    if intent is None:
        return Prediction(outcome="none", latency_ms=(time.perf_counter() - started) * 1000)

    count = _extract_requested_count(utterance)

    if intent == "summon_entity":
        resolution = resolve_entity_id_from_text(utterance)
    else:
        resolution = resolve_item_id_from_text(utterance)

    status = resolution.get("status")
    if status == "ambiguous":
        prediction = Prediction(
            outcome="clarify",
            intent=intent,
            details={"resolution": resolution},
        )
        prediction.latency_ms = (time.perf_counter() - started) * 1000
        return prediction
    if status != "resolved":
        prediction = Prediction(
            outcome="reject",
            intent=intent,
            details={"resolution": resolution},
        )
        prediction.latency_ms = (time.perf_counter() - started) * 1000
        return prediction

    # Прогоняем через реальный исполнитель инструментов, чтобы поймать в том числе
    # серверную валидацию идентификатора и обрезание количества.
    if intent == "summon_entity":
        arguments = {"entity_query": utterance, "count": count}
    else:
        arguments = {"item_query": utterance, "count": count}

    result = execute_tool_call(intent, arguments)
    prediction = _from_tool_result(intent, result, count)
    prediction.latency_ms = (time.perf_counter() - started) * 1000
    return prediction


CONFIG = Config(key=KEY, title=TITLE, description=DESCRIPTION, predict=predict)
