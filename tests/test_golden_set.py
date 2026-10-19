"""Проверки целостности золотого набора.

Следят за тем, чтобы разметка оставалась консистентной: ссылки на предметы и существа
указывают на реальные записи каталога, идентификаторы уникальны, поля заполнены по схеме.
Сам прогон метрик тестом не является — он запускается через eval.runner.
"""

import json
import unittest
from pathlib import Path

from api.entity_catalog import load_entity_catalog
from api.minecraft_catalog import load_catalog
from eval.schema import load_golden_set

GOLDEN_SET_PATH = Path(__file__).resolve().parent.parent / "eval" / "golden_set.jsonl"


class GoldenSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = load_golden_set(GOLDEN_SET_PATH)
        cls.item_ids = {entry["id"] for entry in load_catalog()}
        entities = load_entity_catalog()
        if isinstance(entities, dict):
            entities = entities.get("entities", [])
        cls.entity_ids = {entry["id"] for entry in entities}

    def test_set_is_not_empty(self):
        self.assertGreater(len(self.cases), 0)

    def test_ids_are_unique(self):
        ids = [case.id for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_group_is_represented(self):
        groups = {case.group for case in self.cases}
        self.assertSetEqual(groups, {"typical", "boundary", "negative"})

    def test_expected_items_exist_in_catalog(self):
        for case in self.cases:
            if case.item_id is None:
                continue
            with self.subTest(case=case.id):
                self.assertIn(case.item_id, self.item_ids)

    def test_expected_entities_exist_in_catalog(self):
        for case in self.cases:
            if case.entity_id is None:
                continue
            with self.subTest(case=case.id):
                self.assertIn(case.entity_id, self.entity_ids)

    def test_act_cases_name_a_target(self):
        for case in self.cases:
            if case.outcome != "act":
                continue
            with self.subTest(case=case.id):
                self.assertIsNotNone(
                    case.item_id or case.entity_id,
                    "для outcome=act должен быть указан item_id или entity_id",
                )

    def test_non_act_cases_do_not_name_a_target(self):
        for case in self.cases:
            if case.outcome == "act":
                continue
            with self.subTest(case=case.id):
                self.assertIsNone(case.item_id)
                self.assertIsNone(case.entity_id)

    def test_utterances_are_unique(self):
        utterances = [case.utterance.strip().lower() for case in self.cases]
        self.assertEqual(len(utterances), len(set(utterances)))

    def test_policy_cases_carry_a_note(self):
        for case in self.cases:
            if not case.policy:
                continue
            with self.subTest(case=case.id):
                self.assertTrue(case.note.strip(), "спорная разметка должна быть пояснена")

    def test_file_is_valid_jsonl(self):
        for line_no, raw in enumerate(GOLDEN_SET_PATH.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            with self.subTest(line=line_no):
                json.loads(line)


if __name__ == "__main__":
    unittest.main()


class RepeatedRunTests(unittest.TestCase):
    """Свёртка повторных прогонов: медиана, разброс и нестабильные реплики."""

    @staticmethod
    def fake_run(verdicts):
        from eval.runner import UNSAFE
        rows = [{"id": f"c{index}", "group": "typical", "verdict": verdict, "latency_ms": 10.0}
                for index, verdict in enumerate(verdicts)]
        passed = sum(1 for verdict in verdicts if verdict == "pass")
        summary = {
            "config": "x", "title": "X", "description": "", "timestamp": "t",
            "total": len(verdicts), "failed": 0, "passed": passed,
            "unsafe": sum(1 for verdict in verdicts if verdict in UNSAFE),
            "verdicts": {verdict: verdicts.count(verdict) for verdict in set(verdicts)},
            "by_group": {"typical": {"total": len(verdicts), "pass": passed}},
            "latency_ms": {"median": 10.0, "max": 10.0},
        }
        return rows, summary

    def test_median_and_spread(self):
        from eval.runner import aggregate
        runs = [self.fake_run(["pass", "pass", "wrong_target"]),
                self.fake_run(["pass", "pass", "pass"]),
                self.fake_run(["pass", "intent_missed", "pass"])]
        summary = aggregate(runs)
        self.assertEqual(summary["repeat"], 3)
        self.assertEqual(summary["passed"], 2)
        self.assertEqual(summary["passed_range"], [2, 3])
        self.assertEqual(summary["unsafe_range"], [0, 1])

    def test_unstable_cases_are_listed(self):
        from eval.runner import aggregate
        runs = [self.fake_run(["pass", "wrong_target"]), self.fake_run(["pass", "pass"])]
        summary = aggregate(runs)
        self.assertEqual(set(summary["unstable"]), {"c1"})
