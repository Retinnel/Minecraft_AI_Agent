"""Ступень развития игрока: вывод из онтологии, а не назначенная руками таблица.

Ступень определяется по лучшей кирке, открытое ею — по требованиям источников добычи,
путь к следующей — планировщиком. Модель в выводе не участвует.
"""

import unittest
from pathlib import Path

from kb.graph import KnowledgeBase
from orchestrator import Pipeline, Proposal
from orchestrator.pipeline import SHOW_PROGRESS
from orchestrator.responder import MODEL, TEMPLATE, Responder
from solver.progression import Progression, render_progress
from solver.validator import ACT, SURVIVAL

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"

WOODEN = "minecraft:wooden_pickaxe"
STONE = "minecraft:stone_pickaxe"
IRON = "minecraft:iron_pickaxe"
DIAMOND = "minecraft:diamond_pickaxe"
NETHERITE = "minecraft:netherite_pickaxe"


class StubIntent:
    def __init__(self, proposal):
        self.proposal = proposal

    def extract(self, utterance, **context):
        return self.proposal


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class ProgressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.progression = Progression(cls.kb)

    def test_no_pickaxe_is_the_start(self):
        report = self.progression.assess({})
        self.assertEqual(report.rank, -1)
        self.assertIsNone(report.tool)
        self.assertEqual(report.next_tool, WOODEN)
        self.assertTrue(report.next_plan.feasible)
        self.assertIn("начало пути", render_progress(report, self.kb))

    def test_stage_follows_the_best_pickaxe(self):
        report = self.progression.assess({WOODEN: 1, STONE: 1})
        self.assertEqual(report.rank, 1)
        self.assertEqual(report.tool, STONE)
        self.assertEqual(report.next_tool, IRON)

    def test_gold_is_not_a_stage_of_its_own(self):
        # Золотая кирка по уровню равна деревянной: ступень та же, дальше — камень.
        report = self.progression.assess({"minecraft:golden_pickaxe": 1})
        self.assertEqual(report.rank, 0)
        self.assertEqual(report.next_tool, STONE)

    def test_unlocks_come_from_tool_requirements_in_the_ontology(self):
        stone = self.progression.assess({STONE: 1})
        self.assertIn("minecraft:raw_iron", stone.unlocks)
        iron = self.progression.assess({IRON: 1})
        self.assertIn("minecraft:diamond", iron.unlocks)
        self.assertIn("minecraft:diamond", stone.next_unlocks)
        diamond = self.progression.assess({DIAMOND: 1})
        self.assertIn("minecraft:obsidian", diamond.unlocks)

    def test_wood_stage_names_useful_resources_first(self):
        wood = self.progression.assess({WOODEN: 1})
        self.assertEqual(wood.unlocks[:2], ["minecraft:cobblestone", "minecraft:coal"])

    def test_last_stage_has_no_next(self):
        report = self.progression.assess({NETHERITE: 1})
        self.assertTrue(report.final)
        self.assertIn("Выше ступени нет", render_progress(report, self.kb))

    def test_next_plan_counts_from_the_inventory(self):
        bare = self.progression.assess({STONE: 1})
        ready = self.progression.assess({STONE: 1, "minecraft:iron_ingot": 3,
                                         "minecraft:stick": 2, "minecraft:crafting_table": 1})
        self.assertLess(len(ready.next_plan.actions()), len(bare.next_plan.actions()))
        self.assertEqual(len(ready.next_plan.actions()), 1)

    # --- в конвейере ---

    def test_pipeline_answers_from_the_solver(self):
        pipeline = Pipeline(StubIntent(Proposal(SHOW_PROGRESS)), self.kb)
        for mode in ("creative", SURVIVAL):
            turn = pipeline.handle("на каком я этапе", mode=mode, inventory={STONE: 1})
            self.assertEqual(turn.outcome, ACT)
            self.assertEqual(turn.action, SHOW_PROGRESS)
            self.assertEqual(turn.target, IRON)
            self.assertIn("каменная", turn.progress_summary)
            self.assertTrue(turn.plan_tree)
            self.assertIsNone(turn.command)

    def test_retelling_must_name_the_next_tool(self):
        pipeline = Pipeline(StubIntent(Proposal(SHOW_PROGRESS)), self.kb)
        turn = pipeline.handle("мой прогресс", inventory={STONE: 1})
        vague = Responder(ask_text=lambda messages: "Ты молодец, продолжай в том же духе!")
        self.assertEqual(vague.reply(turn, "мой прогресс").source, TEMPLATE)
        exact = Responder(ask_text=lambda messages: "Ты на каменной ступени, дальше — железная кирка.")
        self.assertEqual(exact.reply(turn, "мой прогресс").source, MODEL)


if __name__ == "__main__":
    unittest.main()
