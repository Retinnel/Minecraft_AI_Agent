"""Условия прогона набора golden_autopilot.jsonl: выживание, одиночная игра, мод на связи.

«Сделай сам» предъявляется модели только при включённом автопилоте, поэтому основной набор
его не видит и не меняется. Здесь режим включён, Baritone установлен, инвентарь пуст.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from eval.schema import Config

AUTOPILOT = {"enabled": True, "singleplayer": True, "available": True, "baritone": True,
             "max_effort": 30}


def with_autopilot(config: Config) -> Config:
    if config.key != "hybrid":
        raise SystemExit("Набор «Сделай сам» прогоняется только гибридом.")
    predict = config.predict

    def predict_with_autopilot(utterance: str, **kwargs: Any):
        kwargs.setdefault("mode", "survival")
        return predict(utterance, autopilot=dict(AUTOPILOT), **kwargs)

    return replace(config, predict=predict_with_autopilot)
