"""Разбор смерти: решатель выводит, какая защита была достижима из инвентаря.

Проверяется главное отличие от совета «носи броню»: мера засчитывается достижимой, только
если план её изготовления от инвентаря на момент смерти не содержит добычи.
"""

import unittest
from pathlib import Path

from kb.graph import KnowledgeBase
from solver.planner import Planner
from solver.postmortem import (ARMOR, CRAFTABLE, UNUSED, WORN, DeathAnalyzer, essentials,
                               focus_plan, render_postmortem)

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"

TABLE = "minecraft:crafting_table"
INGOT = "minecraft:iron_ingot"
CHEST = "minecraft:iron_chestplate"


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class DeathAnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.analyzer = DeathAnalyzer(cls.kb, Planner(cls.kb))

    def items(self, measures):
        return [measure.item for measure in measures]

    # --- угроза ---

    def test_cause_is_mapped_to_a_threat(self):
        self.assertEqual(self.analyzer.analyze("explosion.player").threat, "explosion")
        self.assertEqual(self.analyzer.analyze("mob_attack").threat, "combat")
        self.assertEqual(self.analyzer.analyze("что-то новое").threat, "other")

    def test_unprotectable_threat_is_said_so(self):
        result = self.analyzer.analyze("outOfWorld", {INGOT: 30, TABLE: 1})
        self.assertFalse(result.protectable)
        self.assertIn("не защищает", render_postmortem(result, self.kb))

    # --- броня ---

    def test_armor_craftable_from_the_inventory_is_named(self):
        result = self.analyzer.analyze("mob_attack", {INGOT: 8, TABLE: 1})
        self.assertTrue(result.preventable)
        chest = next(m for m in result.craftable if m.item == CHEST)
        self.assertEqual(chest.status, CRAFTABLE)
        self.assertEqual(chest.steps, 1)
        self.assertEqual(chest.points, 6)
        # Самая полезная мера идёт первой.
        self.assertEqual(result.craftable[0].item, CHEST)
        self.assertIn("Железный нагрудник", essentials(result, self.kb))

    def test_measures_share_one_inventory(self):
        # Восьми слитков хватает на нагрудник — и только на него: поножи и ботинки из тех
        # же слитков уже не собрать, и называть их достижимыми нельзя.
        result = self.analyzer.analyze("mob_attack", {INGOT: 8, TABLE: 1})
        armor = [m.item for m in result.craftable if m.group == ARMOR]
        self.assertEqual(armor, [CHEST])
        # Слитков больше — мер больше.
        rich = self.analyzer.analyze("mob_attack", {INGOT: 15, TABLE: 1})
        self.assertEqual([m.item for m in rich.craftable if m.group == ARMOR],
                         [CHEST, "minecraft:iron_leggings"])

    def test_missing_station_counts_as_extra_steps_or_blocks(self):
        # Слитки есть, верстака нет, но есть брёвна: верстак собирается, это лишние действия.
        with_logs = self.analyzer.analyze("mob_attack", {INGOT: 8, "minecraft:oak_log": 1})
        chest = next(m for m in with_logs.craftable if m.item == CHEST)
        self.assertGreater(chest.steps, 1)
        # Ни верстака, ни дерева: нагрудник из слитков не собрать, добыча не засчитывается.
        bare = self.analyzer.analyze("mob_attack", {INGOT: 8})
        self.assertNotIn(CHEST, self.items(bare.craftable))

    def test_gathering_is_not_counted_as_reachable(self):
        result = self.analyzer.analyze("mob_attack", {TABLE: 1})
        self.assertFalse(result.preventable)
        self.assertEqual(result.craftable, [])
        # Но ближайшая мера на будущее названа и план к ней есть.
        self.assertIsNotNone(result.next_goal)
        self.assertTrue(result.next_plan.feasible)
        self.assertIn("К следующему разу", render_postmortem(result, self.kb))

    def test_armor_lying_in_the_inventory_is_unused(self):
        result = self.analyzer.analyze("mob_attack", {CHEST: 1})
        self.assertEqual(self.items(result.unused)[0], CHEST)
        self.assertEqual(result.unused[0].status, UNUSED)
        self.assertIn("без дела", render_postmortem(result, self.kb))

    def test_worn_armor_is_not_offered_again(self):
        result = self.analyzer.analyze("mob_attack", {CHEST: 1, INGOT: 8, TABLE: 1}, armor=[CHEST])
        self.assertIn(CHEST, self.items(result.worn))
        self.assertNotIn(CHEST, self.items(result.unused))
        self.assertNotIn(CHEST, self.items(result.craftable))
        # Остальные слоты из слитков по-прежнему собираются.
        self.assertTrue(any(m.group == ARMOR for m in result.craftable))

    def test_better_piece_is_offered_over_a_worn_weaker_one(self):
        result = self.analyzer.analyze("mob_attack", {INGOT: 8, TABLE: 1},
                                       armor=["minecraft:leather_chestplate"])
        self.assertIn(CHEST, self.items(result.craftable))

    # --- прочие меры ---

    def test_shield_in_hand_counts_and_in_bag_does_not(self):
        held = self.analyzer.analyze("arrow", {"minecraft:shield": 1}, held=["minecraft:shield"])
        self.assertEqual(held.worn[0].status, WORN)
        bagged = self.analyzer.analyze("arrow", {"minecraft:shield": 1})
        self.assertIn("minecraft:shield", self.items(bagged.unused))

    def test_food_for_hunger_and_water_for_fire(self):
        hunger = self.analyzer.analyze("starve", {"minecraft:bread": 3})
        self.assertIn("minecraft:bread", self.items(hunger.unused))
        fire = self.analyzer.analyze("lava", {INGOT: 3, TABLE: 1})
        self.assertIn("minecraft:bucket", self.items(fire.craftable))

    # --- вывод наружу ---

    def test_summary_carries_the_solver_facts(self):
        result = self.analyzer.analyze("explosion.player", {INGOT: 8, TABLE: 1})
        text = render_postmortem(result, self.kb)
        self.assertIn("взрыв", text)
        self.assertIn("Брони на тебе не было", text)
        self.assertIn("железный нагрудник (1 действие)", text)
        self.assertIsNotNone(focus_plan(result))

    def test_analysis_is_deterministic_and_does_not_mutate_the_inventory(self):
        inventory = {INGOT: 8, TABLE: 1}
        first = self.analyzer.analyze("mob_attack", inventory)
        second = self.analyzer.analyze("mob_attack", inventory)
        self.assertEqual(self.items(first.craftable), self.items(second.craftable))
        self.assertEqual(inventory, {INGOT: 8, TABLE: 1})


if __name__ == "__main__":
    unittest.main()
