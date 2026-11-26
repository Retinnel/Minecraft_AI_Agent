"""Вторая среда на том же ядре: онтология Terraria из kb/worlds, тот же решатель.

Проверяется главное: планировщик, резолвер, валидатор и конвейер работают на другой игре
без собственного кода под неё — всё о мире берётся из онтологии.
"""

import tempfile
import unittest
from pathlib import Path

from kb import games
from kb.build import build
from kb.graph import KnowledgeBase
from orchestrator.extractor import build_prompt
from orchestrator.pipeline import Pipeline, Proposal, WORLD_GAME_ACTIONS
from resolver.resolve import Resolver
from solver.planner import Planner
from solver.render import render_tree
from solver.validator import ACT, CLARIFY, REJECT, Validator

SOURCE = Path(__file__).resolve().parent.parent / "kb" / "worlds" / "terraria"
START = {"terraria:copper_pickaxe": 1, "terraria:copper_axe": 1}


class Scripted:
    """Извлекатель-заглушка: возвращает заранее заданное намерение и помнит вызов."""

    def __init__(self, action, target=None, count=1):
        self.proposal = Proposal(action=action, target=target, count=count)
        self.calls = []

    def extract(self, utterance, **kwargs):
        self.calls.append(kwargs)
        return self.proposal


class TerrariaOntologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.report = build(SOURCE, Path(cls.dir.name))
        cls.kb = KnowledgeBase.load(Path(cls.dir.name) / "game.ttl")
        cls.planner = Planner(cls.kb)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_ontology_has_no_minecraft_in_it(self):
        self.assertEqual(self.report["игра"], "Terraria")
        self.assertFalse(self.kb.exists("minecraft:diamond"))
        self.assertTrue(self.kb.exists("terraria:nightmare_pickaxe"))

    def test_plan_goes_through_tool_tiers_of_this_world(self):
        plan = self.planner.plan("terraria:nightmare_pickaxe", 1, START)
        self.assertTrue(plan.feasible)
        items = {step.item for step in plan.root.walk()}
        # Демонит берётся только золотой киркой — её план строит сам.
        self.assertIn("terraria:gold_pickaxe", items)
        self.assertIn("terraria:shadow_scale", items)
        tree = render_tree(plan, self.kb)
        self.assertIn("на наковальне", tree)
        self.assertIn("в печи", tree)

    def test_tier_scale_comes_from_the_world_description(self):
        self.assertGreater(self.kb.tool_tier_rank("gold"), self.kb.tool_tier_rank("iron"))
        self.assertTrue(self.kb.tier_satisfies("nightmare", "gold"))
        self.assertFalse(self.kb.tier_satisfies("copper", "gold"))

    def test_without_starting_tools_the_plan_names_what_is_missing(self):
        plan = self.planner.plan("terraria:furnace", 1, {})
        self.assertFalse(plan.feasible)
        self.assertIn("terraria:copper_pickaxe", plan.missing)

    def test_station_item_is_known_to_the_knowledge_base(self):
        self.assertEqual(self.kb.station_item("iron_anvil"), "terraria:iron_anvil")
        self.assertIsNone(self.kb.station_item("inventory"))

    def test_resolver_and_validator_work_unchanged(self):
        resolver = Resolver(self.kb)
        self.assertEqual(resolver.resolve("кошмарную кирку", action="plan_goal").thing,
                         "terraria:nightmare_pickaxe")
        self.assertEqual(resolver.resolve("наковальня", action="plan_goal").thing,
                         "terraria:iron_anvil")
        self.assertFalse(resolver.resolve("алмазная кирка", action="plan_goal").ok)
        validator = Validator(self.kb)
        self.assertEqual(validator.validate("plan_goal", "terraria:gold_pickaxe", 1,
                                            "survival").outcome, ACT)
        self.assertEqual(validator.validate("plan_goal", "minecraft:diamond", 1,
                                            "survival").outcome, REJECT)


