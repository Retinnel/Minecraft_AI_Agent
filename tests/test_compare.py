"""Страница сравнения отвечает в режиме текущей сессии.

Без этого переключатель режима в шапке ничего бы не менял на странице: гибрид отвечал бы
в творческом режиме, а выдача в выживании считалась бы допустимой.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from api import compare
from eval.configs import hybrid
from eval.schema import Prediction
from kb.graph import KnowledgeBase
from orchestrator import Pipeline, Proposal
from orchestrator.session import Session
from solver.validator import SURVIVAL

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


class StubIntent:
    def __init__(self, proposal):
        self.proposal = proposal

    def extract(self, utterance, **context):
        return self.proposal


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class CompareModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def context(self, mode):
        session = Session()
        session.set_mode(mode)
        with patch("api.advice._assistant_session", return_value=session), \
             patch("api.advice._inventory_counts", return_value={}):
            return compare._session_context()

    def test_hybrid_refuses_giving_in_survival(self):
        pipeline = Pipeline(StubIntent(Proposal("give_item", "алмазная кирка", 1)), self.kb)
        context = self.context(SURVIVAL)
        with patch.object(hybrid, "_get_pipeline", return_value=pipeline):
            result = compare._run_hybrid("дай алмазную кирку", context)
        self.assertEqual(result["outcome"], "reject")
        self.assertIsNone(result["command"])
        self.assertTrue(result["verdict"]["allowed"])

    def test_hybrid_gives_in_creative(self):
        pipeline = Pipeline(StubIntent(Proposal("give_item", "алмазная кирка", 1)), self.kb)
        context = self.context("creative")
        with patch.object(hybrid, "_get_pipeline", return_value=pipeline):
            result = compare._run_hybrid("дай алмазную кирку", context)
        self.assertEqual(result["outcome"], "act")
        self.assertEqual(result["command"], "/give @s minecraft:diamond_pickaxe 1")

    def test_llm_only_giving_is_judged_by_the_session_mode(self):
        prediction = Prediction(outcome="act", intent="give_item",
                                item_id="minecraft:diamond_pickaxe", count=1,
                                command="/give @s minecraft:diamond_pickaxe 1", latency_ms=1.0)
        self.assertTrue(compare._judge(prediction, "creative")["allowed"])
        self.assertFalse(compare._judge(prediction, SURVIVAL)["allowed"])


if __name__ == "__main__":
    unittest.main()
