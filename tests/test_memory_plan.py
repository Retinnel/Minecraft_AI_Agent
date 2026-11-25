"""Память мира в плане и ответах о мире: запасы сундуков, увиденные руды, нужный инструмент.

План по-прежнему строит онтология; память лишь сообщает, что уже лежит в хранилищах и где
игрок видел источники. Без памяти план обязан оставаться прежним.
"""

import unittest
from pathlib import Path

from kb.graph import KnowledgeBase
from memory.adapters.minecraft import container, observations
from memory.model import NETHER, OVERWORLD, Position
from memory.store import MemoryStore
from orchestrator import Pipeline, Proposal
from orchestrator.pipeline import RECALL
from orchestrator.responder import Responder
from solver.locator import Locator, tool_sentence
from solver.planner import MINE, SMELT, TAKE, Planner
from solver.render import render_summary, render_tree
from solver.worldfacts import SOURCES

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"
HOME = Position(OVERWORLD, 0, 64, 0)


def block(block_id, x, y, z, dimension="minecraft:overworld"):
    return {"id": block_id, "x": x, "y": y, "z": z, "dimension": dimension}


def chest(items, x=12, y=64, z=-40):
    return {"block": "minecraft:chest", "x": x, "y": y, "z": z,
            "dimension": "minecraft:overworld", "items": items}


