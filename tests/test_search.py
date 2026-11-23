"""Веб-поиск: движки, порог релевантности, переход на запасной движок, кэш.

В сеть тесты не ходят: Tavily подменяется транспортом httpx, DuckDuckGo — фальшивым
бэкендом. Проверяется то, что делает поиск надёжным источником, а не болтовнёй: мусор с
низкой релевантностью отбрасывается, сбой одного движка не роняет ответ, сбой сети не
выдаётся за «ничего не нашлось».
"""

import json
import unittest

import httpx

from search.engines import DuckDuckGoEngine, Hit, SearchUnavailable, TavilyEngine
from search.service import MAX_PASSAGE_CHARS, WebSearch, clean_passage


def tavily_client(results, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(status, json={"results": results})
    return httpx.Client(transport=httpx.MockTransport(handler))


class FakeEngine:
    def __init__(self, name, hits=(), error=None):
        self.name = name
        self.hits = list(hits)
        self.error = error
        self.calls = 0

    def search(self, query, *, max_results=5):
        self.calls += 1
        if self.error:
            raise SearchUnavailable(self.error)
        return self.hits


def hit(score, content="Margit is weak to bleed. Use Spirit Ash summons.", url="https://polygon.com/x",
        title="How to beat Margit"):
    return Hit(title=title, url=url, content=content, score=score, engine="fake")


class TavilyEngineTests(unittest.TestCase):
    def test_results_are_parsed(self):
        client = tavily_client([{"title": "Guide", "url": "https://www.polygon.com/g",
                                 "content": "Heal at 50% HP.", "score": 0.84}])
        hits = TavilyEngine("key", client=client).search("Elden Ring Margit")
        self.assertEqual(hits[0].score, 0.84)
        self.assertEqual(hits[0].site, "polygon.com")

    def test_video_sites_are_excluded_in_request(self):
        seen = []
        TavilyEngine("key", client=tavily_client([], seen=seen)).search("q")
        self.assertIn("youtube.com", seen[0]["exclude_domains"])

    def test_http_error_is_a_channel_failure(self):
        engine = TavilyEngine("key", client=tavily_client([], status=401))
        with self.assertRaises(SearchUnavailable):
            engine.search("q")

    def test_key_is_required(self):
        with self.assertRaises(ValueError):
            TavilyEngine("")


class DuckDuckGoEngineTests(unittest.TestCase):
    def test_no_results_is_not_a_failure(self):
        class Backend:
            def text(self, query, max_results):
                raise RuntimeError("No results found.")
        self.assertEqual(DuckDuckGoEngine(Backend()).search("q"), [])

    def test_snippets_have_no_score(self):
        class Backend:
            def text(self, query, max_results):
                return [{"title": "T", "href": "https://wiki.example/p", "body": "text"}]
        self.assertIsNone(DuckDuckGoEngine(Backend()).search("q")[0].score)


class WebSearchTests(unittest.TestCase):
    def test_low_relevance_is_dropped(self):
        """Замер: верные страницы — 0,75–0,84, новости про издателя — 0,03–0,05."""
        engine = FakeEngine("tavily", [hit(0.84), hit(0.05, url="https://finance.yahoo.com/n")])
        answer = WebSearch([engine]).search("how to beat Margit", game="Elden Ring")
        self.assertEqual([h.site for h in answer.hits], ["polygon.com"])
        self.assertEqual(answer.rejected, 1)

    def test_only_junk_means_not_found(self):
        answer = WebSearch([FakeEngine("tavily", [hit(0.04)])]).search("q")
        self.assertFalse(answer.found)

    def test_falls_back_when_main_engine_fails(self):
        main = FakeEngine("tavily", error="квота исчерпана")
        backup = FakeEngine("duckduckgo", [hit(None)])
        answer = WebSearch([main, backup]).search("how to beat Margit")
        self.assertEqual(answer.engine, "duckduckgo")
        self.assertTrue(answer.errors)

    def test_falls_back_when_main_finds_only_junk(self):
        main = FakeEngine("tavily", [hit(0.04)])
        backup = FakeEngine("duckduckgo", [hit(None)])
        self.assertEqual(WebSearch([main, backup]).search("Margit").engine, "duckduckgo")

    def test_snippet_without_score_must_mention_the_query(self):
        unrelated = hit(None, content="Stock prices rose today.", title="Market news")
        answer = WebSearch([FakeEngine("duckduckgo", [unrelated])]).search("Margit weakness")
        self.assertFalse(answer.found)

    def test_game_name_is_added_once(self):
        self.assertEqual(WebSearch.compose("how to beat Margit", "Elden Ring"),
                         "Elden Ring how to beat Margit")
        self.assertEqual(WebSearch.compose("Elden Ring Margit", "Elden Ring"), "Elden Ring Margit")

    def test_russian_game_name_is_not_added_to_english_query(self):
        self.assertEqual(WebSearch.compose("Witcher 3 Bloody Baron", "Ведьмака 3"),
                         "Witcher 3 Bloody Baron")

    def test_game_name_without_article_is_not_repeated(self):
        """Живой замер: «The Witcher 3 Witcher 3 Bloody Baron …» — модель писала игру сама."""
        self.assertEqual(WebSearch.compose("Witcher 3 Bloody Baron save Anna", "The Witcher 3"),
                         "Witcher 3 Bloody Baron save Anna")

    def test_repeated_question_comes_from_cache(self):
        engine = FakeEngine("tavily", [hit(0.8)])
        search = WebSearch([engine])
        search.search("Margit")
        second = search.search("Margit")
        self.assertEqual(engine.calls, 1)
        self.assertTrue(second.cached)

    def test_channel_failure_is_not_cached(self):
        """Сбой сети не запоминается: через минуту поиск может заработать."""
        engine = FakeEngine("tavily", error="сеть")
        search = WebSearch([engine])
        search.search("Margit")
        search.search("Margit")
        self.assertEqual(engine.calls, 2)

    def test_cache_expires(self):
        now = [0.0]
        engine = FakeEngine("tavily", [hit(0.8)])
        search = WebSearch([engine], cache_seconds=10, clock=lambda: now[0])
        search.search("Margit")
        now[0] = 11.0
        search.search("Margit")
        self.assertEqual(engine.calls, 2)

    def test_passage_is_trimmed_at_sentence(self):
        text = ("Heal often. " * 200).strip()
        trimmed = clean_passage(text)
        self.assertLessEqual(len(trimmed), MAX_PASSAGE_CHARS)
        self.assertTrue(trimmed.endswith("."))

    def test_sources_name_the_site(self):
        answer = WebSearch([FakeEngine("tavily", [hit(0.8)])]).search("Margit")
        self.assertEqual(answer.sources()[0]["site"], "polygon.com")


if __name__ == "__main__":
    unittest.main()
