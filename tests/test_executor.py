"""Проверки исполнителя плана («Сделай сам»).

Цикл «задача → исполнение → пересчёт от инвентаря» прогоняется на упрощённой игре
(`tests/game_sim.py`): каждая выданная задача должна быть исполнима в момент выдачи,
а цикл — приходить к цели. Отдельно — отказы: охота, Незер, трудоёмкость, станции.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from game_sim import SimGame  # noqa: E402
from kb.graph import KnowledgeBase  # noqa: E402
from solver.executor import (TASK_CRAFT, TASK_KINDS, TASK_MINE, TASK_PLACE, TASK_SMELT,  # noqa: E402
                             TASK_TAKE, Context, ExecutionPlanner, Task, Waiting)
from solver.planner import Planner  # noqa: E402

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"
STONE_PICKAXE = "minecraft:stone_pickaxe"
HALF_WAY = {"minecraft:stick": 2, "minecraft:wooden_pickaxe": 1}


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class ExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def setUp(self):
        self.executor = ExecutionPlanner(self.kb)

    def run_to_goal(self, goal, count=1, inventory=None, chest=None, limit=60, **context):
        game = SimGame(self.kb, inventory, chest)
        for _ in range(limit):
            ctx = Context(inventory=dict(game.inventory), stations=tuple(game.stations),
                          storage=dict(game.chest), **context)
            _, task = self.executor.next_task(goal, count, ctx)
            if task is None:
                break
            self.assertIsInstance(task, Task)
            game.apply(task.to_dict())
        return game

    # --- цикл до цели ---

    def test_stone_pickaxe_from_empty_inventory(self):
        game = self.run_to_goal(STONE_PICKAXE)
        self.assertGreaterEqual(game.inventory.get(STONE_PICKAXE, 0), 1)
        kinds = [task["kind"] for task in game.done]
        # Верстак ставится до крафта на нём, деревянная кирка — до булыжника.
        self.assertLess(kinds.index(TASK_PLACE),
                        [task["item"] for task in game.done].index("minecraft:wooden_pickaxe"))
        mined = [task["item"] for task in game.done if task["kind"] == TASK_MINE]
        self.assertIn("minecraft:cobblestone", mined)
        self.assertLess([task["item"] for task in game.done].index("minecraft:wooden_pickaxe"),
                        [task["item"] for task in game.done].index("minecraft:cobblestone"))
        self.assertLessEqual(len(game.done), 15)

    def test_every_task_kind_is_whitelisted_and_has_no_command(self):
        game = self.run_to_goal("minecraft:iron_pickaxe")
        self.assertGreaterEqual(game.inventory.get("minecraft:iron_pickaxe", 0), 1)
        for task in game.done:
            self.assertIn(task["kind"], TASK_KINDS)
            self.assertNotIn("command", task)
            for value in task.values():
                if isinstance(value, str):
                    self.assertFalse(value.startswith("/"), value)

    def test_furnace_is_built_and_fuelled_for_smelting(self):
        game = self.run_to_goal("minecraft:iron_ingot", 3,
                                {"minecraft:raw_iron": 3, "minecraft:stone_pickaxe": 1})
        self.assertEqual(game.inventory.get("minecraft:iron_ingot"), 3)
        smelt = [task for task in game.done if task["kind"] == TASK_SMELT]
        self.assertEqual(len(smelt), 1)
        self.assertEqual(smelt[0]["input"], "minecraft:raw_iron")
        self.assertTrue(smelt[0]["fuel"])
        self.assertIn("furnace", game.stations)

    def test_inventory_shortens_the_run(self):
        full = self.run_to_goal(STONE_PICKAXE)
        short = self.run_to_goal(STONE_PICKAXE, inventory=dict(HALF_WAY))
        self.assertLess(len(short.done), len(full.done))

    def test_goal_in_inventory_needs_nothing(self):
        _, task = self.executor.next_task(STONE_PICKAXE, 1,
                                          Context(inventory={STONE_PICKAXE: 1}))
        self.assertIsNone(task)

    def test_craft_task_carries_recipe_and_station(self):
        _, task = self.executor.next_task(
            STONE_PICKAXE, 1, Context(inventory={**HALF_WAY, "minecraft:cobblestone": 3},
                                      stations=("crafting_table",)))
        self.assertEqual(task.kind, TASK_CRAFT)
        self.assertEqual(task.recipe_id, "minecraft:stone_pickaxe")
        self.assertEqual(task.station, "crafting_table")
        self.assertEqual(task.runs, 1)

    def test_station_in_inventory_is_placed_first(self):
        _, task = self.executor.next_task(
            STONE_PICKAXE, 1, Context(inventory={**HALF_WAY, "minecraft:cobblestone": 3,
                                                 "minecraft:crafting_table": 1}))
        self.assertEqual(task.kind, TASK_PLACE)
        self.assertEqual(task.item, "minecraft:crafting_table")

    # --- память мира ---

    def test_take_from_remembered_chest(self):
        ctx = Context(inventory=dict(HALF_WAY), stations=("crafting_table",),
                      storage={"minecraft:cobblestone": 3},
                      container_of=lambda item: ({"x": 1, "y": 64, "z": 2}, "сундук в 5 блоках"))
        _, task = self.executor.next_task(STONE_PICKAXE, 1, ctx)
        self.assertEqual(task.kind, TASK_TAKE)
        self.assertEqual(task.count, 3)
        self.assertEqual(task.position, {"x": 1, "y": 64, "z": 2})
        game = self.run_to_goal(STONE_PICKAXE, inventory=dict(HALF_WAY),
                                chest={"minecraft:cobblestone": 3})
        self.assertNotIn(TASK_MINE, [task["kind"] for task in game.done
                                     if task["item"] == "minecraft:cobblestone"])

    def test_mine_task_gets_remembered_position(self):
        ctx = Context(inventory=dict(HALF_WAY), stations=("crafting_table",),
                      block_at=lambda blocks: ({"x": 5, "y": 60, "z": 5}, "камень в 12 блоках")
                      if "minecraft:stone" in blocks else None)
        _, task = self.executor.next_task(STONE_PICKAXE, 1, ctx)
        self.assertEqual(task.kind, TASK_MINE)
        self.assertEqual(task.position, {"x": 5, "y": 60, "z": 5})
        self.assertEqual(task.where, "камень в 12 блоках")

    # --- без Baritone и обходы ---

    def test_without_baritone_mining_becomes_waiting(self):
        _, task = self.executor.next_task(
            STONE_PICKAXE, 1, Context(inventory=dict(HALF_WAY), stations=("crafting_table",),
                                      can_mine=False))
        self.assertIsInstance(task, Waiting)
        self.assertEqual(task.item, "minecraft:cobblestone")
        self.assertEqual(task.count, 3)

    def test_without_baritone_crafting_still_runs(self):
        _, task = self.executor.next_task(
            STONE_PICKAXE, 1, Context(inventory={**HALF_WAY, "minecraft:cobblestone": 3},
                                      stations=("crafting_table",), can_mine=False))
        self.assertIsInstance(task, Task)
        self.assertEqual(task.kind, TASK_CRAFT)

    def test_excluded_source_is_not_offered(self):
        _, task = self.executor.next_task(
            STONE_PICKAXE, 1, Context(inventory=dict(HALF_WAY), stations=("crafting_table",),
                                      excluded=("minecraft:stone",)))
        self.assertEqual(task.kind, TASK_MINE)
        self.assertNotIn("minecraft:stone", task.blocks)
        self.assertTrue(task.blocks)

    def test_unnatural_blocks_are_never_mined(self):
        game = self.run_to_goal("minecraft:torch", 4)
        for task in game.done:
            for block in task.get("blocks") or []:
                self.assertNotIn("stripped", block)
                self.assertNotIn("infested", block)

    def test_logs_of_any_tree_are_accepted(self):
        _, task = self.executor.next_task(STONE_PICKAXE, 1, Context(inventory={}))
        self.assertEqual(task.kind, TASK_MINE)
        self.assertIn("minecraft:oak_log", task.blocks)
        self.assertIn("minecraft:spruce_log", task.blocks)

    # --- отказы ---

    def check(self, goal, inventory=None):
        return self.executor.check(self.executor.plan(goal, 1, Context(inventory=inventory or {})))

    def test_simple_goals_pass_the_check(self):
        for goal in (STONE_PICKAXE, "minecraft:furnace", "minecraft:iron_pickaxe"):
            with self.subTest(goal=goal):
                self.assertEqual(self.check(goal), [])

    def test_hunting_is_refused(self):
        problems = self.check("minecraft:leather_chestplate")
        self.assertTrue(any("охота" in problem for problem in problems), problems)

    def test_nether_is_refused(self):
        problems = self.check("minecraft:quartz")
        self.assertTrue(any("Незер" in problem for problem in problems), problems)

    def test_deep_ores_are_refused(self):
        # Сценарий из спецификации: алмазная кирка — отказ, потому что опасно и глубоко.
        problems = self.check("minecraft:diamond_pickaxe")
        self.assertTrue(any("на глубине" in problem for problem in problems), problems)
        self.assertEqual(self.check("minecraft:iron_ingot"), [])

    def test_deepslate_variants_only_when_nothing_else(self):
        _, task = self.executor.next_task(
            "minecraft:iron_ingot", 1, Context(inventory={"minecraft:stone_pickaxe": 1}))
        self.assertEqual(task.kind, TASK_MINE)
        self.assertIn("minecraft:iron_ore", task.blocks)
        self.assertNotIn("minecraft:deepslate_iron_ore", task.blocks)

    def test_effort_limit(self):
        problems = self.check("minecraft:netherite_ingot")
        self.assertTrue(any("трудоёмкость" in problem for problem in problems), problems)
        executor = ExecutionPlanner(self.kb, max_effort=5)
        plan = executor.plan(STONE_PICKAXE, 1, Context(inventory={}))
        self.assertTrue(executor.check(plan))
        self.assertEqual(executor.check(plan, effort_limit=False), [])

    def test_disabled_task_kind_is_refused(self):
        executor = ExecutionPlanner(self.kb, allowed_kinds=(TASK_MINE, TASK_CRAFT, TASK_PLACE))
        plan = executor.plan("minecraft:iron_ingot", 1,
                             Context(inventory={"minecraft:raw_iron": 1}))
        self.assertTrue(any("smelt" in problem for problem in executor.check(plan)))


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class PlannerExtensionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.planner = Planner(cls.kb)

    def test_steps_remember_recipe(self):
        plan = self.planner.plan(STONE_PICKAXE, 1, {})
        self.assertEqual(plan.root.recipe, "stone_pickaxe")
        for step in plan.root.walk():
            if step.kind == "craft":
                self.assertTrue(step.recipe, step.item)

    def test_excluded_sources_change_the_route(self):
        plan = self.planner.plan("minecraft:cobblestone", 3, {"minecraft:wooden_pickaxe": 1},
                                 excluded_sources=["minecraft:stone"])
        sources = [step.source for step in plan.root.walk() if step.kind == "mine"]
        self.assertNotIn("minecraft:stone", sources)
        # Без исключений — обычный путь, и прежние вызовы не задеты.
        plan = self.planner.plan("minecraft:cobblestone", 3, {"minecraft:wooden_pickaxe": 1})
        self.assertTrue(plan.feasible)


if __name__ == "__main__":
    unittest.main()
