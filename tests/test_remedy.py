"""Отказ называет условие: при чём просьба станет выполнимой или что можно взамен."""

import unittest

from kb.graph import KnowledgeBase
from orchestrator.pipeline import Pipeline, Turn
from orchestrator.responder import Responder
from solver.validator import REJECT, Validator


class ValidatorRemedyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = Validator(KnowledgeBase.shared())

    def test_mode_refusal_names_the_mode_where_it_is_allowed(self):
        verdict = self.validator.validate("give_item", "minecraft:diamond_pickaxe", 1, "survival")
        self.assertEqual(verdict.outcome, REJECT)
        self.assertEqual(verdict.cause, "mode")
        self.assertIn("творческий режим", verdict.remedy)

    def test_restriction_does_not_depend_on_mode_and_offers_a_question(self):
        verdict = self.validator.validate("summon_entity", "minecraft:ender_dragon", 1, "creative")
        self.assertEqual(verdict.cause, "restriction")
        self.assertIn("не зависит от режима", verdict.remedy)
        self.assertIn("что из него выпадает", verdict.remedy)
        # Запрещённое действие взамен себя не предлагается.
        self.assertNotIn("призови", verdict.remedy)

    def test_wrong_kind_offers_the_action_that_fits(self):
        creature = self.validator.validate("give_item", "minecraft:zombie", 1, "creative")
        self.assertEqual(creature.cause, "kind")
        self.assertIn("призови", creature.remedy)
        item = self.validator.validate("summon_entity", "minecraft:diamond", 1, "creative")
        self.assertIn("дай", item.remedy)
        self.assertNotIn("выпадает", item.remedy)

    def test_substitute_is_checked_by_the_same_rules(self):
        # Командный блок выдавать нельзя — и в замену призыву он не предлагается.
        verdict = self.validator.validate("summon_entity", "minecraft:command_block", 1, "creative")
        self.assertNotIn("дай", verdict.remedy or "")

    def test_allowed_action_has_no_remedy(self):
        verdict = self.validator.validate("give_item", "minecraft:diamond", 5, "creative")
        self.assertIsNone(verdict.remedy)
        self.assertIsNone(verdict.cause)

    def test_condition_is_in_the_trace(self):
        verdict = self.validator.validate("summon_entity", "minecraft:wither", 1, "creative")
        self.assertTrue(any(line.startswith("условие:") for line in verdict.explanation))


class PipelineRemedyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = Pipeline(object())

    def test_survival_give_reports_the_distance_by_plan(self):
        stages = {}
        turn = self.pipeline._decide("give_item", "minecraft:wooden_pickaxe", 1, "survival",
                                     {"minecraft:oak_log": 3}, stages)
        self.assertEqual(turn.outcome, REJECT)
        self.assertIn("шагов —", turn.remedy)
        self.assertIn("творческий режим", turn.remedy)
        self.assertTrue(stages["remedy"]["feasible"])
        self.assertGreater(stages["remedy"]["steps"], 0)

    def test_creative_refusal_keeps_validator_remedy(self):
        turn = self.pipeline._decide("summon_entity", "minecraft:ender_dragon", 1, "creative",
                                     None, {})
        self.assertIn("не зависит от режима", turn.remedy)


class ResponderRemedyTests(unittest.TestCase):
    def test_condition_is_appended_verbatim_after_model_text(self):
        responder = Responder(ask_text=lambda messages: "Боссов призывать нельзя.")
        turn = Turn(outcome=REJECT, action="summon_entity", reason="Призыв боссов запрещён.",
                    remedy="Это правило не зависит от режима и количества.")
        reply = responder.reply(turn, "призови дракона")
        self.assertTrue(reply.text.endswith("Это правило не зависит от режима и количества."))

    def test_refusal_without_condition_is_unchanged(self):
        responder = Responder(ask_text=lambda messages: "")
        turn = Turn(outcome=REJECT, action="give_item", reason="Количество должно быть от 1 до 64.")
        self.assertEqual(responder.reply(turn, "дай 0 алмазов").text,
                         "Количество должно быть от 1 до 64.")


if __name__ == "__main__":
    unittest.main()


class OwnTargetTests(unittest.TestCase):
    """Цель реплики называет игрок: из предыдущего обмена она берётся, только если своей нет."""

    CONTEXT = "игрок: «пусть помощник печь сделает»; итог: Помощник начинает: Печь."

    class Extractor:
        def __init__(self, with_context, alone):
            self.with_context, self.alone, self.calls = with_context, alone, []

        def extract(self, utterance, **kwargs):
            from orchestrator.pipeline import Proposal
            self.calls.append(kwargs)
            action, target = self.with_context if kwargs.get("context") else self.alone
            return Proposal(action=action, target=target)

    def _handle(self, extractor, utterance):
        return Pipeline(extractor).handle(utterance, mode="survival", inventory={},
                                          context=self.CONTEXT)

    def test_target_borrowed_from_context_is_replaced_by_the_named_one(self):
        extractor = self.Extractor(("plan_goal", "Печь"), ("plan_goal", None))
        turn = self._handle(extractor, "Каменную кирку пусть сделает тоже")
        self.assertEqual(turn.target, "minecraft:stone_pickaxe")
        self.assertEqual(turn.stages["context_check"]["borrowed"], "Печь")
        # Решает словарь онтологии: модель второй раз не спрашивается.
        self.assertEqual(len(extractor.calls), 1)

    def test_generic_word_gives_a_question_not_the_previous_target(self):
        extractor = self.Extractor(("plan_goal", "Печь"), ("plan_goal", None))
        turn = self._handle(extractor, "Пусть кирку сделает тоже ещё одну.")
        self.assertEqual(turn.outcome, "clarify")
        self.assertIn("Каменная кирка", [option.label for option in turn.options])

    def test_true_continuation_keeps_the_target_from_context(self):
        extractor = self.Extractor(("plan_goal", "Печь"), ("plan_goal", None))
        turn = self._handle(extractor, "и ещё одну")
        self.assertEqual(turn.target, "minecraft:furnace")
        self.assertEqual(turn.stages["context_check"]["named"], [])

    def test_target_named_in_the_utterance_is_left_alone(self):
        extractor = self.Extractor(("plan_goal", "верстак"), ("plan_goal", "верстак"))
        turn = self._handle(extractor, "а теперь верстак")
        self.assertEqual(turn.target, "minecraft:crafting_table")
        self.assertNotIn("context_check", turn.stages)


class OwnedGoalWordingTests(unittest.TestCase):
    def test_no_detailed_plan_phrase_when_the_goal_is_already_owned(self):
        pipeline = Pipeline(object())
        owned = pipeline._decide("plan_goal", "minecraft:stone_pickaxe", 1, "survival",
                                 {"minecraft:stone_pickaxe": 1}, {})
        self.assertNotIn("отдельном экране", Responder.facts(owned))
        needed = pipeline._decide("plan_goal", "minecraft:stone_pickaxe", 1, "survival", {}, {})
        self.assertIn("отдельном экране", Responder.facts(needed))
