"""Хранилище фактов памяти одного мира.

Факты лежат в `memory.jsonl` в папке мира, по строке на факт, и целиком подняты в память
процесса: фактов в мире — тысячи, поиск по ним перебором занимает доли миллисекунды.

Повторное наблюдение не плодит записей, а обновляет имеющуюся:

- руда — одна запись на тип блока и участок 16×16, в ней координаты блоков; блок, который
  мод больше не видит на своём месте, из записи убирается, а опустевшая запись становится
  `stale`;
- хранилище — одна запись на координаты, содержимое заменяется последним увиденным;
- отметка и заметка — одна запись на название: «запомни, тут база» второй раз переносит
  отметку, а не заводит вторую базу;
- реплика с экрана — одна запись на текст: тот же диалог на следующем кадре не повторяется.

Запись по просьбе «забудь» не стирается, а получает `removed`: ни в ответы, ни в план
она больше не попадает. Лимит фактов на мир соблюдается вытеснением: сначала забытые,
затем устаревшие, затем самые давние наблюдения; заметки игрока вытесняются последними.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from memory.model import (ACTIVE, CONFIDENCE, CONTAINER, DEATH, DIALOGUE, MAX_BLOCKS, MAX_LABEL,
                          MAX_TEXT, MOD, NOTE, PLACE, PLAYER, REMOVED, RESOURCE, SCREEN, STALE,
                          Fact, Position, now_iso)

logger = logging.getLogger(__name__)

FILE_NAME = "memory.jsonl"
DEFAULT_MAX_FACTS = 5000

# Сторона участка, по которому группируются руды: совпадает с чанком игры.
AREA = 16

# Порядок вытеснения при переполнении: меньшее значение уходит первым.
EVICTION_ORDER = {REMOVED: 0, STALE: 1}
EVICTION_KIND = {RESOURCE: 0, DIALOGUE: 0, CONTAINER: 1, DEATH: 1, PLACE: 2, NOTE: 2}

# Сколько гибелей помнить: для «где я умер» важна последняя, остальные — история.
MAX_DEATHS = 20


def _clean(text: Optional[str], limit: int) -> Optional[str]:
    text = " ".join(str(text or "").split())
    return text[:limit] if text else None


def _key(text: Optional[str]) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def area_of(position: Position) -> Tuple[str, int, int]:
    return position.dimension, position.x // AREA, position.z // AREA


class MemoryStore:
    def __init__(self, path: Optional[Path] = None, *, game: str = "Minecraft", world: str = "",
                 max_facts: int = DEFAULT_MAX_FACTS) -> None:
        self.path = Path(path) if path else None
        self.game = game
        self.world = world
        self.max_facts = max(1, int(max_facts))
        self._facts: Dict[str, Fact] = {}
        if self.path is not None:
            self.load()

    # --- файл ---

    def load(self) -> None:
        self._facts = {}
        if self.path is None:
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                fact = Fact.from_dict(json.loads(line))
            except json.JSONDecodeError:
                continue
            if fact is not None:
                self._facts[fact.id] = fact

    def save(self) -> None:
        """Файл переписывается целиком через временный: оборванная запись не портит память."""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                "".join(json.dumps(fact.to_dict(), ensure_ascii=False) + "\n"
                        for fact in self._facts.values()),
                encoding="utf-8")
            temporary.replace(self.path)
        except OSError as error:
            logger.warning(f"[Memory] Память не сохранена: {error}")

    # --- чтение ---

    def __len__(self) -> int:
        return len(self._facts)

    def get(self, fact_id: str) -> Optional[Fact]:
        return self._facts.get(str(fact_id))

    def facts(self, kind: Optional[str] = None, *, statuses: Sequence[str] = (ACTIVE,)) -> List[Fact]:
        return [fact for fact in self._facts.values()
                if (kind is None or fact.kind == kind) and fact.status in statuses]

    def last_death(self) -> Optional[Fact]:
        deaths = self.facts(DEATH)
        return max(deaths, key=lambda fact: fact.last_seen) if deaths else None

    def storage(self, dimension: Optional[str] = None) -> Dict[str, List[Tuple[Fact, int]]]:
        """Что где лежит: предмет → хранилища с количеством, самые полные — первыми."""
        found: Dict[str, List[Tuple[Fact, int]]] = defaultdict(list)
        for fact in self.facts(CONTAINER):
            if dimension and fact.position and fact.position.dimension != dimension:
                continue
            for item, count in fact.contents.items():
                found[item].append((fact, count))
        for item in found:
            found[item].sort(key=lambda pair: -pair[1])
        return dict(found)

    # --- запись ---

    def _new(self, kind: str, *, subject: Optional[str] = None, label: Optional[str] = None,
             position: Optional[Position] = None, text: Optional[str] = None,
             source: str = MOD, now: Optional[str] = None) -> Fact:
        now = now or now_iso()
        fact = Fact(id=uuid.uuid4().hex[:12], game=self.game, world=self.world, kind=kind,
                    subject=subject, label=_clean(label, MAX_LABEL), position=position,
                    text=_clean(text, MAX_TEXT), source=source,
                    confidence=CONFIDENCE.get(source, 1.0), first_seen=now, last_seen=now)
        self._facts[fact.id] = fact
        return fact

    @staticmethod
    def _seen(fact: Fact, now: str) -> None:
        fact.last_seen = now
        fact.seen_count += 1
        fact.status = ACTIVE

    def observe_blocks(self, blocks: Iterable[Tuple[str, Position]],
                       removed: Iterable[Position] = (), *,
                       labels: Optional[Dict[str, str]] = None,
                       now: Optional[str] = None) -> Dict[str, int]:
        """Пакет наблюдений сканера: найденные блоки и места, где блока больше нет."""
        now = now or now_iso()
        labels = labels or {}
        added = updated = stale = 0

        index: Dict[Tuple[str, Tuple[str, int, int]], Fact] = {}
        for fact in self.facts(RESOURCE, statuses=(ACTIVE, STALE)):
            if fact.position is not None:
                index[(fact.subject, area_of(fact.position))] = fact

        touched = set()
        for subject, position in blocks:
            key = (subject, area_of(position))
            fact = index.get(key)
            coordinates = (position.x, position.y, position.z)
            if fact is None:
                fact = self._new(RESOURCE, subject=subject, label=labels.get(subject) or subject,
                                 position=position, now=now)
                fact.blocks = [coordinates]
                index[key] = fact
                added += 1
                touched.add(fact.id)
                continue
            if coordinates not in fact.blocks and len(fact.blocks) < MAX_BLOCKS:
                fact.blocks.append(coordinates)
            if fact.id not in touched:
                self._seen(fact, now)
                touched.add(fact.id)
                updated += 1

        for position in removed:
            coordinates = (position.x, position.y, position.z)
            for fact in self.facts(RESOURCE):
                if fact.position is None or area_of(fact.position) != area_of(position):
                    continue
                if coordinates not in fact.blocks:
                    continue
                fact.blocks.remove(coordinates)
                if fact.blocks:
                    # Позицией записи остаётся блок, который ещё есть.
                    x, y, z = fact.blocks[0]
                    fact.position = Position(fact.position.dimension, x, y, z)
                else:
                    fact.status = STALE
                    stale += 1

        self._limit()
        return {"added": added, "updated": updated, "stale": stale}

    def record_container(self, block: str, position: Position, items: Dict[str, int], *,
                         label: Optional[str] = None, now: Optional[str] = None) -> Fact:
        """Содержимое хранилища при закрытии: заменяет прежнее, пустое тоже запоминается."""
        now = now or now_iso()
        contents = {}
        for item, count in (items or {}).items():
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if count > 0 and item:
                contents[str(item)] = contents.get(str(item), 0) + count
        fact = next((existing for existing in self.facts(CONTAINER, statuses=(ACTIVE, STALE))
                     if existing.position == position), None)
        if fact is None:
            fact = self._new(CONTAINER, subject=block, label=label or block, position=position,
                             now=now)
        else:
            self._seen(fact, now)
            fact.subject = block
            fact.label = _clean(label, MAX_LABEL) or fact.label
        fact.contents = contents
        self._limit()
        return fact

    def note(self, label: str, text: Optional[str] = None, *,
             position: Optional[Position] = None, source: str = PLAYER,
             now: Optional[str] = None) -> Fact:
        """Отметка места (с позицией) или заметка (без неё). Название — ключ записи."""
        now = now or now_iso()
        kind = PLACE if position is not None else NOTE
        label = _clean(label, MAX_LABEL) or _clean(text, MAX_LABEL) or "заметка"
        fact = next((existing for existing in self.facts(PLACE) + self.facts(NOTE)
                     if _key(existing.label) == _key(label)), None)
        if fact is None:
            fact = self._new(kind, subject=None, label=label, position=position, text=text,
                             source=source, now=now)
        else:
            self._seen(fact, now)
            fact.kind = kind
            fact.position = position
            fact.text = _clean(text, MAX_TEXT) or fact.text
            fact.source = source
            fact.confidence = CONFIDENCE.get(source, fact.confidence)
        self._limit()
        return fact

    def record_death(self, position: Optional[Position], cause: str,
                     inventory: Optional[Dict[str, int]] = None, *,
                     label: Optional[str] = None, now: Optional[str] = None) -> Fact:
        """Гибель: место, причина и что было с собой. Каждая гибель — своя запись."""
        now = now or now_iso()
        fact = self._new(DEATH, subject=_clean(cause, MAX_LABEL), label=label or cause,
                         position=position, now=now)
        fact.contents = {str(item): int(count) for item, count in (inventory or {}).items()
                         if isinstance(count, (int, float)) and count > 0}
        deaths = sorted(self.facts(DEATH, statuses=(ACTIVE, STALE, REMOVED)),
                        key=lambda item: item.last_seen)
        for old in deaths[:-MAX_DEATHS]:
            del self._facts[old.id]
        self._limit()
        return fact

    def record_dialogue(self, speaker: Optional[str], line: str, *,
                        now: Optional[str] = None) -> Optional[Fact]:
        """Реплика персонажа с экрана. Повтор того же текста только обновляет время."""
        line = _clean(line, MAX_TEXT)
        if not line:
            return None
        now = now or now_iso()
        speaker = _clean(speaker, MAX_LABEL)
        digest = hashlib.sha1(f"{_key(speaker)}|{_key(line)}".encode("utf-8")).hexdigest()[:16]
        fact = next((existing for existing in self.facts(DIALOGUE, statuses=(ACTIVE, STALE))
                     if existing.subject == digest), None)
        if fact is None:
            fact = self._new(DIALOGUE, subject=digest, label=speaker, text=line, source=SCREEN,
                             now=now)
        else:
            self._seen(fact, now)
        self._limit()
        return fact

    def forget(self, fact_id: str) -> Optional[Fact]:
        fact = self._facts.get(str(fact_id))
        if fact is None or fact.status == REMOVED:
            return None
        fact.status = REMOVED
        return fact

    # --- лимит ---

    def _limit(self) -> None:
        excess = len(self._facts) - self.max_facts
        if excess <= 0:
            return
        ranked = sorted(self._facts.values(), key=lambda fact: (
            EVICTION_ORDER.get(fact.status, 2), EVICTION_KIND.get(fact.kind, 1), fact.last_seen))
        for fact in ranked[:excess]:
            del self._facts[fact.id]
