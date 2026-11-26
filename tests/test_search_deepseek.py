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
from search.engines import (ACCEPTABLE, GOOD, DeepSeekEngine, DuckDuckGoEngine, Hit,
                            SearchUnavailable, TavilyEngine, default_engines, normalize)
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
        self.assertAlmostEqual(hits[0].score, normalize(1.92, *DeepSeekEngine.SCALE))
        self.assertAlmostEqual(hits[0].score, 0.825)
        self.assertEqual(hits[0].raw_score, 1.92)                         # исходная оценка — для журнала
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


class NormalizationTests(unittest.TestCase):
    """Оценки движков приводятся к общей шкале, и порог на ней один."""

    def test_both_anchor_points_of_an_engine_land_on_the_common_ones(self):
        for engine in (TavilyEngine, DeepSeekEngine):
            acceptable, good = engine.SCALE
            with self.subTest(engine=engine.name):
                self.assertAlmostEqual(normalize(acceptable, *engine.SCALE), ACCEPTABLE)
                self.assertAlmostEqual(normalize(good, *engine.SCALE), GOOD)

    def test_values_stay_within_the_scale(self):
        scale = DeepSeekEngine.SCALE
        self.assertEqual(normalize(0.0, *scale), 0.0)
        self.assertEqual(normalize(-1.0, *scale), 0.0)
        self.assertEqual(normalize(3.0, *scale), 1.0)        # верх шкалы движка
        self.assertEqual(normalize(7.0, *scale), 1.0)

    def test_order_is_preserved(self):
        raws = [0.2, 0.86, 1.46, 1.5, 1.6, 1.75, 1.92, 2.9]
        scores = [normalize(raw, *DeepSeekEngine.SCALE) for raw in raws]
        self.assertEqual(scores, sorted(scores))
        self.assertLess(normalize(1.46, *DeepSeekEngine.SCALE), ACCEPTABLE)   # чуть ниже границы
        self.assertGreater(normalize(1.6, *DeepSeekEngine.SCALE), ACCEPTABLE)

    def test_good_pages_of_both_engines_get_similar_scores(self):
        """Ради этого и нужна вторая опорная точка: одинаковые числа значат одно и то же."""
        tavily = [normalize(raw, *TavilyEngine.SCALE) for raw in (0.75, 0.80, 0.85)]
        deepseek = [normalize(raw, *DeepSeekEngine.SCALE) for raw in (1.85, 1.90, 1.95)]
        for ours, theirs in zip(deepseek, tavily):
            self.assertAlmostEqual(ours, theirs, delta=0.1)
        # Мусор у обоих остаётся далеко под границей.
        self.assertLess(normalize(0.05, *TavilyEngine.SCALE), 0.1)
        self.assertLess(normalize(0.86, *DeepSeekEngine.SCALE), 0.2)

    def test_tavily_scale_already_matches_the_common_one(self):
        for raw in (0.04, 0.3, 0.55, 0.84, 1.0):
            self.assertAlmostEqual(normalize(raw, *TavilyEngine.SCALE), raw)

    def test_tavily_hits_carry_both_scores(self):
        def handler(request):
            return httpx.Response(200, json={"results": [
                {"title": "t", "url": "https://minecraft.wiki/w", "content": "c", "score": 0.84}]})
        hits = TavilyEngine("key", client=httpx.Client(
            transport=httpx.MockTransport(handler))).search("q")
        self.assertAlmostEqual(hits[0].score, 0.84)
        self.assertEqual(hits[0].raw_score, 0.84)


class EngineThresholdTests(unittest.TestCase):
    """Порог один, а отсекает он у каждого движка то, что этот движок считает слабым."""

    def engine(self, scores):
        return DeepSeekEngine("key", client=client(
            [result(score, url=f"https://site{index}.com/x") for index, score in enumerate(scores)]))

    def test_weak_pages_of_this_engine_are_dropped(self):
        # 0,86 из 3 — запрос о выдуманном существе на замере. Простое деление на три дало
        # бы 0,29 — у самого порога; пересчёт от границы движка даёт 0,17.
        answer = WebSearch([self.engine([0.86, 0.72])]).search("pink unicorn")
        self.assertFalse(answer.found)
        self.assertEqual(answer.rejected, 2)

    def test_pages_above_the_engine_threshold_are_kept(self):
        answer = WebSearch([self.engine([1.92, 1.6, 1.2])]).search("Margit")
        self.assertEqual(len(answer.hits), 2)
        self.assertEqual(answer.engine, "deepseek")

    def test_engine_with_scores_on_the_common_scale_needs_no_scale_of_its_own(self):
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


class NicheGameSearchTests(unittest.TestCase):
    """Малоизвестная игра: вики на сайте с её именем, склеенное название, вторая попытка."""

    def _engine(self, answers):
        from search.engines import Hit

        class Engine:
            name = "stub"

            def __init__(self):
                self.queries = []

            def search(self, query, max_results=5):
                self.queries.append(query)
                return [Hit(title=title, url=f"https://{site}/page", content="text", score=score,
                            engine="stub") for title, site, score in answers(query)]

        return Engine()

    def test_window_title_is_searched_as_words(self):
        from search.service import WebSearch, spaced
        self.assertEqual(spaced("CasualtiesUnknown"), "Casualties Unknown")
        self.assertEqual(spaced("Elden Ring"), "Elden Ring")
        self.assertEqual(WebSearch.compose("CasualtiesUnknown bandage", "CasualtiesUnknown"),
                         "Casualties Unknown bandage")
        # Название с двоеточием и без — одно и то же: второй раз не добавляется.
        self.assertEqual(WebSearch.compose("Casualties Unknown bandage", "Casualties: Unknown"),
                         "Casualties Unknown bandage")

    def test_page_on_the_games_own_site_is_accepted_below_the_threshold(self):
        from search.service import WebSearch
        engine = self._engine(lambda query: [("Wiki", "casualtiesunknown.miraheze.org", 0.1),
                                             ("News", "example.com", 0.1)])
        answer = WebSearch([engine]).search("bandage", game="Casualties: Unknown")
        self.assertEqual([hit.site for hit in answer.hits], ["casualtiesunknown.miraheze.org"])

    def test_short_game_name_gives_no_site_bonus(self):
        from search.engines import Hit
        from search.service import own_site
        hit = Hit(title="t", url="https://gta.example.com/x", content="c", score=0.1, engine="s")
        self.assertFalse(own_site(hit, "GTA"))

    def test_second_attempt_asks_for_the_wiki_only_after_a_miss(self):
        from search.service import WebSearch
        engine = self._engine(lambda query: [("Guide", "guides.example.com", 0.9)]
                              if " wiki " in query else [("News", "example.com", 0.1)])
        answer = WebSearch([engine]).search("strange item", game="Some Game")
        self.assertEqual(engine.queries, ["Some Game strange item", "Some Game wiki strange item"])
        self.assertEqual(answer.query, "Some Game wiki strange item")

        hit_first = self._engine(lambda query: [("Guide", "guides.example.com", 0.9)])
        WebSearch([hit_first]).search("strange item", game="Some Game")
        self.assertEqual(len(hit_first.queries), 1)
