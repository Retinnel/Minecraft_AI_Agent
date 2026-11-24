"""Поисковые движки: сменные источники знаний из сети.

Движок знает только запрос и возвращает найденные фрагменты страниц. Он ничего не знает ни
о Minecraft, ни об игроке, ни о конвейере — поэтому тот же поиск годится для любой игры.

Основной движок — Tavily: API, сделанный для языковых агентов, возвращает не только ссылки,
но и уже очищенный текст релевантных фрагментов страниц вместе с оценкой релевантности.
Второй — Search API DeepSeek: работает с тем же ключом, что языковая модель, отвечает за
секунду и тоже оценивает релевантность, но чаще приводит страницы-агрегаторы. Запасной —
DuckDuckGo без ключа: он даёт только короткие сниппеты и может отказать без
предупреждения, но держит ассистента в строю, если остальные недоступны.

Порядок задаётся переменной `SEARCH_ENGINES` (по умолчанию `tavily,deepseek,duckduckgo`);
движок без ключа или без библиотеки пропускается.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence

TAVILY_URL = "https://api.tavily.com/search"
DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"

# Порядок движков, если он не задан переменной SEARCH_ENGINES.
DEFAULT_ORDER = ("tavily", "deepseek", "duckduckgo")

# Сайты, текст которых для ответа бесполезен: у видео в выдаче — только описание ролика,
# у соцсетей — пересказ без проверки (живой замер: facebook.com в источниках о визере).
EXCLUDED_DOMAINS = ("youtube.com", "tiktok.com", "instagram.com", "facebook.com",
                    "pinterest.com")

# Общая шкала релевантности — от 0 до 1 с двумя опорными точками, одинаковыми для всех
# движков: ниже ACCEPTABLE страница в ответ не идёт, около GOOD лежит типичная страница по
# теме вопроса.
ACCEPTABLE = 0.3
GOOD = 0.8


def normalize(raw: float, acceptable: float, good: float) -> float:
    """Оценка движка на общей шкале.

    У движков свои шкалы: Tavily оценивает от 0 до 1, мусору даёт 0,03–0,05, страницам по
    теме — 0,75–0,85; DeepSeek оценивает от 0 до 3, мусору даёт 0,6–0,9, страницам по теме —
    1,6–1,96. Сравнивать такие числа с одним порогом нельзя, а держать порог у каждого
    движка — значит размазать одно правило по нескольким местам.

    Движок называет две точки своей шкалы: с какой оценки страница приемлема и какую
    оценку получает типичная страница по теме. Первая переходит в ACCEPTABLE, вторая — в
    GOOD, между ними и за ними оценка пересчитывается линейно и не выходит за [0, 1].
    Одной точки мало: при пересчёте только по границе хорошие страницы DeepSeek ложились
    на 0,32–0,48, а Tavily — на 0,57–0,86, и одинаковые числа значили разное.
    """
    raw = max(0.0, float(raw))
    if raw <= acceptable:
        value = ACCEPTABLE * raw / acceptable
    else:
        value = ACCEPTABLE + (GOOD - ACCEPTABLE) * (raw - acceptable) / (good - acceptable)
    # Округление убирает хвост двоичной арифметики: 0,84 у движка остаётся 0,84 и здесь.
    return round(min(value, 1.0), 6)


# Сколько ждать ответа поисковика. Вопрос игрока — живой диалог: дольше ждать нельзя,
# лучше перейти к запасному движку.
TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class Hit:
    """Найденный фрагмент страницы."""

    title: str
    url: str
    content: str
    # Релевантность на общей шкале от 0 до 1 (см. normalize). У движков без оценки — None.
    score: Optional[float]
    engine: str
    # Оценка в том виде, в каком её дал движок: для журнала и замеров.
    raw_score: Optional[float] = None

    @property
    def site(self) -> str:
        """Домен без www — так источник называется игроку."""
        match = re.match(r"https?://(?:www\.)?([^/]+)", self.url)
        return match.group(1) if match else self.url


class SearchUnavailable(RuntimeError):
    """Сбой поисковика: сеть, ключ, квота. Не то же самое, что «ничего не найдено»."""


class Engine(Protocol):
    name: str

    def search(self, query: str, *, max_results: int = 5) -> List[Hit]: ...



class TavilyEngine:
    """Tavily: обычный поиск стоит 1 кредит, углублённый — 2; бесплатно 1000 в месяц."""

    name = "tavily"
    # Шкала от 0 до 1; по замеру страницы по теме получают 0,75–0,85, мусор — 0,03–0,05.
    # Обе опорные точки совпадают с общими, так что пересчёт оценку не меняет.
    SCALE = (0.3, 0.8)

    def __init__(self, api_key: str, *, depth: str = "basic", client=None,
                 excluded: Sequence[str] = EXCLUDED_DOMAINS) -> None:
        if not api_key:
            raise ValueError("Для Tavily нужен ключ TAVILY_API_KEY")
        self.api_key = api_key
        self.depth = depth
        self.excluded = list(excluded)
        self._client = client

    def _post(self, payload):
        import httpx

        client = self._client or httpx.Client(timeout=TIMEOUT_SECONDS)
        try:
            return client.post(TAVILY_URL, json=payload,
                               headers={"Authorization": f"Bearer {self.api_key}"})
        finally:
            if self._client is None:
                client.close()

    def search(self, query: str, *, max_results: int = 5) -> List[Hit]:
        payload = {"query": query, "search_depth": self.depth, "max_results": max_results,
                   "exclude_domains": self.excluded}
        try:
            response = self._post(payload)
        except Exception as error:
            raise SearchUnavailable(f"Tavily недоступен: {type(error).__name__}: {error}") from error
        if response.status_code != 200:
            raise SearchUnavailable(f"Tavily ответил {response.status_code}: {response.text[:200]}")
        results = response.json().get("results") or []
        return [Hit(title=str(item.get("title") or ""), url=str(item.get("url") or ""),
                    content=str(item.get("content") or ""),
                    score=(normalize(item["score"], *self.SCALE)
                           if item.get("score") is not None else None),
                    engine=self.name,
                    raw_score=float(item["score"]) if item.get("score") is not None else None)
                for item in results if item.get("url")]


class DeepSeekEngine:
    """Search API DeepSeek: 0,75 $ за тысячу успешных запросов, ключ тот же, что у модели.

    Замер 2026-11-24 (шесть запросов об играх): ответ за секунду; оценка `rerank_score` от
    0 до 3 у страниц по теме — 1,4–1,96, у запроса о выдуманном существе — 0,56–0,86.
    Провайдер считает качественными результаты от 1,5; эта оценка и названа границей
    приемлемого при пересчёте на общую шкалу. Без указания
    языка в выдачу попадают китайские и японские страницы, поэтому к запросу латиницей
    добавляется оператор `lang:en`. Одна площадка с автоматически собранными ответами
    занимала первые три места из шести — берётся не больше одной страницы с сайта.
    """

    name = "deepseek"
    # Шкала от 0 до 3: приемлемое — от 1,5 (граница провайдера), типичная страница по теме
    # на замере — около 1,9.
    SCALE = (1.5, 1.9)
    MAX_QUERY = 128          # предел длины запроса у API
    FETCHED = 10             # запрашивается с запасом: часть результатов отсеивается здесь
    # Площадки с автоматически собранными ответами. На замере ncesc.com стоял первым
    # источником в пяти ответах из шести, и неточности в ответах шли оттуда.
    AGGREGATORS = ("ncesc.com",)

    def __init__(self, api_key: str, *, client=None,
                 excluded: Sequence[str] = EXCLUDED_DOMAINS + AGGREGATORS) -> None:
        if not api_key:
            raise ValueError("Для поиска DeepSeek нужен ключ DEEPSEEK_API")
        self.api_key = api_key
        self.excluded = tuple(excluded)
        self._client = client

    def _post(self, payload):
        import httpx

        client = self._client or httpx.Client(timeout=TIMEOUT_SECONDS)
        try:
            return client.post(DEEPSEEK_SEARCH_URL, json=payload,
                               headers={"Authorization": f"Bearer {self.api_key}"})
        finally:
            if self._client is None:
                client.close()

    @classmethod
    def compose(cls, query: str) -> str:
        """Запрос в пределах длины API; для запроса латиницей — только английские страницы."""
        query = " ".join((query or "").split())
        suffix = "" if re.search(r"[^\x00-\x7f]", query) or "lang:" in query else " lang:en"
        room = cls.MAX_QUERY - len(suffix)
        if len(query) > room:
            query = query[:room].rsplit(" ", 1)[0]
        return query + suffix

    def search(self, query: str, *, max_results: int = 5) -> List[Hit]:
        payload = {"query": self.compose(query), "count": max(self.FETCHED, max_results)}
        try:
            response = self._post(payload)
        except Exception as error:
            raise SearchUnavailable(
                f"Поиск DeepSeek недоступен: {type(error).__name__}: {error}") from error
        if response.status_code != 200:
            raise SearchUnavailable(
                f"Поиск DeepSeek ответил {response.status_code}: {response.text[:200]}")

        hits: List[Hit] = []
        sites = set()
        for item in response.json().get("results") or []:
            url = str(item.get("url") or "")
            if not url or any(domain in url for domain in self.excluded):
                continue
            # Выжимка длиннее и написана для модели; короткий фрагмент — запасной вариант.
            hit = Hit(title=str(item.get("title") or ""), url=url,
                      content=str(item.get("summary") or item.get("snippet") or ""),
                      score=(normalize(item["rerank_score"], *self.SCALE)
                             if item.get("rerank_score") is not None else None),
                      engine=self.name,
                      raw_score=(float(item["rerank_score"])
                                 if item.get("rerank_score") is not None else None))
            if hit.site in sites:
                continue
            sites.add(hit.site)
            hits.append(hit)
            if len(hits) >= max_results:
                break
        return hits


class DuckDuckGoEngine:
    """DuckDuckGo без ключа: только сниппеты, оценки релевантности нет."""

    name = "duckduckgo"

    def __init__(self, backend=None) -> None:
        self._backend = backend

    def search(self, query: str, *, max_results: int = 5) -> List[Hit]:
        try:
            if self._backend is None:
                from ddgs import DDGS
                backend = DDGS()
            else:
                backend = self._backend
            results = backend.text(query, max_results=max_results)
        except Exception as error:
            # «Ничего не найдено» библиотека тоже сообщает исключением.
            if "No results" in str(error):
                return []
            raise SearchUnavailable(f"DuckDuckGo недоступен: {type(error).__name__}: {error}") from error
        return [Hit(title=str(item.get("title") or ""), url=str(item.get("href") or ""),
                    content=str(item.get("body") or ""), score=None, engine=self.name)
                for item in results or []
                if item.get("href") and not any(d in item["href"] for d in EXCLUDED_DOMAINS)]


def build_engine(name: str) -> Optional[Engine]:
    """Движок по имени или None, если для него нет ключа либо библиотеки."""
    from api import keys

    if name == "tavily":
        key = keys.get("TAVILY_API_KEY")
        return TavilyEngine(key) if key else None
    if name == "deepseek":
        key = keys.get("DEEPSEEK_API", "DEEPSEEK_API_KEY")
        return DeepSeekEngine(key) if key else None
    if name == "duckduckgo":
        try:
            import ddgs  # noqa: F401
        except ImportError:
            return None
        return DuckDuckGoEngine()
    return None


def default_engines(order: Optional[Sequence[str]] = None) -> List[Engine]:
    """Доступные движки в порядке из SEARCH_ENGINES (или в порядке по умолчанию)."""
    if order is None:
        listed = [name.strip().lower() for name in os.getenv("SEARCH_ENGINES", "").split(",")]
        order = [name for name in listed if name] or DEFAULT_ORDER
    engines = [build_engine(name) for name in dict.fromkeys(order)]
    return [engine for engine in engines if engine is not None]
