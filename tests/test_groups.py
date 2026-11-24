"""Обобщённые названия в плане: «любое бревно» там, где рецепту годится любое.

Планировщик по-прежнему выбирает конкретный предмет; проверяется только то, как шаг
называется для игрока, и что запас игрока своего имени не теряет.
"""

import unittest
from pathlib import Path

from kb.graph import KnowledgeBase
from solver import groups
from solver.planner import HAVE, Planner
from solver.render import render_summary, render_tree
from solver.techtree import HAVE_IT, TechTree

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


def mc(name):
    return f"minecraft:{name}"


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class GroupNamesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.planner = Planner(cls.kb)

    def tree(self, goal, inventory=None):
        return render_tree(self.planner.plan(mc(goal), 1, inventory or {}), self.kb)

    def test_any_planks_and_any_log_when_the_recipe_takes_any(self):
        tree = self.tree("stone_pickaxe")
        self.assertIn("Любые доски ×2 — скрафтить в руках", tree)
        self.assertIn("Любое бревно ×1 — добыть", tree)
        self.assertIn("Булыжник любого вида ×3 — добыть", tree)
        self.assertNotIn("Берёзов", tree)

    def test_specific_goal_keeps_specific_names(self):
        """Берёзовые доски делаются только из берёзы: обобщать здесь нельзя."""
        tree = self.tree("birch_planks")
        self.assertIn("Берёзовые доски ×1", tree)
        self.assertIn("Берёзовое бревно ×1 — добыть", tree)
        self.assertNotIn("юбое", tree)

    def test_what_the_player_owns_keeps_its_own_name(self):
        tree = self.tree("stone_pickaxe", {mc("oak_planks"): 2, mc("oak_log"): 1})
        self.assertIn("Дубовые доски ×2 — есть", tree)
        self.assertIn("Дубовое бревно ×1 — есть", tree)

    def test_leftover_of_a_generic_step_is_named_the_same(self):
        """Остаток досок, сделанных по плану, не становится вдруг «берёзовым»."""
        tree = self.tree("stone_pickaxe")
        self.assertIn("Любые доски ×2 — есть", tree)

    def test_source_is_not_repeated_when_it_adds_nothing(self):
        tree = self.tree("iron_pickaxe")
        self.assertIn("Железная руда ×3 — добыть (каменная кирка и выше)", tree)
        self.assertNotIn("из «Железная руда»", tree)

    def test_summary_uses_the_same_names(self):
        summary = render_summary(self.planner.plan(mc("stone_pickaxe"), 1, {}), self.kb)
        self.assertIn("добыть: любое бревно, булыжник любого вида", summary)

    def test_planner_still_picks_a_concrete_item(self):
        plan = self.planner.plan(mc("stick"), 1, {})
        planks = plan.root.children[0]
        self.assertEqual(planks.group, "planks")
        self.assertTrue(self.kb.exists(planks.item))
        self.assertIn(planks.item, groups.members("planks", self.kb))

    def test_every_labelled_group_exists_in_the_knowledge_base(self):
        layer = groups._layer()
        for group in list(layer["labels"]) + list(layer["within"]):
            with self.subTest(group=group):
                self.assertGreater(len(groups.members(group, self.kb)), 1)
        for narrow, wide in layer["within"].items():
            self.assertIn(wide, layer["labels"])
            self.assertTrue(set(groups.members(narrow, self.kb))
                            <= set(groups.members(wide, self.kb)))

    def test_tree_node_stands_for_the_whole_group(self):
        """Дубовые доски в инвентаре — узел «Любые доски» отмечен как имеющийся."""
        tree = TechTree(self.kb, self.planner)
        nodes = tree.structure().nodes
        generic = {node.group: item for item, node in nodes.items() if node.group}
        self.assertIn("planks", generic)
        self.assertEqual(nodes[generic["planks"]].label, "Любые доски")
        state = tree.state({mc("oak_planks"): 4})
        self.assertEqual(state[generic["planks"]].status, HAVE_IT)
        self.assertEqual(state[generic["planks"]].count, 4)


if __name__ == "__main__":
    unittest.main()
