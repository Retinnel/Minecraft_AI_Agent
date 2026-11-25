"""Отбор фактов памяти под вопрос игрока и их описание словами.

Отбор детерминированный: совпадение по сущности онтологии (в Minecraft) или по основам
слов (в любой игре), затем расстояние от игрока, затем свежесть. Модель в отборе не
участвует, а описание собирается из полей факта — координаты, количества и время в
ответ ставит система.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, List, Optional, Sequence

from memory.model import (CONTAINER, DEATH, DIALOGUE, MOD, NOTE, PLACE, PLAYER, RESOURCE,
                          SCREEN, Fact, Position)
from memory.store import MemoryStore
from resolver.text import query_stems

DEFAULT_LIMIT = 10

# Доля основ запроса, которая должна найтись в факте. Половина — чтобы «что говорила
# Кейра про лекарство» находило реплику Кейры, где нет слова «говорила».
MIN_OVERLAP = 0.5

SOURCE_NAMES = {
    MOD: "по сведениям мода",
    PLAYER: "по твоим словам",
    SCREEN: "распознано по снимку экрана, может быть неточным",
}


@dataclass
class Match:
    fact: Fact
    score: float
    distance: Optional[int]


def _stems(text: Optional[str]) -> set:
    return set(query_stems(text or "")) if text else set()


def _fact_stems(fact: Fact, label_of: Callable[[str], Optional[str]]) -> set:
    words = [fact.label, fact.text]
    if fact.kind == CONTAINER:
        words.extend(label_of(item) or item for item in fact.contents)
    if fact.kind in (RESOURCE, CONTAINER) and fact.subject:
        words.append(label_of(fact.subject) or fact.subject)
    return set().union(*(_stems(word) for word in words if word))


def recall(store: MemoryStore, query: Optional[str] = None, *,
           subjects: Iterable[str] = (), kinds: Optional[Sequence[str]] = None,
           position: Optional[Position] = None, limit: int = DEFAULT_LIMIT,
           label_of: Optional[Callable[[str], Optional[str]]] = None) -> List[Match]:
    """Факты, относящиеся к вопросу, лучшие — первыми.

    subjects — сущности онтологии, которые назвал игрок (уже определённые резолвером);
    совпадение с ними сильнее совпадения по словам. Без запроса и сущностей отбираются
    все факты нужного вида — так отвечается «где я умер».
    """
    label_of = label_of or (lambda thing: None)
    wanted = set(subjects or ())
    asked = _stems(query)
    matches: List[Match] = []
    for fact in store.facts():
        if kinds and fact.kind not in kinds:
            continue
        score = 0.0
        if wanted and (fact.subject in wanted or wanted & set(fact.contents)):
            score = 2.0
        elif asked:
            overlap = len(asked & _fact_stems(fact, label_of)) / len(asked)
            if overlap >= MIN_OVERLAP:
                score = overlap
        elif not wanted:
            score = 0.5
        if score <= 0:
            continue
        distance = fact.position.distance(position) if fact.position else None
        matches.append(Match(fact=fact, score=score * fact.confidence, distance=distance))

    matches.sort(key=lambda match: (
        -match.score,
        match.distance if match.distance is not None else float("inf"),
        _negated_time(match.fact.last_seen)))
    return matches[:max(1, limit)]


def _negated_time(stamp: str) -> float:
    moment = _parse(stamp)
    return -moment.timestamp() if moment else 0.0


def _parse(stamp: str) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def age(stamp: str, now: Optional[datetime] = None) -> Optional[str]:
    """«только что», «12 минут назад», «вчера» — сколько прошло с наблюдения."""
    moment = _parse(stamp)
    if moment is None:
        return None
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} {_plural(minutes, 'минуту', 'минуты', 'минут')} назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} {_plural(hours, 'час', 'часа', 'часов')} назад"
    days = hours // 24
    if days == 1:
        return "вчера"
    return f"{days} {_plural(days, 'день', 'дня', 'дней')} назад"


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many


def _items(contents, label_of, limit: int = 6) -> str:
    ranked = sorted(contents.items(), key=lambda pair: (-pair[1], pair[0]))
    parts = [f"{label_of(item) or item} ×{count}" for item, count in ranked[:limit]]
    if len(ranked) > limit:
        parts.append(f"ещё {len(ranked) - limit}")
    return ", ".join(parts)


def describe(fact: Fact, *, origin: Optional[Position] = None,
             label_of: Optional[Callable[[str], Optional[str]]] = None,
             now: Optional[datetime] = None, show_source: bool = True) -> str:
    """Факт одной фразой: что, где, насколько далеко, когда, откуда известно."""
    label_of = label_of or (lambda thing: None)
    name = fact.label or (label_of(fact.subject) if fact.subject else None) or fact.subject or ""
    head = name
    if fact.kind == RESOURCE and fact.amount:
        head = f"{name}, {fact.amount} {_plural(fact.amount, 'блок', 'блока', 'блоков')}"
    elif fact.kind == DEATH:
        head = f"Гибель ({name})" if name else "Гибель"
    elif fact.kind == DIALOGUE:
        head = f"{name or 'Собеседник'}: «{fact.text}»"
    elif fact.kind in (PLACE, NOTE) and fact.text and fact.text != fact.label:
        head = f"{name} — {fact.text}" if name else fact.text

    details: List[str] = []
    if fact.position is not None:
        details.append(fact.position.describe())
        relative = fact.position.relative(origin)
        if relative:
            details.append(relative)
    if fact.kind == CONTAINER:
        details.append(("внутри: " + _items(fact.contents, label_of)) if fact.contents else "пусто")
    if fact.kind == DEATH and fact.contents:
        details.append("с собой было: " + _items(fact.contents, label_of, limit=4))
    when = age(fact.last_seen, now)
    if when:
        details.append(when)
    text = f"{head}: {', '.join(details)}" if details else head
    if show_source:
        text += f" ({SOURCE_NAMES.get(fact.source, fact.source)})"
    return text


def numbers(fact: Fact) -> List[int]:
    """Числа, которые пересказ обязан сохранить: координаты и количество блоков."""
    found: List[int] = []
    if fact.position is not None:
        found.extend([fact.position.x, fact.position.y, fact.position.z])
    return found
