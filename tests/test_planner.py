"""Проверки планировщика.

Сверяют планы с тем, как устроена игра, и закрепляют поведение, которое легко сломать при
правке эвристик: учёт инвентаря, повторное использование инструмента, обход циклов,
предпочтение надёжных источников ненадёжным.
"""

import unittest
from pathlib import Path

from kb.graph import KnowledgeBase
from solver.render import render_alternatives
from solver.planner import CRAFT, HAVE, HUNT, MINE, MISSING, SMELT, Planner

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class PlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.planner = Planner(cls.kb)

    def items_of(self, plan, kind):
        return [step.item for step in plan.root.walk() if step.kind == kind]

    # --- инвентарь ---

    def test_goal_already_in_inventory_needs_no_actions(self):
        plan = self.planner.plan("minecraft:diamond", 3, {"minecraft:diamond": 5})
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.actions(), [])
        self.assertEqual(plan.root.kind, HAVE)

    def test_inventory_closes_part_of_the_goal(self):
        # Двух алмазов не хватает, значит остаток планируется, а имеющееся засчитывается.
        plan = self.planner.plan("minecraft:diamond_pickaxe", 1,
                                 {"minecraft:diamond": 3, "minecraft:stick": 2})
        self.assertTrue(plan.feasible)
        self.assertEqual(len(plan.actions()), 1)
        self.assertEqual(plan.root.kind, CRAFT)
        self.assertEqual(sorted(self.items_of(plan, HAVE)),
                         ["minecraft:diamond", "minecraft:stick"])

    def test_inventory_is_not_double_counted(self):
        # Одна палка есть, нужно две: план обязан добыть недостающую, а не засчитать обе.
        plan = self.planner.plan("minecraft:stick", 2, {"minecraft:stick": 1})
        self.assertTrue(plan.feasible)
        have = [step for step in plan.root.walk() if step.kind == HAVE
                and step.item == "minecraft:stick"]
        self.assertEqual(sum(step.count for step in have), 1)

    # --- цепочка развития ---

    def test_diamond_pickaxe_from_scratch_follows_the_progression(self):
        plan = self.planner.plan("minecraft:diamond_pickaxe", 1, {})
        self.assertTrue(plan.feasible)
        crafted = set(self.items_of(plan, CRAFT))
        # Алмаз требует железной кирки, та — каменной, та — деревянной.
        self.assertIn("minecraft:diamond_pickaxe", crafted)
        self.assertIn("minecraft:iron_pickaxe", crafted)
        self.assertIn("minecraft:stone_pickaxe", crafted)
        self.assertIn("minecraft:wooden_pickaxe", crafted)

    def test_diamond_is_mined_with_an_iron_tier_tool(self):
        plan = self.planner.plan("minecraft:diamond", 1, {})
        mining = [step for step in plan.root.walk()
                  if step.kind == MINE and step.item == "minecraft:diamond"]
        self.assertTrue(mining)
        self.assertEqual(mining[0].tool_kind, "pickaxe")
        self.assertEqual(mining[0].tool_tier, "iron")

    def test_existing_tool_is_reused_instead_of_being_crafted(self):
        plan = self.planner.plan("minecraft:diamond", 1, {"minecraft:diamond_pickaxe": 1})
        self.assertTrue(plan.feasible)
        # Алмазная кирка сильнее железной, поэтому годится и делать ничего не нужно.
        self.assertNotIn("minecraft:iron_pickaxe", self.items_of(plan, CRAFT))
        self.assertIn("minecraft:diamond_pickaxe", self.items_of(plan, HAVE))
        self.assertEqual(len(plan.actions()), 1)

    def test_weaker_tool_does_not_count(self):
        # Каменной киркой алмаз не добыть, значит железную всё равно придётся сделать.
        plan = self.planner.plan("minecraft:diamond", 1, {"minecraft:stone_pickaxe": 1})
        self.assertIn("minecraft:iron_pickaxe", self.items_of(plan, CRAFT))

    # --- количества ---

    def test_recipe_yield_is_taken_into_account(self):
        # Один рецепт даёт четыре палки, значит на пять нужно два подхода, а не пять.
        plan = self.planner.plan("minecraft:stick", 5, {"minecraft:oak_planks": 64})
        self.assertTrue(plan.feasible)
        planks = [step for step in plan.root.walk() if step.item == "minecraft:oak_planks"]
        self.assertTrue(planks)
        self.assertEqual(planks[0].count, 4)

    def test_larger_goal_requires_more_input(self):
        small = self.planner.plan("minecraft:stick", 4, {"minecraft:oak_planks": 64})
        large = self.planner.plan("minecraft:stick", 64, {"minecraft:oak_planks": 64})
        take = lambda plan: next(step.count for step in plan.root.walk()
                                 if step.item == "minecraft:oak_planks")
        self.assertLess(take(small), take(large))

    # --- надёжность источников ---

    def test_unreliable_sources_are_avoided_when_a_reliable_path_exists(self):
        # Палки выпадают из мёртвого куста за один шаг, но с шансом получить ноль.
        plan = self.planner.plan("minecraft:stick", 4, {})
        self.assertTrue(plan.feasible)
        for step in plan.root.walk():
            with self.subTest(step=step.item):
                self.assertFalse(step.rare, "план не должен опираться на ненадёжное выпадение")

    def test_unreliable_source_is_still_used_when_it_is_the_only_one(self):
        plan = self.planner.plan("minecraft:rotten_flesh", 1, {})
        self.assertTrue(plan.feasible)
        self.assertIn(HUNT, [step.kind for step in plan.root.walk()])

    def test_iron_is_smelted_rather_than_hunted(self):
        # Железный слиток падает с железного голема, но добыча руды надёжнее охоты.
        plan = self.planner.plan("minecraft:iron_ingot", 3, {})
        self.assertTrue(plan.feasible)
        self.assertIn(SMELT, [step.kind for step in plan.root.walk()])

    # --- границы ---

    def test_uncraftable_and_unobtainable_goal_is_reported(self):
        plan = self.planner.plan("minecraft:bedrock", 1, {})
        self.assertFalse(plan.feasible)
        self.assertIn("minecraft:bedrock", plan.missing)
        missing = [step for step in plan.root.walk() if step.kind == MISSING]
        self.assertTrue(missing[0].reason)

    def test_reversible_recipe_does_not_loop(self):
        # Алмазный блок делается из девяти алмазов, а алмазы получаются из блока.
        plan = self.planner.plan("minecraft:diamond_block", 1, {})
        self.assertTrue(plan.feasible)
        self.assertLess(len(list(plan.root.walk())), 100)

    def test_plan_is_deterministic(self):
        first = self.planner.plan("minecraft:diamond_pickaxe", 1, {})
        second = Planner(self.kb).plan("minecraft:diamond_pickaxe", 1, {})
        self.assertEqual([(s.kind, s.item, s.count) for s in first.root.walk()],
                         [(s.kind, s.item, s.count) for s in second.root.walk()])

    def test_inventory_passed_in_is_not_mutated(self):
        inventory = {"minecraft:diamond": 3, "minecraft:stick": 2}
        snapshot = dict(inventory)
        self.planner.plan("minecraft:diamond_pickaxe", 1, inventory)
        self.assertEqual(inventory, snapshot)


