"""Набор реплик «Сделай сам»: целостность разметки и прохождение конвейера.

Модель подменена заглушкой, которая понимает реплику так, как задумано разметкой. Для
граничных реплик заглушка нарочно ошибается в сторону соседнего намерения: конвейер
обязан поправить её по явным словам игрока («сам», «как»). Точность самой модели на
этом наборе меряет прогон `eval.runner --autopilot` с ключами провайдера.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from eval.autopilot_fixture import with_autopilot
from eval.configs import hybrid
from eval.runner import run
from eval.schema import load_golden_set
from kb.graph import KnowledgeBase
from orchestrator import Pipeline, Proposal

ROOT = Path(__file__).resolve().parent.parent
AUTOPILOT_SET = ROOT / "eval" / "golden_autopilot.jsonl"
KB_PATH = ROOT / "data" / "kb" / "game.ttl"

UNDERSTOOD = {
    "сделай сам каменную кирку": Proposal("plan_goal", "каменная кирка"),   # модель спутала
    "скрафти за меня верстак": Proposal("execute_plan", "верстак"),
    "сам сделай печку": Proposal("execute_plan", "печь"),
    "сделай мне сам 4 факела": Proposal("execute_plan", "факел", 4),
    "как сделать каменную кирку": Proposal("execute_plan", "каменная кирка"),  # модель спутала
    "что нужно для печки": Proposal("plan_goal", "печь"),
    "сделай сам": Proposal("execute_plan", None),
    "сделай сам алмазную кирку": Proposal("execute_plan", "алмазная кирка"),
    "сделай сам незеритовый слиток": Proposal("execute_plan", "незеритовый слиток"),
    "добудь за меня кожаную куртку": Proposal("execute_plan", "кожаная куртка"),
}


class Understands:
    def extract(self, utterance, **context):
        return UNDERSTOOD[utterance]


class AutopilotSetIntegrityTests(unittest.TestCase):
    def test_ten_cases_of_every_group(self):
        cases = load_golden_set(AUTOPILOT_SET)
        self.assertEqual(len(cases), 10)
        self.assertEqual({case.group for case in cases}, {"typical", "boundary", "negative"})
        self.assertEqual({case.intent for case in cases}, {"execute_plan", "plan_goal"})
        self.assertEqual({case.utterance for case in cases}, set(UNDERSTOOD))

    def test_only_the_hybrid_runs_it(self):
        from eval.runner import load_config
        with self.assertRaises(SystemExit):
            with_autopilot(load_config("symbolic"))


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class AutopilotSetPipelineTests(unittest.TestCase):
    def test_pipeline_passes_every_case_when_the_model_understands(self):
        kb = KnowledgeBase.load(KB_PATH)
        cases = load_golden_set(AUTOPILOT_SET)
        with patch.object(hybrid, "_pipeline", Pipeline(Understands(), kb)):
            rows, _ = run(with_autopilot(hybrid.CONFIG), cases)
        failed = {row["id"]: (row["verdict"], row["actual"]["outcome"], row["actual"]["intent"])
                  for row in rows if row["verdict"] != "pass"}
        self.assertEqual(failed, {})
        # «Сделай сам» команд не собирает: исполняет мод по задачам после «да».
        self.assertEqual(sum(1 for row in rows if row["actual"]["command"]), 0)


if __name__ == "__main__":
    unittest.main()
