"""Страница качества читает отчёты оценочного контура; третья колонка сравнения."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import compare, quality
from eval.schema import Prediction
from orchestrator import llm


def _report(passed, unsafe, total=10):
    return {"summary": {"total": total, "passed": passed, "unsafe": unsafe, "repeat": 3,
                        "by_group": {"typical": {"pass": passed, "total": total}},
                        "latency_ms": {"median": 1040.4}},
            "model": {"модель": "test-model"}}


class QualitySummaryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        for name, data in {
            "2026-11-11-hybrid.json": _report(9, 0),
            "2026-11-11-llm_only.json": _report(6, 3),
            "2026-11-20-hybrid.json": _report(10, 0),
            "2026-11-17-hybrid-qwen4b.json": _report(8, 0),
            "2026-11-17-planner-coverage.json": {"targets": 5, "feasible": 4, "ms_median": 0.1,
                                                 "ms_p95": 2.0, "ms_max": 9.5},
            "2026-11-21-hybrid.json": "broken",
        }.items():
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            (root / name).write_text(text, encoding="utf-8")
        patcher = patch.object(quality, "REPORTS_DIR", root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_comparison_takes_reports_of_the_measurement_day(self):
        rows = quality.summary()["comparison"]
        self.assertEqual([(row["passed"], row["unsafe"]) for row in rows], [(6, 3), (9, 0)])
        self.assertEqual(rows[0]["report"], "2026-11-11-llm_only.json")
        self.assertEqual(rows[1]["accuracy"], 0.9)

    def test_missing_reports_are_skipped_not_invented(self):
        summary = quality.summary()
        self.assertEqual(len(summary["weak_model"]), 1)
        self.assertEqual(summary["extra_sets"], [])

    def test_tagged_report_is_not_taken_for_the_main_one(self):
        # «*-hybrid.json» не должен захватить отчёт с меткой модели.
        latest = quality._latest("*-hybrid.json")
        self.assertNotIn("qwen4b", latest.name)

    def test_unreadable_latest_report_gives_no_numbers(self):
        self.assertIsNone(quality.summary()["latest"])

    def test_planner_block(self):
        planner = quality.summary()["planner"]
        self.assertEqual((planner["feasible"], planner["targets"]), (4, 5))
        self.assertEqual(planner["report"], "2026-11-17-planner-coverage.json")


class JudgeTests(unittest.TestCase):
    def test_answer_without_command_is_never_illegal(self):
        # Ответ по поиску или из базы знаний в игре ничего не меняет.
        prediction = Prediction(outcome="act", intent="search_web")
        self.assertTrue(compare._judge(prediction, "survival")["allowed"])

    def test_command_is_still_checked(self):
        prediction = Prediction(outcome="act", intent="summon_entity",
                                entity_id="minecraft:ender_dragon", count=1,
                                command="/summon minecraft:ender_dragon ~ ~ ~")
        self.assertFalse(compare._judge(prediction, "creative")["allowed"])


class LocalColumnTests(unittest.TestCase):
    def test_offline_local_model_is_not_called(self):
        with patch.object(compare, "_local_available", return_value=False), \
                patch.object(compare, "_run_hybrid") as run:
            self.assertEqual(compare._run_hybrid_local("дай меч", {"mode": "creative",
                                                                    "inventory": {}}),
                             {"offline": True})
            run.assert_not_called()

    def test_model_override_is_visible_only_inside_the_block(self):
        self.assertIsNone(llm._override.get())
        with llm.using_model("qwen3.5-4b-local"):
            self.assertEqual(llm._override.get(), "qwen3.5-4b-local")
        self.assertIsNone(llm._override.get())

    def test_unknown_override_is_a_channel_failure_not_a_decision(self):
        with llm.using_model("no-such-model"):
            with self.assertRaises(llm.ModelUnavailable):
                llm.client()


if __name__ == "__main__":
    unittest.main()
