"""Поисковые движки: сменные источники знаний из сети.

Движок знает только запрос и возвращает найденные фрагменты страниц. Он ничего не знает ни
о Minecraft, ни об игроке, ни о конвейере — поэтому тот же поиск годится для любой игры.

Основной движок — Tavily: API, сделанный для языковых агентов, возвращает не только ссылки,
но и уже очищенный текст релевантных фрагментов страниц вместе с оценкой релевантности.
Запасной — DuckDuckGo без ключа: он даёт только короткие сниппеты и может отказать без
предупреждения, но держит ассистента в строю, если основной недоступен.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence

TAVILY_URL = "https://api.tavily.com/search"

# Сайты, текст которых для ответа бесполезен: у видео в выдаче — только описание ролика,
# у соцсетей — пересказ без проверки (живой замер: facebook.com в источниках о визере).
EXCLUDED_DOMAINS = ("youtube.com", "tiktok.com", "instagram.com", "facebook.com",
                    "pinterest.com")

# Сколько ждать ответа поисковика. Вопрос игрока — живой диалог: дольше ждать нельзя,
# лучше перейти к запасному движку.
TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class Hit:
    """Найденный фрагмент страницы."""

    title: str
    url: str
    content: str
    # Релевантность по оценке движка, от 0 до 1. У движков без оценки — None.
    score: Optional[float]
    engine: str

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
                    score=float(item["score"]) if item.get("score") is not None else None,
                    engine=self.name)
                for item in results if item.get("url")]


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


def default_engines() -> List[Engine]:
    """Движки по доступности: Tavily, если есть ключ, затем DuckDuckGo, если стоит ddgs."""
    engines: List[Engine] = []
    from api import keys
    key = keys.get("TAVILY_API_KEY")
    if key:
        engines.append(TavilyEngine(key))
    try:
        import ddgs  # noqa: F401
        engines.append(DuckDuckGoEngine())
    except ImportError:
        pass
    return engines
