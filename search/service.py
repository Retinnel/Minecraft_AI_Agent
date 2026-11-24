"""Веб-поиск как источник знаний: отбор, порог релевантности, переход на запасной движок.

Поиск — третий источник знаний ассистента наряду с онтологией и моделью, и самый широкий:
он покрывает то, чего нет в онтологии, — тактику, поведение существ, постройки, другие игры.
Зато ему меньше всего доверия. Поэтому:

- результаты ниже порога релевантности отбрасываются. Замер на запросах про игры: верные
  страницы получают у Tavily 0,75–0,84, мусор (новости, биржевые сводки) — 0,03–0,05.
  Лучше честное «не нашёл», чем пересказ новости про акции издателя;
- у фрагментов без оценки (DuckDuckGo) проверяется, что в них вообще есть слова запроса;
- текст из сети — данные, а не указания. Он попадает только в ответ игроку и никогда — в
  выбор действия: выдать или призвать что-то на основании найденного нельзя.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from search.engines import Engine, Hit, SearchUnavailable, default_engines

# Порог релевантности для движков, которые её оценивают (см. замер выше). Движок может
# задать свой порог свойством `min_score`: шкалы оценок у движков разные.
MIN_SCORE = 0.3

# Сколько фрагментов отдаётся модели и какой длины. Ответ игроку — несколько фраз, а запрос к
# модели должен оставаться коротким: длинный контекст ухудшает ответ слабой модели.
MAX_SOURCES = 3
MAX_PASSAGE_CHARS = 1200

# Сколько хранить найденное. Повторный вопрос не тратит кредиты и не ждёт сети.
CACHE_SECONDS = 3600

# Слова запроса, по которым проверяется сниппет без оценки релевантности.
_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
_QUERY_FILLER = frozenset({"how", "what", "where", "when", "which", "does", "with", "from",
                           "that", "this", "best", "guide", "minecraft", "game"})


@dataclass
class SearchAnswer:
    """Итог поиска: принятые фрагменты и след того, как они получены."""

    query: str
    hits: List[Hit] = field(default_factory=list)
    engine: Optional[str] = None
    rejected: int = 0                 # отброшено по релевантности
    errors: List[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    cached: bool = False

    @property
    def found(self) -> bool:
        return bool(self.hits)

    def sources(self) -> List[Dict[str, str]]:
        return [{"title": hit.title, "url": hit.url, "site": hit.site} for hit in self.hits]


def clean_passage(text: str, limit: int = MAX_PASSAGE_CHARS) -> str:
    """Схлопывает пробелы и обрезает по границе предложения, а не посреди слова."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > limit // 2 else cut.rstrip() + "…"


def _mentions_query(hit: Hit, query: str) -> bool:
    words = {word.lower() for word in _WORD.findall(query)} - _QUERY_FILLER
    if not words:
        return True
    text = f"{hit.title} {hit.content}".lower()
    return any(word in text for word in words)


class WebSearch:
    def __init__(self, engines: Optional[Sequence[Engine]] = None, *,
                 cache_seconds: float = CACHE_SECONDS, clock=time.monotonic) -> None:
        self.engines = list(engines) if engines is not None else default_engines()
        self.cache_seconds = cache_seconds
        self._clock = clock
        self._cache: Dict[str, Tuple[float, SearchAnswer]] = {}

    @property
    def available(self) -> bool:
        return bool(self.engines)

    @staticmethod
    def compose(query: str, game: Optional[str]) -> str:
        """Название игры в запросе: без него «как победить босса» ищется в чём угодно.

        Модель часто сама пишет игру в запрос, иногда без артикля: «Witcher 3» при игре
        «The Witcher 3». Такое название не добавляется второй раз.
        """
        query = " ".join((query or "").split())
        # Игру, названную по-русски («Ведьмака 3»), в английский запрос не добавляем: русское
        # слово сбивает поисковик, а английское название модель пишет в запрос сама.
        if not game or _CYRILLIC.search(game):
            return query
        core = re.sub(r"^the\s+", "", game.strip(), flags=re.IGNORECASE).lower()
        if core not in query.lower():
            query = f"{game} {query}"
        return query

    def search(self, query: str, *, game: Optional[str] = None) -> SearchAnswer:
        full = self.compose(query, game)
        key = full.lower()
        cached = self._cache.get(key)
        if cached and self._clock() - cached[0] < self.cache_seconds:
            answer = cached[1]
            return SearchAnswer(query=answer.query, hits=list(answer.hits), engine=answer.engine,
                                rejected=answer.rejected, cached=True)

        started = time.perf_counter()
        answer = SearchAnswer(query=full)
        for engine in self.engines:
            try:
                hits = engine.search(full, max_results=MAX_SOURCES + 2)
            except SearchUnavailable as error:
                answer.errors.append(str(error))
                continue
            threshold = getattr(engine, "min_score", None)
            accepted = [hit for hit in hits if self._relevant(hit, full, threshold)]
            answer.rejected += len(hits) - len(accepted)
            if accepted:
                answer.hits = [Hit(title=hit.title, url=hit.url, content=clean_passage(hit.content),
                                   score=hit.score, engine=hit.engine)
                               for hit in accepted[:MAX_SOURCES]]
                answer.engine = engine.name
                break
        answer.elapsed_ms = (time.perf_counter() - started) * 1000

        # Сбой сети не кэшируется: через минуту поиск может заработать.
        if answer.found or not answer.errors:
            self._cache[key] = (self._clock(), answer)
        return answer

    @staticmethod
    def _relevant(hit: Hit, query: str, threshold: Optional[float] = None) -> bool:
        if not hit.content.strip():
            return False
        if hit.score is not None:
            return hit.score >= (MIN_SCORE if threshold is None else threshold)
        return _mentions_query(hit, query)


_shared: Optional[WebSearch] = None


def shared() -> WebSearch:
    """Один сервис на процесс: кэш найденного общий для всех запросов."""
    global _shared
    if _shared is None:
        _shared = WebSearch()
    return _shared