class Stub:
    def __init__(self, proposal):
        self.proposal = proposal

    def extract(self, utterance, **context):
        return self.proposal


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class PlanWithStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.planner = Planner(cls.kb)

    def kinds(self, plan, kind):
        return [step.item for step in plan.root.walk() if step.kind == kind]

    def test_without_storage_the_plan_is_unchanged(self):
        before = self.planner.plan("minecraft:iron_pickaxe", 1, {})
        after = self.planner.plan("minecraft:iron_pickaxe", 1, {}, storage={}, where={}, known={})
        self.assertEqual(render_tree(before, self.kb), render_tree(after, self.kb))
        self.assertEqual(before.effort, after.effort)
        self.assertEqual(after.taken(), [])

    def test_ingots_from_a_chest_replace_smelting(self):
        plain = self.planner.plan("minecraft:iron_pickaxe", 1, {})
        stocked = self.planner.plan("minecraft:iron_pickaxe", 1, {},
                                    storage={"minecraft:iron_ingot": 5},
                                    where={"minecraft:iron_ingot": "Сундук, x 12, y 64, z −40"})
        self.assertTrue(stocked.feasible)
        self.assertNotIn("minecraft:iron_ingot", self.kinds(stocked, SMELT))
        taken = {step.item: step for step in stocked.taken() if not step.children}
        self.assertEqual(taken["minecraft:iron_ingot"].count, 3)
        self.assertEqual(taken["minecraft:iron_ingot"].where, "Сундук, x 12, y 64, z −40")
        self.assertLess(len(stocked.actions()), len(plain.actions()))
        self.assertIn("взять из хранилища: железный слиток ×3", render_summary(stocked, self.kb))
        self.assertIn("взять из хранилища: Сундук", render_tree(stocked, self.kb))

    def test_partial_stock_is_topped_up(self):
        plan = self.planner.plan("minecraft:iron_pickaxe", 1, {},
                                 storage={"minecraft:iron_ingot": 1})
        taken = [step for step in plan.taken() if not step.children]
        self.assertEqual([(step.item, step.count) for step in taken], [("minecraft:iron_ingot", 1)])
        # Слиток из сундука засчитан, руды добывается на оставшиеся два.
        smelted = next(step for step in plan.root.walk()
                       if step.kind == SMELT and step.item == "minecraft:iron_ingot")
        self.assertEqual([(step.kind, step.count) for step in smelted.children],
                         [(TAKE, 1), (MINE, 2)])

    def test_inventory_is_used_before_storage(self):
        plan = self.planner.plan("minecraft:iron_pickaxe", 1,
                                 {"minecraft:iron_ingot": 3, "minecraft:stick": 2},
                                 storage={"minecraft:iron_ingot": 10})
        self.assertEqual(plan.taken(), [])

    def test_a_stored_pickaxe_is_taken_instead_of_crafted(self):
        plan = self.planner.plan("minecraft:diamond", 1, {},
                                 storage={"minecraft:iron_pickaxe": 1})
        self.assertIn("minecraft:iron_pickaxe", [step.item for step in plan.taken()])
        self.assertNotIn("minecraft:iron_pickaxe", [step.item for step in plan.actions()])

    def test_storage_alone_can_close_the_goal(self):
        plan = self.planner.plan("minecraft:diamond", 2, {}, storage={"minecraft:diamond": 4})
        self.assertEqual(plan.actions(), [])
        self.assertEqual(plan.root.kind, TAKE)
        self.assertIn("взять из хранилища", render_summary(plan, self.kb))

    def test_a_seen_source_is_marked_in_the_plan(self):
        plan = self.planner.plan("minecraft:iron_ingot", 1, {"minecraft:stone_pickaxe": 1},
                                 known={"minecraft:iron_ore": "x 5, y 40, z 5"})
        mined = [step for step in plan.root.walk() if step.kind == MINE and step.where]
        self.assertTrue(mined)
        self.assertEqual(mined[0].source, "minecraft:iron_ore")
        self.assertIn("ты видел: x 5, y 40, z 5", render_tree(plan, self.kb))


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class LocatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.locator = Locator(cls.kb)

    def setUp(self):
        self.store = MemoryStore(None)

    def test_nearest_source_first_and_other_dimension_last(self):
        observations(self.store, {"blocks": [
            block("minecraft:diamond_ore", 300, 12, 0),
            block("minecraft:deepslate_diamond_ore", -40, -58, 0),
            block("minecraft:diamond_ore", 1, 12, 1, NETHER)]}, self.kb)
        located = self.locator.locate("minecraft:diamond", self.store, position=HOME)
        self.assertEqual([entry.distance for entry in located], [40, 300, None])
        self.assertEqual(located[0].fact.subject, "minecraft:deepslate_diamond_ore")

    def test_tool_tier_comes_from_the_ontology(self):
        observations(self.store, {"blocks": [block("minecraft:diamond_ore", 10, 12, 0)]}, self.kb)
        without = self.locator.locate("minecraft:diamond", self.store,
                                      inventory={"minecraft:stone_pickaxe": 1}, position=HOME)[0]
        self.assertEqual((without.tool, without.tool_ok), ("minecraft:iron_pickaxe", False))
        self.assertEqual(tool_sentence(without, self.kb),
                         "Нужна железная кирка или лучше — её у тебя нет.")
        better = self.locator.locate("minecraft:diamond", self.store,
                                     inventory={"minecraft:diamond_pickaxe": 1}, position=HOME)[0]
        self.assertTrue(better.tool_ok)

    def test_unrelated_resources_are_ignored(self):
        observations(self.store, {"blocks": [block("minecraft:coal_ore", 1, 60, 1)]}, self.kb)
        self.assertEqual(self.locator.locate("minecraft:diamond", self.store), [])

    def test_storage_of_the_current_dimension(self):
        container(self.store, chest({"minecraft:iron_ingot": 5}), self.kb)
        totals, where = self.locator.storage(self.store, HOME)
        self.assertEqual(totals, {"minecraft:iron_ingot": 5})
        self.assertIn("x 12, y 64, z −40", where["minecraft:iron_ingot"])
        totals, _ = self.locator.storage(self.store, Position(NETHER, 0, 64, 0))
        self.assertEqual(totals, {})


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class PipelineMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def setUp(self):
        self.store = MemoryStore(None)

    def handle(self, proposal, text, **kwargs):
        return Pipeline(Stub(proposal), self.kb).handle(text, memory=self.store, position=HOME,
                                                        **kwargs)

    def test_plan_takes_from_a_remembered_chest(self):
        container(self.store, chest({"minecraft:iron_ingot": 5}), self.kb)
        turn = self.handle(Proposal("plan_goal", "железная кирка"), "как сделать железную кирку")
        self.assertEqual(turn.outcome, "act")
        self.assertEqual(turn.stages["planner"]["taken"], {"minecraft:iron_ingot": 3})
        self.assertTrue(any("из хранилищ по памяти мира" in line for line in turn.explanation))
        self.assertIn("взять из хранилища", turn.plan_summary)

    def test_plan_without_memory_is_unchanged(self):
        plain = Pipeline(Stub(Proposal("plan_goal", "железная кирка")), self.kb).handle(
            "как сделать железную кирку")
        empty = self.handle(Proposal("plan_goal", "железная кирка"), "как сделать железную кирку")
        self.assertEqual(plain.plan_summary, empty.plan_summary)
        self.assertNotIn("taken", empty.stages["planner"])

    def test_sources_answer_mentions_what_was_seen(self):
        observations(self.store, {"blocks": [block("minecraft:diamond_ore", -120, -58, 340)]}, self.kb)
        turn = self.handle(Proposal(SOURCES, "алмаз"), "откуда берутся алмазы",
                           inventory={"minecraft:iron_pickaxe": 1})
        self.assertIn("Ты видел: Алмазная руда", turn.world_summary)
        self.assertIn("x −120, y −58, z 340", turn.world_summary)
        self.assertIn("она у тебя есть", turn.world_summary)
        self.assertEqual(turn.memory_numbers, (-120, -58, 340))
        responder = Responder(ask_text=lambda messages: "Алмазы добываются из алмазной руды.")
        # Пересказ без координат не принимается: остаётся шаблон с числами из памяти.
        self.assertIn("−120", responder.reply(turn, "откуда берутся алмазы").text)

    def test_ambiguous_name_searches_every_reading(self):
        # «Железо» — слиток, кусочек или рудное железо: для «где» переспрашивать незачем.
        container(self.store, chest({"minecraft:iron_ingot": 5}), self.kb)
        turn = self.handle(Proposal(RECALL, "железо"), "где у меня железо")
        self.assertEqual(turn.outcome, "act")
        self.assertIn("minecraft:iron_ingot", turn.stages["memory"]["subjects"])
        self.assertIn("Сундук", turn.memory_summary)

    def test_recall_of_ore_says_whether_the_pickaxe_is_there(self):
        observations(self.store, {"blocks": [block("minecraft:diamond_ore", 10, 12, 0)]}, self.kb)
        turn = self.handle(Proposal(RECALL, "алмазы"), "где алмазы",
                           inventory={"minecraft:wooden_pickaxe": 1})
        self.assertIn("Нужна железная кирка или лучше — её у тебя нет.", turn.memory_summary)


if __name__ == "__main__":
    unittest.main()
