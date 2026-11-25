"""События мода Minecraft → факты памяти.

Значения берутся из сообщений мода как есть: координаты, идентификаторы блоков и предметов,
количества. Идентификатор, которого нет в базе знаний, отбрасывается — в память не попадает
произвольная строка из запроса.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from memory.model import COMPANION, MOD, Position
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


# --- сканер и хранилища ---

# Что ищет сканер мода: руды, порталы, рассадники. Список отдаёт сервер, мод его не знает
# заранее, — так состав и нагрузку можно поменять без пересборки мода.
SCAN_BLOCKS = (
    "minecraft:coal_ore", "minecraft:deepslate_coal_ore",
    "minecraft:iron_ore", "minecraft:deepslate_iron_ore",
    "minecraft:copper_ore", "minecraft:deepslate_copper_ore",
    "minecraft:gold_ore", "minecraft:deepslate_gold_ore", "minecraft:nether_gold_ore",
    "minecraft:redstone_ore", "minecraft:deepslate_redstone_ore",
    "minecraft:lapis_ore", "minecraft:deepslate_lapis_ore",
    "minecraft:diamond_ore", "minecraft:deepslate_diamond_ore",
    "minecraft:emerald_ore", "minecraft:deepslate_emerald_ore",
    "minecraft:nether_quartz_ore", "minecraft:ancient_debris",
    "minecraft:spawner", "minecraft:nether_portal", "minecraft:end_portal_frame",
    "minecraft:budding_amethyst",
)

# Хранилища, содержимое которых запоминается при закрытии.
SHULKER_COLORS = ("white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
                  "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black")
CONTAINER_BLOCKS = frozenset((
    "minecraft:chest", "minecraft:trapped_chest", "minecraft:barrel", "minecraft:ender_chest",
    "minecraft:shulker_box", *(f"minecraft:{color}_shulker_box" for color in SHULKER_COLORS),
))

# Сколько записей принимается в одном пакете: сканер с радиусом 48 находит сотни руд,
# тысячи — уже ошибка или чужой клиент.
MAX_BATCH = 2000
MAX_ITEM_TYPES = 64


def _known(kb, thing: str) -> bool:
    return kb is None or kb.exists(thing)


def _label(kb, thing: str) -> str:
    return (kb.label(thing) if kb is not None else None) or thing


def scan_list(settings: Dict[str, Any], kb=None) -> Dict[str, Any]:
    """Что и как сканировать: блоки, радиус, частота."""
    blocks = [block for block in SCAN_BLOCKS if _known(kb, block)]
    return {"enabled": bool(settings.get("enabled")), "blocks": blocks,
            "radius": settings.get("scan_radius"), "interval_s": settings.get("scan_interval_s")}


def _block_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if ":" in text else (f"minecraft:{text}" if text else "")


def observations(store: MemoryStore, payload: Dict[str, Any], kb=None) -> Dict[str, Any]:
    """Пакет сканера: найденные блоки из списка и места, где их больше нет."""
    allowed = set(SCAN_BLOCKS)
    found, labels, skipped = [], {}, 0
    for entry in list(payload.get("blocks") or [])[:MAX_BATCH]:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        block = _block_id(entry.get("id"))
        position = Position.parse(entry)
        if block not in allowed or position is None or not _known(kb, block):
            skipped += 1
            continue
        found.append((block, position))
        labels.setdefault(block, _label(kb, block))
    removed = [position for position in (Position.parse(entry) for entry in
                                         list(payload.get("removed") or [])[:MAX_BATCH])
               if position is not None]
    result = store.observe_blocks(found, removed, labels=labels, source=_source(payload))
    result["skipped"] = skipped
    return result


def container(store: MemoryStore, payload: Dict[str, Any], kb=None):
    """Содержимое хранилища при закрытии. Неизвестные предметы отбрасываются."""
    block = _block_id(payload.get("block"))
    position = Position.parse(payload)
    if block not in CONTAINER_BLOCKS or position is None:
        return None
    items: Dict[str, int] = {}
    for item, count in list((payload.get("items") or {}).items())[:MAX_ITEM_TYPES]:
        item = _block_id(item)
        try:
            count = int(count)
        except (TypeError, ValueError):
            continue
        if count > 0 and _known(kb, item):
            items[item] = items.get(item, 0) + min(count, 27 * 64)
    return store.record_container(block, position, items, label=_label(kb, block),
                                  source=_source(payload))


def _source(payload: Dict[str, Any]) -> str:
    """Кто видел: мод игрока или помощник (отдельный игрок под управлением сервера)."""
    return COMPANION if payload.get("source") == COMPANION else MOD
