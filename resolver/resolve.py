"""Сопоставление реплики игрока с сущностью онтологии.

Резолвер отвечает на один вопрос: о каком объекте базы знаний идёт речь. Он не решает,
допустимо ли действие, — это дело валидатора, — и не извлекает количество.

Ответ у него трёхзначный: сущность найдена, запрос неоднозначен, ничего похожего нет.
Средний случай существен: принятая политика запрещает молча выбирать за игрока, когда
запрос допускает несколько прочтений. Отличие неоднозначности от отсутствия проводится по
тому, объясняет ли кандидат запрос целиком (см. MIN_QUERY_COVERAGE в lexical.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from kb.graph import KnowledgeBase
from resolver.lexical import Candidate, LexicalIndex

RESOURCE_ID_PATTERN = re.compile(r"(minecraft:[a-z0-9_./-]+)")

# Оценка лидера, ниже которой сопоставление не считается состоявшимся. Порог низкий
# намеренно: отсутствие сопоставления определяется прежде всего тем, что имя не объясняет
# запрос целиком (MIN_QUERY_COVERAGE), а оценка отделяет уверенные совпадения от слабых.
MIN_SCORE = 0.30

# Отрыв лидера от второго кандидата, ниже которого выбор остаётся за игроком. Запрос из
# родового слова («меч», «кирка», «доски») покрывает несколько названий одинаково и отрыва
# не даёт — это и есть неоднозначность, о которой система обязана спросить.
AMBIGUITY_MARGIN = 0.15

MAX_CANDIDATES = 5

# Какие виды сущностей уместны для действия.
KINDS_FOR_ACTION = {
    "give_item": ("item", "block"),
    "remove_item": ("item", "block"),
    "summon_entity": ("creature",),
}

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"


@dataclass
class Resolution:
    status: str                       # resolved | ambiguous | not_found
    query: str
    thing: Optional[str] = None
    label: Optional[str] = None
    kinds: Sequence[str] = ()
    confidence: float = 0.0
    candidates: List[Candidate] = field(default_factory=list)
    method: str = "lexical"           # explicit | lexical

    @property
    def ok(self) -> bool:
        return self.status == RESOLVED


class Resolver:
    def __init__(self, kb: Optional[KnowledgeBase] = None) -> None:
        self.kb = kb or KnowledgeBase.shared()
        self.index = LexicalIndex(self.kb)

    def resolve(self, query: str, *, action: Optional[str] = None) -> Resolution:
        text = (query or "").strip()
        if not text:
            return Resolution(status=NOT_FOUND, query=query or "")

        # Точный идентификатор в реплике снимает вопрос — но только если он есть в онтологии.
        explicit = RESOURCE_ID_PATTERN.search(text.lower())
        if explicit and self.kb.exists(explicit.group(1)):
            thing = explicit.group(1)
            return Resolution(
                status=RESOLVED,
                query=text,
                thing=thing,
                label=self.kb.label(thing) or thing,
                kinds=self.kb.kinds(thing),
                confidence=1.0,
                method="explicit",
            )

        kinds = KINDS_FOR_ACTION.get(action or "")
        candidates = self.index.search(text, kinds=kinds, limit=MAX_CANDIDATES)
        if not candidates or candidates[0].score < MIN_SCORE:
            return Resolution(status=NOT_FOUND, query=text, candidates=candidates)

        best = candidates[0]
        runner_up = candidates[1].score if len(candidates) > 1 else 0.0
        if best.score - runner_up < AMBIGUITY_MARGIN:
            tied = [item for item in candidates if best.score - item.score < AMBIGUITY_MARGIN]
            return Resolution(
                status=AMBIGUOUS,
                query=text,
                confidence=round(best.score - runner_up, 4),
                candidates=tied,
            )

        return Resolution(
            status=RESOLVED,
            query=text,
            thing=best.thing,
            label=best.label,
            kinds=best.kinds,
            confidence=best.score,
            candidates=candidates,
        )


_shared: Optional[Resolver] = None


def shared() -> Resolver:
    """Единственный резолвер на процесс: построение индекса занимает около секунды."""
    global _shared
    if _shared is None:
        _shared = Resolver()
    return _shared


def resolve(query: str, *, action: Optional[str] = None) -> Resolution:
    return shared().resolve(query, action=action)