class WorldTreeTests(unittest.TestCase):
    """Древо развития второй среды: тот же вывод ступеней, витрина и стартовый инвентарь —
    из описания мира."""

    @classmethod
    def setUpClass(cls):
        from solver.techtree import TechTree
        games.reset()
        cls.world = games.find("Terraria")
        kb = cls.world.kb()
        cls.tree = TechTree(kb, Planner(kb), showcase=cls.world.showcase,
                            base=cls.world.starting_inventory)
        cls.structure = cls.tree.structure()

    def test_every_showcase_item_gets_into_the_tree(self):
        self.assertEqual(self.structure.skipped, [])
        self.assertIn("terraria:nightmare_pickaxe", self.structure.nodes)

    def test_tiers_follow_the_pickaxe_scale_of_this_world(self):
        nodes = self.structure.nodes
        self.assertEqual(nodes["terraria:wood"].tier, 0)
        # Демонит открывает золотая кирка: ступень выше руд, которые берёт медная.
        self.assertGreater(nodes["terraria:demonite_ore"].tier, nodes["terraria:iron_ore"].tier)
        self.assertGreaterEqual(nodes["terraria:nightmare_pickaxe"].tier,
                                nodes["terraria:demonite_ore"].tier)

    def test_state_comes_from_the_starting_inventory(self):
        state = self.tree.state(self.world.starting_inventory)
        self.assertEqual(state["terraria:copper_pickaxe"].status, "have")
        self.assertEqual(state["terraria:demonite_ore"].status, "locked")
        self.assertEqual(state["terraria:iron_ore"].status, "ready")


class WorldPipelineTests(unittest.TestCase):
    def setUp(self):
        games.reset()
        self.addCleanup(games.reset)

    def _pipeline(self, extractor):
        return Pipeline(extractor)

    def test_game_with_ontology_is_found_by_spoken_name(self):
        self.assertEqual(games.find("террария").game, "Terraria")
        self.assertEqual(games.find("Terraria").key, "terraria")
        self.assertIsNone(games.find("Elden Ring"))
        self.assertIsNone(games.find("Minecraft"))

    def test_plan_is_built_on_the_world_ontology_from_starting_inventory(self):
        extractor = Scripted("plan_goal", "золотая кирка")
        turn = self._pipeline(extractor).handle("как сделать золотую кирку", mode="creative",
                                                inventory={"minecraft:diamond": 9},
                                                game="террария")
        self.assertEqual((turn.outcome, turn.target), (ACT, "terraria:gold_pickaxe"))
        self.assertTrue(turn.stages["planner"]["feasible"])
        self.assertIsNone(turn.command)
        # Модели предъявлены действия мира, и подсказка — как для игры с базой знаний.
        call = extractor.calls[0]
        self.assertEqual(tuple(call["actions"]), WORLD_GAME_ACTIONS)
        self.assertTrue(call["ontology"])
        self.assertEqual(call["game"], "Terraria")

    def test_generic_name_is_clarified_with_this_worlds_options(self):
        turn = self._pipeline(Scripted("plan_goal", "кирка")).handle("хочу кирку", game="Terraria")
        self.assertEqual(turn.outcome, CLARIFY)
        self.assertIn("Кошмарная кирка", [option.label for option in turn.options])

    def test_world_question_is_answered_by_the_ontology(self):
        turn = self._pipeline(Scripted("ask_drops", "пожиратель миров")).handle(
            "что падает с пожирателя", game="Terraria")
        self.assertEqual(turn.outcome, ACT)
        self.assertIn("теневая чешуя", turn.world_summary.lower())

    def test_no_commands_for_the_second_world(self):
        turn = self._pipeline(Scripted("give_item", "золотая кирка")).handle(
            "дай золотую кирку", mode="creative", game="Terraria")
        self.assertEqual(turn.outcome, REJECT)
        self.assertIsNone(turn.command)

    def test_minecraft_call_is_unchanged(self):
        extractor = Scripted("plan_goal", "алмазная кирка")
        turn = self._pipeline(extractor).handle("как сделать алмазную кирку", mode="survival")
        self.assertEqual(turn.target, "minecraft:diamond_pickaxe")
        self.assertNotIn("ontology", extractor.calls[0])

    def test_game_without_ontology_still_only_searches(self):
        extractor = Scripted("plan_goal", "кирка")
        turn = self._pipeline(extractor).handle("как сделать кирку", game="Elden Ring")
        self.assertEqual(turn.outcome, REJECT)
        self.assertNotIn("ontology", extractor.calls[0])


class PromptTests(unittest.TestCase):
    def test_minecraft_prompt_does_not_depend_on_the_new_flag(self):
        actions = ("plan_goal", "ask_uses", "search_web")
        self.assertEqual(build_prompt(actions, game="Minecraft"),
                         build_prompt(actions, game="Minecraft", ontology=True))

    def test_world_with_ontology_keeps_recipe_questions_out_of_search(self):
        actions = ("plan_goal", "ask_uses", "search_web")
        with_kb = build_prompt(actions, game="Terraria", ontology=True)
        without = build_prompt(actions, game="Terraria")
        self.assertNotEqual(with_kb, without)
        self.assertIn("Terraria", with_kb)


if __name__ == "__main__":
    unittest.main()
