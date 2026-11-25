"""Ближайший известный источник предмета — по памяти мира и онтологии.

Онтология знает, из каких блоков предмет добывается и какой инструмент для этого нужен;
память знает, где игрок такие блоки видел. Локатор сводит одно с другим: «где алмазы» →
алмазная руда в 190 блоках, нужна железная кирка — она есть или её нет.

Координаты — из памяти (их прислал мод), уровень инструмента — из онтологии
(`tier_satisfies`). Модель здесь не участвует.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from kb.graph import KnowledgeBase, Source
from memory.model import RESOURCE, Fact, Position
from memory.store import MemoryStore


@dataclass
class Located:
    fact: Fact
    source: Optional[Source]          # источник добычи; None — сам предмет (хранилище)
    distance: Optional[int]
    tool: Optional[str] = None        # нужный инструмент, самый простой из подходящих
    tool_ok: Optional[bool] = None    # есть ли подходящий в инвентаре; None — не нужен


class Locator:
    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb

    def sources(self, item: str) -> Dict[str, Source]:
        """Блоки, из которых добывается предмет; сам блок — источник себя."""
        found = {source.id: source for source in self.kb.sources_of(item)
                 if source.kind != "creature"}
        return found

    def tool_check(self, source: Source, inventory: Optional[Dict[str, int]]):
        """Нужный инструмент и есть ли он: (None, None), если инструмент не обязателен."""
        if not source.tool_required or not source.tool_kind:
            return None, None
        suitable = [tool for tool in self.kb.tools_of_kind(source.tool_kind)
                    if self.kb.tier_satisfies(tool.tier, source.tool_tier)]
        if not suitable:
            return None, False
        owned = any((inventory or {}).get(tool.id, 0) > 0 for tool in suitable)
        simplest = min(suitable, key=lambda tool: (tool.tier_rank, tool.id))
        return simplest.id, owned

    def locate(self, item: str, store: MemoryStore, *,
               inventory: Optional[Dict[str, int]] = None,
               position: Optional[Position] = None) -> List[Located]:
        """Где взять предмет по памяти: увиденные источники, ближайшие — первыми.

        Источники в другом измерении идут после источников рядом: расстояния до них нет.
        """
        sources = self.sources(item)
        located: List[Located] = []
        for fact in store.facts(RESOURCE):
            source = sources.get(fact.subject)
            if fact.subject != item and source is None:
                continue
            distance = fact.position.distance(position) if fact.position else None
            entry = Located(fact=fact, source=source, distance=distance)
            if source is not None:
                entry.tool, entry.tool_ok = self.tool_check(source, inventory)
            located.append(entry)
        located.sort(key=lambda entry: (entry.distance is None,
                                        entry.distance if entry.distance is not None else 0,
                                        entry.fact.last_seen))
        return located

    def known_sources(self, store: MemoryStore, position: Optional[Position] = None) -> Dict[str, str]:
        """Увиденные источники для планировщика: блок → где ближайший, строкой."""
        nearest: Dict[str, Fact] = {}
        for fact in store.facts(RESOURCE):
            if fact.position is None or not fact.subject:
                continue
            best = nearest.get(fact.subject)
            if best is None or _closer(fact, best, position):
                nearest[fact.subject] = fact
        return {subject: _place(fact, position) for subject, fact in nearest.items()}

    def storage(self, store: MemoryStore, position: Optional[Position] = None):
        """Запасы хранилищ для планировщика: предмет → количество и где лежит больше всего.

        Берутся хранилища того измерения, где игрок; без позиции — все.
        """
        dimension = position.dimension if position else None
        totals: Dict[str, int] = {}
        where: Dict[str, str] = {}
        for item, places in store.storage(dimension).items():
            totals[item] = sum(count for _, count in places)
            fact, _ = places[0]
            where[item] = _place(fact, position)
        return totals, where


def _closer(fact: Fact, other: Fact, position: Optional[Position]) -> bool:
    mine, theirs = fact.position.distance(position), other.position.distance(position)
    if mine is None:
        return False
    return theirs is None or mine < theirs


def _place(fact: Fact, position: Optional[Position]) -> str:
    """«Сундук, x 12, y 64, z −40» — коротко, для шага плана."""
    text = f"{fact.label}, {fact.position.describe(with_dimension=False)}" if fact.position \
        else (fact.label or "")
    relative = fact.position.relative(position) if fact.position else None
    return f"{text} ({relative})" if relative else text


def tool_sentence(entry: Located, kb: KnowledgeBase) -> Optional[str]:
    """«Нужна железная кирка — она у тебя есть.»"""
    if entry.tool is None:
        return None
    name = kb.label(entry.tool) or entry.tool
    lowered = name[:1].lower() + name[1:]
    tier = entry.source.tool_tier if entry.source else None
    head = f"Нужна {lowered} или лучше" if tier and tier != "wood" else f"Нужна {lowered}"
    return f"{head} — " + ("она у тебя есть." if entry.tool_ok else "её у тебя нет.")
