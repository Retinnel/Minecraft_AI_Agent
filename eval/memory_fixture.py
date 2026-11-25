"""Память мира для прогона набора golden_memory.jsonl и замера длины планов.

Мир собран вручную и не меняется от прогона к прогону: база, шахта, сундук с железом,
увиденная алмазная руда, одна гибель в лаве. Каждая реплика получает свою свежую копию,
чтобы «запомни» и «забудь» в одном примере не влияли на следующий.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from eval.schema import Config
from memory.model import OVERWORLD, Position
from memory.store import MemoryStore

# Где стоит игрок во время прогона.
HOME = Position(OVERWORLD, 0, 64, 0)

# Что лежит в сундуке у базы: для замера плана берутся типичные запасы середины игры.
CHEST_ITEMS = {"minecraft:iron_ingot": 7, "minecraft:cobblestone": 64, "minecraft:oak_planks": 32,
               "minecraft:stick": 16, "minecraft:coal": 12, "minecraft:string": 6}


def fixture_store() -> MemoryStore:
    store = MemoryStore(None)
    store.note("база", position=Position(OVERWORLD, 100, 64, -50))
    store.note("шахта", position=Position(OVERWORLD, 0, 40, 30))
    store.record_container("minecraft:chest", Position(OVERWORLD, 12, 64, -40), CHEST_ITEMS,
                           label="Сундук")
    store.observe_blocks([("minecraft:deepslate_diamond_ore", Position(OVERWORLD, -120, -58, 340)),
                          ("minecraft:iron_ore", Position(OVERWORLD, 20, 30, 10))],
                         labels={"minecraft:deepslate_diamond_ore": "Глубинная алмазная руда",
                                 "minecraft:iron_ore": "Железная руда"})
    store.record_death(Position(OVERWORLD, 40, 11, -300), "lava", label="огонь или лава")
    return store


def with_memory(config: Config) -> Config:
    """Та же конфигурация, но каждая реплика видит свежую копию памяти и позицию игрока."""
    if config.key != "hybrid":
        raise SystemExit("Набор памяти мира прогоняется только гибридом: "
                         "остальные конфигурации память не читают.")
    predict = config.predict

    def predict_with_memory(utterance: str, **kwargs: Any):
        return predict(utterance, memory=fixture_store(), position=HOME, **kwargs)

    return replace(config, predict=predict_with_memory)
