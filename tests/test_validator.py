"""Проверки валидатора действий и проверки заданий.

Закрепляют принятую политику поведения и главное различие, ради которого затевалась база
знаний: «не знаю такого» и «знаю и не дам» — разные ответы игроку, и второй сопровождается
причиной.
"""

import unittest
from pathlib import Path

from kb.graph import KnowledgeBase
from solver.quest import COLLECT, KILL, QuestChecker
from solver.validator import ACT, CLARIFY, CREATIVE, REJECT, SURVIVAL, Validator

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class ValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.validator = Validator(cls.kb)

    # --- обычный ход ---

    def test_plain_request_is_allowed(self):
        verdict = self.validator.validate("give_item", "minecraft:diamond", 5)
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.count, 5)
        self.assertEqual(verdict.game_id, "minecraft:diamond")
        self.assertFalse(verdict.clamped)

    def test_nbt_item_carries_its_exact_game_id(self):
        target = 'minecraft:potion{Potion:"minecraft:healing"}'
        verdict = self.validator.validate("give_item", target, 1)
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.game_id, target)

    # --- запреты ---

    def test_boss_summon_is_vetoed_with_the_reason_from_the_kb(self):
        verdict = self.validator.validate("summon_entity", "minecraft:ender_dragon", 1)
        self.assertEqual(verdict.outcome, REJECT)
        self.assertIn("боссов", verdict.reason)

    def test_service_block_give_is_vetoed(self):
        verdict = self.validator.validate("give_item", "minecraft:command_block", 1)
        self.assertEqual(verdict.outcome, REJECT)
        self.assertIn("служебный", verdict.reason.lower())

    def test_absence_and_prohibition_give_different_answers(self):
        unknown = self.validator.validate("give_item", "minecraft:star_sword", 1)
        forbidden = self.validator.validate("summon_entity", "minecraft:ender_dragon", 1)
        self.assertEqual(unknown.outcome, REJECT)
        self.assertEqual(forbidden.outcome, REJECT)
        self.assertNotEqual(unknown.reason, forbidden.reason)
        self.assertIn("нет", unknown.reason.lower())

    def test_restriction_is_scoped_to_its_action(self):
        # Дракона нельзя призвать, но запрета на выдачу яйца призыва это не создаёт.
        self.assertEqual(
            self.validator.validate("summon_entity", "minecraft:ender_dragon", 1).outcome, REJECT)
        self.assertTrue(
            self.validator.validate("give_item", "minecraft:dragon_egg", 1).allowed)

    # --- род цели ---

    def test_summoning_an_item_is_rejected(self):
        verdict = self.validator.validate("summon_entity", "minecraft:diamond", 1)
        self.assertEqual(verdict.outcome, REJECT)
        self.assertIn("не существо", verdict.reason)

    # --- количество ---

    def test_zero_and_negative_counts_are_rejected_not_silently_fixed(self):
        for count in (0, -5):
            with self.subTest(count=count):
                verdict = self.validator.validate("give_item", "minecraft:diamond", count)
                self.assertEqual(verdict.outcome, REJECT)
                self.assertIn("от 1 до 64", verdict.reason)

    def test_excessive_count_is_clamped_and_flagged(self):
        verdict = self.validator.validate("give_item", "minecraft:diamond", 1000)
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.count, 64)
        self.assertTrue(verdict.clamped, "ответ обязан назвать итоговое число")

    def test_summon_limit_differs_from_item_limit(self):
        verdict = self.validator.validate("summon_entity", "minecraft:zombie", 100)
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.count, 16)

    def test_missing_target_asks_instead_of_refusing(self):
        verdict = self.validator.validate("give_item", "", 1)
        self.assertEqual(verdict.outcome, CLARIFY)

    # --- режимы ---

    def test_survival_mode_has_no_giving_actions(self):
        available = self.validator.available_actions(SURVIVAL)
        self.assertNotIn("give_item", available)
        self.assertNotIn("summon_entity", available)
        self.assertIn("plan_goal", available)
        self.assertIn("create_quest", available)

    def test_survival_mode_vetoes_giving_even_if_proposed(self):
        # Вторая линия защиты: даже если инструмент как-то просочился, действие не пройдёт.
        verdict = self.validator.validate("give_item", "minecraft:diamond", 1, mode=SURVIVAL)
        self.assertEqual(verdict.outcome, REJECT)
        self.assertIn("выживания", verdict.reason)

    def test_creative_mode_allows_what_survival_forbids(self):
        self.assertTrue(
            self.validator.validate("give_item", "minecraft:diamond", 1, mode=CREATIVE).allowed)

    # --- объяснение ---

    def test_every_verdict_carries_an_explanation_chain(self):
        cases = [
            ("give_item", "minecraft:diamond", 5, CREATIVE),
            ("summon_entity", "minecraft:ender_dragon", 1, CREATIVE),
            ("give_item", "minecraft:diamond", 1, SURVIVAL),
            ("give_item", "minecraft:star_sword", 1, CREATIVE),
        ]
        for action, target, count, mode in cases:
            with self.subTest(target=target, mode=mode):
                verdict = self.validator.validate(action, target, count, mode=mode)
                self.assertTrue(verdict.explanation, "решение должно быть объяснимо")

    def test_rejection_always_names_a_reason(self):
        verdict = self.validator.validate("give_item", "minecraft:barrier", 1)
        self.assertEqual(verdict.outcome, REJECT)
        self.assertTrue(verdict.reason.strip())


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class QuestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.checker = QuestChecker(cls.kb)

    def test_achievable_collect_quest_is_accepted(self):
        verdict = self.checker.check(COLLECT, "minecraft:iron_ore", 10,
                                     "minecraft:iron_pickaxe", 1,
                                     inventory={"minecraft:stone_pickaxe": 1})
        self.assertTrue(verdict.accepted)
        self.assertGreater(verdict.effort, 0)
        self.assertIsNotNone(verdict.plan)

    def test_kill_quest_is_accepted(self):
        verdict = self.checker.check(KILL, "minecraft:zombie", 5, "minecraft:iron_ingot", 3)
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.effort, 5)

    def test_unobtainable_goal_is_rejected_before_the_player_sees_it(self):
        verdict = self.checker.check(COLLECT, "minecraft:bedrock", 1, "minecraft:diamond", 1)
        self.assertEqual(verdict.outcome, REJECT)
        self.assertIsNotNone(verdict.reason)

    def test_forbidden_reward_rejects_the_quest(self):
        verdict = self.checker.check(KILL, "minecraft:zombie", 3, "minecraft:command_block", 1)
        self.assertEqual(verdict.outcome, REJECT)
        self.assertIn("Награда", verdict.reason)

    def test_boss_kill_quest_is_rejected(self):
        verdict = self.checker.check(KILL, "minecraft:ender_dragon", 1, "minecraft:diamond", 5)
        self.assertEqual(verdict.outcome, REJECT)
        self.assertIn("опасно", verdict.reason)

    def test_disproportionate_quest_is_rejected(self):
        verdict = self.checker.check(KILL, "minecraft:zombie", 500, "minecraft:diamond", 1)
        self.assertEqual(verdict.outcome, REJECT)
        self.assertIn("тяжёлое", verdict.reason)

    def test_nonexistent_goal_is_rejected(self):
        verdict = self.checker.check(COLLECT, "minecraft:star_sword", 1, "minecraft:diamond", 1)
        self.assertEqual(verdict.outcome, REJECT)

    def test_collecting_a_creature_is_rejected(self):
        verdict = self.checker.check(COLLECT, "minecraft:zombie", 3, "minecraft:diamond", 1)
        self.assertEqual(verdict.outcome, REJECT)

    def test_verdict_is_explainable(self):
        verdict = self.checker.check(KILL, "minecraft:zombie", 5, "minecraft:iron_ingot", 3)
        self.assertTrue(verdict.explanation)
        self.assertTrue(any("соразмерность" in line for line in verdict.explanation))


if __name__ == "__main__":
    unittest.main()
