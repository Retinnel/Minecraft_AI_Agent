"""Конфигурация C — гибрид: языковая модель, онтология и решатель вместе.

Отличие от конфигурации A ровно одно, но оно определяющее: модель не называет
идентификаторов. Она отвечает на вопрос «что игрок хочет и о чём говорит», перенося
название словами самого игрока. Идентификатор подставляет резолвер по онтологии,
допустимость действия проверяет валидатор, команду собирает сервер.

Отсюда следует свойство, ради которого всё и строилось: недопустимое действие невозможно
получить переформулировкой запроса, потому что в решении о допустимости модель не участвует.

Разбор реплики и её исполнение разведены: здесь модель подключается к общему конвейеру
(orchestrator/pipeline.py) тем же извлекателем намерения (orchestrator/extractor.py), что
работает на сервере. Прогон измеряет ту систему, которая работает в игре, а не её копию.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv  # noqa: E402

# Оценочный контур запускается отдельно от сервера, который обычно и подхватывает .env.
load_dotenv()

from orchestrator import Pipeline  # noqa: E402
from orchestrator.extractor import LanguageModelIntent  # noqa: E402
from orchestrator.llm import ModelUnavailable  # noqa: E402
from solver.validator import ACT  # noqa: E402
from eval.schema import Config, Prediction  # noqa: E402

KEY = "hybrid"
TITLE = "C — гибрид"
DESCRIPTION = "Модель определяет намерение, онтология — предмет, решатель — допустимость."

_pipeline: Optional[Pipeline] = None


def _get_pipeline() -> Pipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline(LanguageModelIntent())
    return _pipeline


def predict(utterance: str) -> Prediction:
    started = time.perf_counter()
    try:
        turn = _get_pipeline().handle(utterance)
    except ModelUnavailable as error:
        # Сбой канала связи — не решение системы, поэтому и не отказ.
        return Prediction(outcome="error", details={"error": str(error)},
                          latency_ms=(time.perf_counter() - started) * 1000)

    latency = (time.perf_counter() - started) * 1000
    is_creature = turn.action == "summon_entity"
    return Prediction(
        outcome=turn.outcome,
        intent=turn.action,
        item_id=turn.target if turn.outcome == ACT and not is_creature else None,
        entity_id=turn.target if turn.outcome == ACT and is_creature else None,
        count=turn.count if turn.outcome == ACT else None,
        command=turn.command,
        latency_ms=latency,
        details={"stages": turn.stages, "reason": turn.reason, "explanation": turn.explanation,
                 "plan_tree": turn.plan_tree, "plan_summary": turn.plan_summary,
                 "plan_alternatives": turn.plan_alternatives},
    )


CONFIG = Config(key=KEY, title=TITLE, description=DESCRIPTION, predict=predict)
