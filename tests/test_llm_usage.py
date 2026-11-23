"""Учёт токенов и попаданий в кэш провайдера: orchestrator.llm."""

import unittest
from types import SimpleNamespace

from orchestrator import llm


def completion(prompt, completion_tokens, hit=None, cached=None):
    details = SimpleNamespace(cached_tokens=cached) if cached is not None else None
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion_tokens,
                            prompt_tokens_details=details)
    if hit is not None:
        usage.prompt_cache_hit_tokens = hit
    return SimpleNamespace(usage=usage)


class UsageTests(unittest.TestCase):
    def setUp(self):
        llm.reset_usage()

    def tearDown(self):
        llm.reset_usage()

    def test_deepseek_cache_hits_are_counted(self):
        llm._record("json", completion(1150, 30, hit=896))
        llm._record("json", completion(1152, 28, hit=896))
        report = llm.usage_report()
        self.assertEqual(report["json"]["calls"], 2)
        self.assertEqual(report["json"]["cache_hit"], 1792)
        self.assertAlmostEqual(report["json"]["cache_share"], 1792 / 2302, places=3)

    def test_openai_style_cached_tokens_are_counted(self):
        llm._record("text", completion(200, 40, cached=128))
        self.assertEqual(llm.usage_report()["text"]["cache_hit"], 128)

    def test_total_sums_all_purposes(self):
        llm._record("json", completion(1000, 30, hit=800))
        llm._record("text", completion(150, 40, hit=0))
        total = llm.usage_report()["total"]
        self.assertEqual((total["calls"], total["prompt"], total["completion"]), (2, 1150, 70))

    def test_response_without_usage_is_ignored(self):
        llm._record("json", SimpleNamespace())
        self.assertEqual(llm.usage_report(), {})

    def test_reset_clears(self):
        llm._record("json", completion(10, 1, hit=0))
        llm.reset_usage()
        self.assertEqual(llm.usage_report(), {})


if __name__ == "__main__":
    unittest.main()
