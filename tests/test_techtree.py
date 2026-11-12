"""Древо развития: строение выводится из онтологии, состояние — из инвентаря."""

import unittest
from pathlib import Path

from api.tree import compact_payload, icon_bytes, tree_payload
from kb.graph import KnowledgeBase
from solver.planner import MINE, Planner
from solver.techtree import (HAVE_IT, INGREDIENT, LOCKED, OPEN, READY, STATION, TOOL, TechTree)

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


def mc(name):
    return f"minecraft:{name}"


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class TechTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.tree = TechTree(cls.kb, Planner(cls.kb))
        cls.structure = cls.tree.structure()
        cls.edges = {(edge.source, edge.target): edge.kind for edge in cls.structure.edges}

    def test_whole_showcase_is_plannable(self):
        self.assertEqual(self.structure.skipped, [])
        for item in self.tree.showcase:
            self.assertTrue(self.structure.nodes[item].showcase, item)

    def test_intermediate_nodes_come_from_plans_not_from_the_showcase(self):
        for item in ("stick", "cobblestone", "iron_ingot", "diamond"):
            node = self.structure.nodes[mc(item)]
            self.assertFalse(node.showcase, item)

    def test_edges_carry_the_kind_of_dependency(self):
        self.assertEqual(self.edges[(mc("stick"), mc("wooden_pickaxe"))], INGREDIENT)
        self.assertEqual(self.edges[(mc("wooden_pickaxe"), mc("cobblestone"))], TOOL)
        self.assertEqual(self.edges[(mc("crafting_table"), mc("wooden_pickaxe"))], STATION)
        self.assertEqual(self.edges[(mc("furnace"), mc("iron_ingot"))], STATION)

    def test_tier_follows_the_pickaxe_requirement(self):
        tiers = {name: self.structure.nodes[mc(name)].tier for name in (
            "wooden_pickaxe", "cobblestone", "stone_pickaxe", "iron_ingot", "iron_pickaxe",
            "diamond", "diamond_pickaxe", "obsidian", "netherite_pickaxe", "shield")}
        self.assertEqual(tiers["wooden_pickaxe"], 0)
        self.assertEqual(tiers["cobblestone"], 1)
        self.assertEqual(tiers["stone_pickaxe"], 1)
        self.assertEqual(tiers["iron_ingot"], 2)
        self.assertEqual(tiers["iron_pickaxe"], 2)
        self.assertEqual(tiers["shield"], 2)
        self.assertEqual(tiers["diamond"], 3)
        self.assertEqual(tiers["diamond_pickaxe"], 3)
        self.assertEqual(tiers["obsidian"], 4)
        self.assertEqual(tiers["netherite_pickaxe"], 4)

    def test_built_blocks_are_not_sources(self):
        # Обсидиан добывается из обсидиана, а не из эндер-сундука; книга делается, а не
        # выбивается из книжной полки; редстоун — из руды, а не из проводки.
        self.assertEqual(self.structure.nodes[mc("obsidian")].source, mc("obsidian"))
        self.assertNotEqual(self.structure.nodes[mc("book")].how, MINE)
        self.assertEqual(self.structure.nodes[mc("redstone")].source, mc("redstone_ore"))

    def test_empty_inventory_opens_only_the_first_tier(self):
        states = self.tree.state({})
        self.assertEqual(states[mc("birch_log")].status, READY)
        self.assertEqual(states[mc("wooden_pickaxe")].status, OPEN)
        self.assertEqual(states[mc("cobblestone")].status, LOCKED)
        self.assertEqual(states[mc("diamond_pickaxe")].status, LOCKED)
        self.assertFalse(any(state.status == HAVE_IT for state in states.values()))

    def test_inventory_lights_the_tree_up(self):
        states = self.tree.state({mc("stone_pickaxe"): 1, mc("crafting_table"): 1,
                                  mc("furnace"): 1, mc("iron_ingot"): 8, mc("stick"): 2})
        self.assertEqual(states[mc("stone_pickaxe")].status, HAVE_IT)
        self.assertEqual(states[mc("iron_ingot")].count, 8)
        # Нагрудник и кирка собираются из того, что лежит, без единой добычи.
        self.assertEqual(states[mc("iron_chestplate")].status, READY)
        self.assertEqual(states[mc("iron_pickaxe")].status, READY)
        # Железная руда добывается каменной киркой, которая уже есть.
        self.assertEqual(states[mc("iron_ore")].status, READY)
        # Алмаз ждёт железной кирки.
        self.assertEqual(states[mc("diamond")].status, LOCKED)

    def test_ready_needs_the_station_too(self):
        without = self.tree.state({mc("iron_ingot"): 8})
        self.assertNotEqual(without[mc("iron_chestplate")].status, READY)

    def test_made_tool_is_reused_within_a_plan(self):
        # Слиток требует и древних обломков (алмазная кирка), и золота (железная): кирки
        # нижних ступеней нужны обеим ветвям, но делается каждая один раз.
        plan = Planner(self.kb).plan(mc("netherite_ingot"), 1, {})
        made = [step.item for step in plan.actions() if step.item.endswith("_pickaxe")]
        self.assertEqual(len(made), len(set(made)), made)


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class TreePayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kb = KnowledgeBase.load(KB_PATH)
        cls.tree = TechTree(kb, Planner(kb))

    def test_payload_has_tiers_nodes_edges_and_counts(self):
        payload = tree_payload(self.tree, {mc("wooden_pickaxe"): 1})
        self.assertEqual(payload["rank"], 0)
        self.assertEqual(payload["open_tier"], 1)
        self.assertEqual([tier["index"] for tier in payload["tiers"]], [0, 1, 2, 3, 4])
        self.assertEqual(payload["tiers"][1]["label"], "Деревянная кирка")
        ids = {node["id"] for node in payload["nodes"]}
        self.assertTrue(all(edge["source"] in ids and edge["target"] in ids
                            for edge in payload["edges"]))
        self.assertEqual(sum(payload["counts"].values()), len(payload["nodes"]))
        pickaxe = next(node for node in payload["nodes"] if node["id"] == mc("wooden_pickaxe"))
        self.assertEqual(pickaxe["status"], HAVE_IT)
        self.assertEqual(pickaxe["status_label"], "есть в инвентаре")

    def test_compact_payload_keeps_only_the_showcase(self):
        payload = compact_payload(self.tree, {})
        listed = [node["id"] for tier in payload["tiers"] for node in tier["nodes"]]
        self.assertIn(mc("diamond_pickaxe"), listed)
        self.assertNotIn(mc("stick"), listed)
        self.assertTrue(payload["tiers"][0]["open"])
        self.assertFalse(payload["tiers"][-1]["open"])

    def test_icon_name_is_not_a_path(self):
        self.assertIsNone(icon_bytes("../../secret"))
        self.assertIsNone(icon_bytes("no_such_item_here"))


if __name__ == "__main__":
    unittest.main()
