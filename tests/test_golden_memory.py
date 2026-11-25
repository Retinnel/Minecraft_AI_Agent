"""Набор реплик о памяти мира: целостность разметки и прохождение конвейера.

Модель подменена заглушкой, которая «понимает» каждую реплику так, как задумано разметкой.
Так проверяется всё, что после модели: валидатор, память, отбор фактов, исход. Точность
самой модели на этом наборе меряет прогон `eval.runner --memory` с ключами провайдера.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from eval.configs import hybrid
from eval.memory_fixture import HOME, fixture_store, with_memory
from eval.runner import classify, run
from eval.schema import MEMORY_INTENTS, Case, load_golden_set
from kb.graph import KnowledgeBase
from orchestrator import Pipeline, Proposal

ROOT = Path(__file__).resolve().parent.parent
MEMORY_SET = ROOT / "eval" / "golden_memory.jsonl"
KB_PATH = ROOT / "data" / "kb" / "game.ttl"

# Как реплику понял бы извлекатель намерения: действие и цель словами игрока.
UNDERSTOOD = {
    "запомни, тут моя ферма": Proposal("remember", "ферма"),
    "где моя база?": Proposal("recall", "база"),
    "где я видел алмазы?": Proposal("recall", "алмазы"),
    "в каком сундуке у меня железо?": Proposal("recall", "железо"),
    "где я умер в последний раз?": Proposal("recall", None),
    "запомни: портал в Незер у деревни": Proposal("remember", "портал в Незер"),
    "забудь отметку шахта": Proposal("forget_fact", "шахта"),
    "запомни": Proposal("remember", None),
    "шахта больше не нужна": Proposal("forget_fact", "шахта"),
    "где я видел древние обломки?": Proposal("recall", "древние обломки"),
}


class Understands:
    def extract(self, utterance, **context):
        return UNDERSTOOD[utterance]


class MemorySetIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_golden_set(MEMORY_SET)

    def test_ten_cases_of_every_group_and_memory_action(self):
        self.assertEqual(len(self.cases), 10)
        self.assertEqual({case.group for case in self.cases}, {"typical", "boundary", "negative"})
        self.assertEqual({case.intent for case in self.cases}, set(MEMORY_INTENTS))

    def test_act_cases_name_the_fact(self):
        for case in self.cases:
            if case.outcome == "act":
                with self.subTest(case=case.id):
                    self.assertTrue(case.subject)

    def test_every_utterance_has_a_reading(self):
        self.assertEqual({case.utterance for case in self.cases}, set(UNDERSTOOD))

    def test_subject_only_for_memory_actions(self):
        with self.assertRaises(ValueError):
            Case(id="x", utterance="дай меч", group="typical", outcome="act",
                 intent="give_item", item_id="minecraft:iron_sword", subject="меч")

    def test_fixture_copies_are_independent(self):
        first = fixture_store()
        first.forget(first.facts()[0].id)
        self.assertEqual(len(fixture_store().facts()), len(first.facts()) + 1)


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class MemorySetPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def test_pipeline_passes_every_case_when_the_model_understands(self):
        cases = load_golden_set(MEMORY_SET)
        with patch.object(hybrid, "_pipeline", Pipeline(Understands(), self.kb)):
            rows, summary = run(with_memory(hybrid.CONFIG), cases)
        failed = {row["id"]: (row["verdict"], row["actual"]) for row in rows if row["verdict"] != "pass"}
        self.assertEqual(failed, {})
        self.assertEqual(sum(1 for row in rows if row["actual"]["command"]), 0)

    def test_wrong_fact_is_a_wrong_target(self):
        case = Case(id="x", utterance="где база", group="typical", outcome="act",
                    intent="recall", subject="база")
        from eval.schema import Prediction
        self.assertEqual(classify(case, Prediction(outcome="act", intent="recall",
                                                   details={"memory_labels": ["шахта"]})),
                         "wrong_target")
        self.assertEqual(classify(case, Prediction(outcome="act", intent="recall",
                                                   details={"memory_labels": ["моя база"]})),
                         "pass")

    def test_only_the_hybrid_reads_memory(self):
        from eval.runner import load_config
        with self.assertRaises(SystemExit):
            with_memory(load_config("symbolic"))

    def test_plans_with_memory_are_never_longer(self):
        from eval.plan_memory import measure, summary
        rows = measure(self.kb)
        total = summary(rows)
        self.assertEqual(total["longer"], 0)
        self.assertGreater(total["shorter"], len(rows) // 2)
        for row in rows:
            with self.subTest(goal=row["goal"]):
                self.assertEqual(row["feasible"], [True, True])
                self.assertLessEqual(row["effort"][1], row["effort"][0])

    def test_home_is_known(self):
        self.assertEqual((HOME.x, HOME.y, HOME.z), (0, 64, 0))


if __name__ == "__main__":
    unittest.main()
