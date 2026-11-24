"""Поисковый движок DeepSeek и порядок движков.

В сеть тесты не ходят: ответ API подменяется транспортом httpx. Проверяется то, что
отличает этот движок от остальных: своя шкала оценок и свой порог, язык выдачи, не больше
одной страницы с сайта, предел длины запроса.
"""

import json
import os
import unittest
from unittest.mock import patch

import httpx

from api import keys
from search.engines import (DeepSeekEngine, DuckDuckGoEngine, Hit, SearchUnavailable,
                            TavilyEngine, default_engines)
from search.service import WebSearch


def client(results, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append({"body": json.loads(request.content),
                         "auth": request.headers.get("authorization"), "url": str(request.url)})
        return httpx.Response(status, json={"results": results})
    return httpx.Client(transport=httpx.MockTransport(handler))


def result(score, url="https://www.polygon.com/margit", summary="Margit is weak to bleed.",
           snippet="short", title="How to beat Margit"):
    return {"rerank_score": score, "url": url, "summary": summary, "snippet": snippet,
            "title": title, "authority": 3}


class DeepSeekEngineTests(unittest.TestCase):
    def test_results_are_parsed_and_the_score_is_brought_to_the_common_scale(self):
        seen = []
        hits = DeepSeekEngine("key", client=client([result(1.92)], seen=seen)).search("Margit")
        self.assertEqual(hits[0].site, "polygon.com")
        self.assertEqual(hits[0].content, "Margit is weak to bleed.")     # выжимка, не сниппет
        self.assertAlmostEqual(hits[0].score, 0.64)
        self.assertEqual(hits[0].engine, "deepseek")
        self.assertEqual(seen[0]["auth"], "Bearer key")
        self.assertTrue(seen[0]["url"].endswith("/v1/tools/search"))

    def test_snippet_is_used_when_there_is_no_summary(self):
        hits = DeepSeekEngine("key", client=client([result(1.9, summary="")])).search("q")
        self.assertEqual(hits[0].content, "short")

    def test_english_query_asks_for_english_pages(self):
        self.assertEqual(DeepSeekEngine.compose("Elden Ring  how to beat Margit"),
                         "Elden Ring how to beat Margit lang:en")
        # Запрос кириллицей и запрос с явным языком не трогаются.
        self.assertEqual(DeepSeekEngine.compose("как победить визера"), "как победить визера")
        self.assertEqual(DeepSeekEngine.compose("wither lang:ru"), "wither lang:ru")

    def test_query_never_exceeds_the_api_limit(self):
        composed = DeepSeekEngine.compose("word " * 60)
        self.assertLessEqual(len(composed), DeepSeekEngine.MAX_QUERY)
        self.assertTrue(composed.endswith(" lang:en"))
        self.assertNotIn("  ", composed)

    def test_one_page_per_site_and_no_video_sites(self):
        results = [result(1.9, url="https://www.polygon.com/a"), result(1.9, url="https://www.polygon.com/b"),
                   result(1.8, url="https://www.youtube.com/watch?v=1"),
                   result(1.8, url="https://www.ncesc.com/answer"),       # площадка-агрегатор
                   result(1.7, url="https://gamerant.com/c")]
        hits = DeepSeekEngine("key", client=client(results)).search("q")
        self.assertEqual([hit.site for hit in hits], ["polygon.com", "gamerant.com"])

    def test_http_error_is_a_channel_failure(self):
        for status in (401, 402, 429, 500):
            with self.subTest(status=status), self.assertRaises(SearchUnavailable):
                DeepSeekEngine("key", client=client([], status=status)).search("q")

    def test_key_is_required(self):
        with self.assertRaises(ValueError):
            DeepSeekEngine("")


class EngineThresholdTests(unittest.TestCase):
    """У каждого движка своя шкала оценок: общий порог годился бы только одному."""

    def engine(self, scores):
        return DeepSeekEngine("key", client=client(
            [result(score, url=f"https://site{index}.com/x") for index, score in enumerate(scores)]))

    def test_engine_threshold_drops_what_the_common_one_would_keep(self):
        # 0,86 из 3 — запрос о выдуманном существе на замере; по общему порогу 0,3 от
        # единицы такая страница почти проходила бы (0,29), по порогу движка — нет.
        answer = WebSearch([self.engine([0.86, 0.72])]).search("pink unicorn")
        self.assertFalse(answer.found)
        self.assertEqual(answer.rejected, 2)

    def test_pages_above_the_engine_threshold_are_kept(self):
        answer = WebSearch([self.engine([1.92, 1.6, 1.2])]).search("Margit")
        self.assertEqual(len(answer.hits), 2)
        self.assertEqual(answer.engine, "deepseek")

    def test_engine_without_its_own_threshold_uses_the_common_one(self):
        class Plain:
            name = "plain"

            def search(self, query, *, max_results=5):
                return [Hit("t", "https://a.com/x", "Margit guide", 0.35, "plain"),
                        Hit("t", "https://b.com/x", "Margit guide", 0.1, "plain")]

        self.assertEqual(len(WebSearch([Plain()]).search("Margit").hits), 1)

    def test_next_engine_is_asked_when_this_one_finds_only_junk(self):
        class Backup:
            name = "backup"

            def search(self, query, *, max_results=5):
                return [Hit("t", "https://minecraft.wiki/x", "unicorn mod", None, "backup")]

        answer = WebSearch([self.engine([0.8]), Backup()]).search("unicorn")
        self.assertEqual(answer.engine, "backup")


class EngineOrderTests(unittest.TestCase):
    def available(self, *, env=None, **stored):
        with patch.object(keys, "_stored", return_value=stored), \
                patch.dict(os.environ, env or {}, clear=True):
            return [engine.name for engine in default_engines()]

    def test_default_order_and_missing_keys_are_skipped(self):
        names = self.available(TAVILY_API_KEY="t", DEEPSEEK_API="d")
        self.assertEqual(names[:2], ["tavily", "deepseek"])
        self.assertEqual(self.available(DEEPSEEK_API="d")[0], "deepseek")
        self.assertNotIn("tavily", self.available())
        self.assertNotIn("deepseek", self.available())

    def test_order_comes_from_the_environment(self):
        names = self.available(env={"SEARCH_ENGINES": "deepseek, tavily"},
                               TAVILY_API_KEY="t", DEEPSEEK_API="d")
        self.assertEqual(names, ["deepseek", "tavily"])

    def test_unknown_and_repeated_names_are_ignored(self):
        names = self.available(env={"SEARCH_ENGINES": "tavily,bing,tavily"}, TAVILY_API_KEY="t")
        self.assertEqual(names, ["tavily"])

    def test_explicit_order_builds_only_what_is_named(self):
        with patch.object(keys, "_stored", return_value={"TAVILY_API_KEY": "t", "DEEPSEEK_API": "d"}):
            engines = default_engines(["deepseek"])
        self.assertEqual([type(engine) for engine in engines], [DeepSeekEngine])
        self.assertNotIsInstance(engines[0], (TavilyEngine, DuckDuckGoEngine))


if __name__ == "__main__":
    unittest.main()
