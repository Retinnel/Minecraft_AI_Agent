"""Память мира: что игрок видел, что лежит в его хранилищах, где он погиб, что просил запомнить.

Хранилище выбирается по миру и игре разговора: для Minecraft — память текущего мира, для
другой игры — память её собственного мира (`api.worlds.memory_dir`). Пока мир не выбран,
память живёт только в процессе и на диск не пишется.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from memory.store import FILE_NAME, MemoryStore

DEFAULT_GAME = "Minecraft"

_stores: Dict[str, MemoryStore] = {}


def shared(game: Optional[str] = None) -> MemoryStore:
    """Хранилище для игры, о которой сейчас разговор."""
    from api import worlds
    from memory.config import settings

    game = game or DEFAULT_GAME
    directory = worlds.memory_dir(game)
    current = worlds.current() or {}
    world = current.get("id") or ""
    own = current.get("game")
    if game.lower() != DEFAULT_GAME.lower() and not (own and own.lower() == game.lower()):
        world = worlds.game_world_id(game)
    if directory is None:
        key = f"process:{game.lower()}"
        path = None
    else:
        path = Path(directory) / FILE_NAME
        key = str(path)
    store = _stores.get(key)
    if store is None:
        store = MemoryStore(path, game=game, world=world,
                            max_facts=settings()["max_facts"])
        _stores[key] = store
    return store


def reset() -> None:
    """Забывает поднятые хранилища: следующее обращение читает файл заново."""
    _stores.clear()