if __name__ == "__main__":
    unittest.main()


class AlternativesTests(unittest.TestCase):
    """Развилки вывода: решатель помнит, что ещё рассматривал и почему выбрал не это.

    Это не отладочный след, а материал для ответа на вопрос «почему план именно такой».
    Проверяется, что выбор действительно был и что причина отказа выражена в тех же
    величинах, по которым шло сравнение.
    """

    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.planner = Planner(cls.kb)

    def decision_for(self, plan, item):
        return next((d for d in plan.decisions if d.item == item), None)

    def test_single_option_is_not_a_decision(self):
        """Там, где способ один, объяснять нечего — развилка не записывается."""
        plan = self.planner.plan("minecraft:diamond_pickaxe", 1)
        self.assertTrue(all(len(d.options) > 1 for d in plan.decisions))

    def test_exactly_one_option_is_chosen(self):
        plan = self.planner.plan("minecraft:diamond_pickaxe", 1)
        for decision in plan.decisions:
            with self.subTest(item=decision.item):
                self.assertEqual(sum(1 for o in decision.options if o.chosen), 1)

    def test_hunting_is_rejected_in_favour_of_mining(self):
        """Заготовленный ответ на защите: железо можно выбить из голема, но это дороже."""
        plan = self.planner.plan("minecraft:iron_ingot", 1)
        decision = self.decision_for(plan, "minecraft:iron_ingot")
        self.assertIsNotNone(decision)
        hunted = [o for o in decision.options if o.detail == "minecraft:iron_golem"]
        self.assertTrue(hunted, "вариант с охотой должен быть рассмотрен")
        self.assertFalse(hunted[0].chosen)
        self.assertGreater(hunted[0].effort, decision.chosen.effort)

    def test_rejected_variants_keep_their_source(self):
        """Три рецепта железного слитка различаются сырьём, а не станцией."""
        plan = self.planner.plan("minecraft:iron_ingot", 1)
        decision = self.decision_for(plan, "minecraft:iron_ingot")
        smelts = {o.via for o in decision.options if o.via}
        self.assertGreater(len(smelts), 1)

    def test_explanation_mentions_both_sides(self):
        plan = self.planner.plan("minecraft:iron_ingot", 1)
        text = render_alternatives(plan, self.kb)
        self.assertIn("выбрано", text)
        self.assertIn("отвергнуто", text)
        self.assertIn("Железный голем", text)
