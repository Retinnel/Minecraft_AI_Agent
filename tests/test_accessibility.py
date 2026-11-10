"""Доступность источников добычи: рукописный слой базы знаний и его учёт планировщиком.

В данных игры нет измерений и биомов, поэтому без этого слоя чернит из Незера и бамбук из
джунглей стоили столько же, сколько булыжник и доски, и при равной цене побеждал алфавит.
Проверки закрепляют, что план идёт привычным путём Верхнего мира, а дальние источники
остаются доступными, когда других нет.
"""

import unittest
from pathlib import Path

from kb.graph import KnowledgeBase
from solver.planner import CRAFT, MINE, Planner
from solver.render import render_tree

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class AccessibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.planner = Planner(cls.kb)

    def mined(self, plan):
        return {(step.item, step.source) for step in plan.root.walk() if step.kind == MINE}

    # --- слой в базе знаний ---

    def test_layer_marks_dimensions_and_unnatural_blocks(self):
        self.assertEqual(self.kb.accessibility("minecraft:blackstone"), "nether")
        self.assertEqual(self.kb.accessibility("minecraft:end_stone"), "end")
        self.assertEqual(self.kb.accessibility("minecraft:bamboo"), "rare_biome")
        self.assertEqual(self.kb.accessibility("minecraft:deepslate_iron_ore"), "deep")
        # Классы по приставке: ошкуренные брёвна и растения в горшках в мире не встречаются.
        self.assertEqual(self.kb.accessibility("minecraft:stripped_oak_log"), "unnatural")
        self.assertEqual(self.kb.accessibility("minecraft:potted_bamboo"), "unnatural")
        self.assertIsNone(self.kb.accessibility("minecraft:cobblestone"))

    def test_sources_sort_nearer_first(self):
        sources = self.kb.sources_of("minecraft:raw_iron")
        self.assertEqual(sources[0].id, "minecraft:iron_ore")
        self.assertEqual(sources[0].remote, None)
        self.assertEqual(sources[1].remote, "deep")

    # --- выбор планировщика ---

    def test_stone_pickaxe_uses_cobblestone_not_blackstone(self):
        plan = self.planner.plan("minecraft:stone_pickaxe", 1, {})
        self.assertTrue(plan.feasible)
        items = {item for item, _ in self.mined(plan)}
        self.assertIn("minecraft:cobblestone", items)
        self.assertNotIn("minecraft:blackstone", items)
        self.assertNotIn("minecraft:cobbled_deepslate", items)

    def test_sticks_come_from_planks_not_bamboo(self):
        plan = self.planner.plan("minecraft:stick", 2, {})
        self.assertTrue(plan.feasible)
        items = {item for item, _ in self.mined(plan)}
        self.assertNotIn("minecraft:bamboo", items)
        crafted = {step.item for step in plan.root.walk() if step.kind == CRAFT}
        self.assertTrue(any(item.endswith("_planks") for item in crafted))

    def test_iron_is_mined_from_plain_ore_not_deepslate(self):
        plan = self.planner.plan("minecraft:iron_ingot", 1, {"minecraft:stone_pickaxe": 1})
        sources = {source for _, source in self.mined(plan)}
        self.assertIn("minecraft:iron_ore", sources)
        self.assertNotIn("minecraft:deepslate_iron_ore", sources)

    def test_planks_come_from_a_common_natural_log(self):
        plan = self.planner.plan("minecraft:crafting_table", 1, {})
        sources = {source for _, source in self.mined(plan)}
        self.assertEqual(len(sources), 1)
        source = next(iter(sources))
        self.assertIn(source, {"minecraft:oak_log", "minecraft:birch_log", "minecraft:spruce_log"})
        self.assertNotIn("stripped", source)

    def test_material_already_in_the_inventory_beats_the_globally_cheaper_one(self):
        # Берёза «вообще» дешевле дуба по алфавиту, но у игрока лежит дуб: доски делаются
        # из него, а не из берёзы, за которой нужно идти в мир.
        plan = self.planner.plan("minecraft:crafting_table", 1, {"minecraft:oak_log": 1})
        self.assertTrue(plan.feasible)
        self.assertEqual(self.mined(plan), set())
        crafted = {step.item for step in plan.root.walk() if step.kind == CRAFT}
        self.assertIn("minecraft:oak_planks", crafted)

    def test_remote_source_is_still_used_when_it_is_the_only_one(self):
        # Незеритовый лом добывается только из древних обломков в Незере: штраф делает путь
        # дороже, но не закрывает его.
        plan = self.planner.plan("minecraft:netherite_scrap", 1, {"minecraft:diamond_pickaxe": 1})
        self.assertTrue(plan.feasible)
        sources = {source for _, source in self.mined(plan)}
        self.assertIn("minecraft:ancient_debris", sources)

    def test_tree_names_the_dimension(self):
        plan = self.planner.plan("minecraft:netherite_scrap", 1, {"minecraft:diamond_pickaxe": 1})
        self.assertIn("в Незере", render_tree(plan, self.kb))


if __name__ == "__main__":
    unittest.main()
