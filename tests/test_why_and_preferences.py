"""«Почему?» по следу правил и профиль игрока как вход решателя."""

import unittest

from api import profile
from kb.graph import KnowledgeBase
from orchestrator import why
from orchestrator.pipeline import Pipeline, Proposal, Turn
from orchestrator.session import Session
from solver.campaign import CampaignBuilder, UNLOCKS_GEAR, UNLOCKS_TOOL

START = {"minecraft:wooden_pickaxe": 1, "minecraft:crafting_table": 1}


class WhyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = Pipeline(object())

    def test_command_is_recognised_without_the_model(self):
        for text in ("почему?", "а почему нельзя", "Почему так?", "почему", "почему план такой?"):
            self.assertTrue(why.is_why(text), text)
        for text in ("почему небо синее", "почему нельзя дракона", "как сделать печь"):
            self.assertFalse(why.is_why(text), text)

    def test_refusal_is_explained_by_the_rule_chain_and_the_condition(self):
        turn = self.pipeline._decide("summon_entity", "minecraft:ender_dragon", 1, "creative", {}, {})
        text = why.render(why.snapshot(turn))
        self.assertIn("запрещён", text)
        self.assertIn("правило запрета (boss)", text)
        self.assertIn("не зависит от режима", text)
        # Условие названо один раз — отдельной фразой, а не строкой следа.
        self.assertEqual(text.count("не зависит от режима"), 1)

    def test_plan_is_explained_by_rejected_alternatives(self):
        turn = self.pipeline._decide("plan_goal", "minecraft:iron_pickaxe", 1, "survival", {}, {})
        text = why.render(why.snapshot(turn))
        self.assertIn("Почему план именно такой", text)
        self.assertIn("отвергнуто", text)

    def test_nothing_to_explain(self):
        self.assertIsNone(why.snapshot(Turn(outcome="none")))
        self.assertIn("объяснять нечего", why.render(None))

    def test_session_keeps_the_trace_across_restart(self):
        session = Session()
        turn = self.pipeline._decide("give_item", "minecraft:diamond", 0, "creative", {}, {})
        session.record("дай 0 алмазов", turn, "Количество должно быть от 1 до 64.")
        restored = Session.from_dict(session.to_dict())
        self.assertEqual(restored.last_why["outcome"], "reject")
        self.assertTrue(restored.last_why["trace"])
        # Болтовня след не стирает: «почему?» после неё относится к последнему отказу.
        session.record("привет", Turn(outcome="none"), "Привет!")
        self.assertEqual(session.last_why["outcome"], "reject")

    def test_question_about_giving_is_answered_by_the_validator(self):
        for text in ("А выдать можешь?", "Почему не можешь выдать?", "почему не даёшь"):
            self.assertTrue(why.is_give_question(text), text)
        for text in ("можешь сделать кирку?", "дай кирку", "почему?"):
            self.assertFalse(why.is_give_question(text), text)
        validator = self.pipeline.validator
        survival = why.render_give(validator.validate("give_item", None, 1, "survival"))
        self.assertIn("творческий режим", survival)
        creative = why.render_give(validator.validate("give_item", None, 1, "creative"))
        self.assertIn("выдаю", creative)


class PlanInsteadOfGiveTests(unittest.TestCase):
    """«Дай» в выживании даёт план — и ответ обязан сказать, что это не выдача."""

    class Extractor:
        def extract(self, utterance, **kwargs):
            return Proposal(action="plan_goal", target="алмазная кирка")

    def test_substitution_is_named_with_the_condition(self):
        from orchestrator.responder import Responder
        turn = Pipeline(self.Extractor()).handle("дай алмазную кирку", mode="survival", inventory={})
        self.assertTrue(turn.stages["give_as_plan"])
        text = Responder(ask_text=lambda messages: "План готов.").reply(turn, "дай кирку").text
        self.assertIn("вместо неё план", text)
        self.assertIn("творческий режим", text)

    def test_question_how_to_make_gets_no_such_note(self):
        turn = Pipeline(self.Extractor()).handle("как сделать алмазную кирку", mode="survival",
                                                 inventory={})
        self.assertIsNone(turn.remedy)

    def test_creative_plan_gets_no_note_either(self):
        turn = Pipeline(self.Extractor()).handle("дай алмазную кирку", mode="creative", inventory={})
        self.assertIsNone(turn.remedy)


class PreferenceTests(unittest.TestCase):
    def _state(self, category=None, times=0, threats=None):
        state = profile._empty()
        for _ in range(times):
            profile.observe(state, "Minecraft", "plan_goal", "act", category)
        if threats:
            state["threats"] = dict(threats)
            state["deaths"] = sum(threats.values())
        return state

    def test_no_preferences_without_evidence(self):
        self.assertEqual(profile.preferences(self._state(profile.WEAPONS, 2)), {})

    def test_interest_and_deadly_threat_with_basis(self):
        found = profile.preferences(self._state(profile.WEAPONS, 6, {"взрыв": 3, "падение": 1}))
        self.assertEqual(found["interest"], "weapons")
        self.assertEqual(found["deadly"], "взрыв")
        self.assertEqual(found["deadly_basis"], "3 из 4 смертей")


class GoalOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = CampaignBuilder(KnowledgeBase.shared())

    def goals(self, interest=None):
        return self.builder.propose_goals(START, "survival", interest=interest)

    def test_without_interest_the_order_is_as_before(self):
        plain = self.goals()
        self.assertEqual([c.item for c in plain],
                         [c.item for c in self.builder.propose_goals(START, "survival")])
        self.assertFalse(any(c.kind == UNLOCKS_GEAR for c in plain))

    def test_weapons_interest_puts_a_sword_first(self):
        goals = self.goals("weapons")
        self.assertEqual(goals[0].item, "minecraft:wooden_sword")
        # Достижимые цели прежние: интерес добавляет и переставляет, но не убирает.
        self.assertLessEqual({c.item for c in self.goals()}, {c.item for c in goals})

    def test_next_sword_tier_follows_the_owned_one(self):
        goals = self.builder.propose_goals({**START, "minecraft:wooden_sword": 1}, "survival",
                                           interest="weapons")
        self.assertEqual(goals[0].item, "minecraft:stone_sword")

    def test_tools_interest_puts_tools_before_stations(self):
        kinds = [c.kind for c in self.goals("tools")]
        self.assertEqual(kinds[0], UNLOCKS_TOOL)
        self.assertEqual(kinds, sorted(kinds, key=lambda kind: kind != UNLOCKS_TOOL))

    def test_pipeline_takes_the_preferred_goal_and_records_the_basis(self):
        class Extractor:
            def extract(self, utterance, **kwargs):
                return Proposal(action="propose_goal")

        turn = Pipeline(Extractor()).handle(
            "предложи цель", mode="survival", inventory=START,
            preferences={"interest": "weapons", "interest_basis": "5 из 6 целей"})
        self.assertEqual(turn.target, "minecraft:wooden_sword")
        self.assertEqual(turn.stages["interest"], {"kind": "weapons", "basis": "5 из 6 целей"})


if __name__ == "__main__":
    unittest.main()
