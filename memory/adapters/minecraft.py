"""События мода Minecraft → факты памяти.

Значения берутся из сообщений мода как есть: координаты, идентификаторы блоков и предметов,
количества. Идентификатор, которого нет в базе знаний, отбрасывается — в память не попадает
произвольная строка из запроса.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from memory.model import Position
from memory.store import MemoryStore


def _counts(inventory: Any) -> Dict[str, int]:
    counts = (inventory or {}).get("counts") if isinstance(inventory, dict) else None
    result: Dict[str, int] = {}
    for item, count in (counts or {}).items():
        try:
            count = int(count)
        except (TypeError, ValueError):
            continue
        if count > 0:
            result[str(item)] = count
    return result


def record_death(store: MemoryStore, death: Dict[str, Any], *,
                 cause_label: Optional[Callable[[str], Optional[str]]] = None):
    """Гибель из состояния игрока: место, причина, инвентарь на тот момент."""
    if not isinstance(death, dict):
        return None
    cause = str(death.get("cause") or "unknown")
    label = (cause_label(cause) if cause_label else None) or cause
    return store.record_death(Position.parse(death.get("position")), cause,
                              _counts(death.get("inventory")), label=label,
                              now=death.get("timestamp"))
