"""Проверки решателя кампании.

Закрепляют то, ради чего кампания строится решателем, а не моделью: вехи берутся из дерева
плана, идут в порядке зависимостей, каждый этап доказан от прогнозируемого инвентаря, а
награда не обесценивает путь. Языковая модель в этих проверках не участвует вовсе — это и
есть довод против «декоративного» нейрокомпонента: цепочка этапов от неё не зависит.
"""

import unittest
from pathlib import Path

from kb.graph import KnowledgeBase
from solver.campaign import (MAX_STAGES, REWARD_MAX, UNLOCKS_GOAL, UNLOCKS_MATERIAL,
                             UNLOCKS_STATION, UNLOCKS_TOOL, CampaignBuilder)
from solver.planner import CRAFT
from solver.quest import MAX_EFFORT

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"

TABLE = "minecraft:crafting_table"
FURNACE = "minecraft:furnace"
WOODEN = "minecraft:wooden_pickaxe"
STONE = "minecraft:stone_pickaxe"
IRON = "minecraft:iron_pickaxe"
DIAMOND = "minecraft:diamond_pickaxe"


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class CampaignStagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.builder = CampaignBuilder(cls.kb)

    # --- вехи ---

    def test_stone_pickaxe_from_scratch_is_three_stages(self):
        campaign = self.builder.build(STONE, 1, {})
        self.assertTrue(campaign.feasible, campaign.reason)
        self.assertEqual(campaign.targets, [TABLE, WOODEN, STONE])
        self.assertEqual([stage.unlocks for stage in campaign.stages],
                         [UNLOCKS_STATION, UNLOCKS_TOOL, UNLOCKS_GOAL])
        self.assertEqual([stage.index for stage in campaign.stages], [1, 2, 3])

    def test_milestones_explain_what_they_unlock(self):
        campaign = self.builder.build(STONE, 1, {})
        table, wooden, goal = campaign.stages
        # Верстак нужен для кирки, деревянная кирка — чтобы добыть камень для каменной.
        # Какой именно камень из тега выберет планировщик — его дело, здесь важна связь.
        self.assertIsNotNone(table.for_item)
        self.assertIn(wooden.for_item, self.kb.tag_members("minecraft:stone_tool_materials"))
        self.assertIsNone(goal.for_item)

    def test_owned_tools_and_stations_are_not_stages(self):
        campaign = self.builder.build(STONE, 1, {TABLE: 1, WOODEN: 1})
        self.assertTrue(campaign.feasible, campaign.reason)
        self.assertEqual(campaign.targets, [STONE])

    def test_hand_crafted_goal_needs_no_station(self):
        campaign = self.builder.build(TABLE, 1, {})
        self.assertTrue(campaign.feasible, campaign.reason)
        self.assertEqual(campaign.targets, [TABLE])

    def test_furnace_needs_a_crafting_table_and_a_pickaxe_first(self):
        # Восемь булыжника руками не добыть: до печи — верстак и деревянная кирка.
        campaign = self.builder.build(FURNACE, 1, {})
        self.assertEqual(campaign.targets, [TABLE, WOODEN, FURNACE])

    def test_plain_furnace_is_preferred_to_the_blast_furnace(self):
        # Обе плавят железо, но доменная печь сама требует печи: станция стоит денег.
        campaign = self.builder.build(IRON, 1, {})
        self.assertIn(FURNACE, campaign.targets)
        self.assertNotIn("minecraft:blast_furnace", campaign.targets)

    def test_diamond_pickaxe_follows_the_progression(self):
        campaign = self.builder.build(DIAMOND, 1, {})
        self.assertTrue(campaign.feasible, campaign.reason)
        targets = campaign.targets
        for item in (TABLE, WOODEN, STONE, FURNACE, IRON, DIAMOND):
            self.assertIn(item, targets)
        # Порядок зависимостей: каждая кирка после предыдущей, печь до железной кирки.
        self.assertLess(targets.index(WOODEN), targets.index(STONE))
        self.assertLess(targets.index(STONE), targets.index(IRON))
        self.assertLess(targets.index(IRON), targets.index(DIAMOND))
        self.assertLess(targets.index(FURNACE), targets.index(IRON))
        self.assertEqual(targets[-1], DIAMOND)

    def test_tool_needed_in_two_branches_is_one_stage(self):
        campaign = self.builder.build(DIAMOND, 1, {})
        self.assertEqual(len(campaign.targets), len(set(campaign.targets)))

    def test_stronger_owned_tool_skips_weaker_stages(self):
        campaign = self.builder.build(DIAMOND, 1, {IRON: 1})
        self.assertTrue(campaign.feasible, campaign.reason)
        for item in (WOODEN, STONE, IRON, FURNACE):
            self.assertNotIn(item, campaign.targets)

    # --- доказательство по траектории ---

    def test_each_stage_is_planned_from_the_projected_inventory(self):
        campaign = self.builder.build(DIAMOND, 1, {})
        for position, stage in enumerate(campaign.stages):
            self.assertTrue(stage.plan.feasible)
            crafted = {step.item for step in stage.plan.root.walk() if step.kind == CRAFT}
            earlier = {s.target for s in campaign.stages[:position]}
            # Что уже добыто на прошлых этапах, заново не изготавливается.
            self.assertFalse(crafted & earlier, f"этап {stage.index} переделывает {crafted & earlier}")

    def test_every_stage_fits_a_single_quest(self):
        campaign = self.builder.build("minecraft:netherite_pickaxe", 1, {})
        self.assertTrue(campaign.feasible, campaign.reason)
        for stage in campaign.stages:
            self.assertLessEqual(stage.effort, MAX_EFFORT, stage.target)

    def test_heavy_stage_is_split_into_a_material_stage(self):
        campaign = self.builder.build("minecraft:netherite_pickaxe", 1, {})
        self.assertIn(UNLOCKS_MATERIAL, [stage.unlocks for stage in campaign.stages])
        material = next(stage for stage in campaign.stages if stage.unlocks == UNLOCKS_MATERIAL)
        self.assertIsNotNone(material.for_item)

    def test_mined_goal_names_where_to_look(self):
        campaign = self.builder.build("minecraft:diamond", 3, {IRON: 1})
        self.assertTrue(campaign.feasible, campaign.reason)
        self.assertIsNotNone(campaign.stages[-1].source)

    def test_plan_is_deterministic(self):
        first = self.builder.build(DIAMOND, 1, {})
        second = self.builder.build(DIAMOND, 1, {})
        self.assertEqual(first.targets, second.targets)
        self.assertEqual([s.reward for s in first.stages], [s.reward for s in second.stages])

    # --- невыполнимое ---

    def test_unobtainable_goal_is_refused_with_a_reason(self):
        campaign = self.builder.build("minecraft:bedrock", 1, {})
        self.assertFalse(campaign.feasible)
        self.assertEqual(campaign.stages, ())
        self.assertIn("не достать", campaign.reason)

    def test_goal_blocked_by_a_boss_drop_is_refused(self):
        campaign = self.builder.build("minecraft:beacon", 1, {})
        self.assertFalse(campaign.feasible)
        self.assertIsNotNone(campaign.reason)


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class CampaignRewardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.builder = CampaignBuilder(cls.kb)

    def test_every_stage_has_an_allowed_reward(self):
        campaign = self.builder.build(DIAMOND, 1, {})
        for stage in campaign.stages:
            self.assertIsNotNone(stage.reward, stage.target)
            self.assertTrue(1 <= stage.reward_count <= REWARD_MAX)
            self.assertIn(stage.reward, [option.item for option in stage.reward_options])

    def test_reward_is_never_a_stage_target(self):
        campaign = self.builder.build("minecraft:torch", 8, {})
        self.assertTrue(campaign.feasible, campaign.reason)
        for stage in campaign.stages:
            self.assertNotEqual(stage.reward, "minecraft:torch")
            self.assertNotIn("minecraft:torch", [option.item for option in stage.reward_options])

    def test_reward_does_not_devalue_later_stages(self):
        # Палки понадобятся на этапе кирки, поэтому за верстак их выдавать нельзя — хлеб можно.
        builder = CampaignBuilder(self.kb, reward_pool=(("minecraft:stick", 4),
                                                        ("minecraft:bread", 2)))
        campaign = builder.build(STONE, 1, {})
        table, wooden, goal = campaign.stages
        self.assertNotIn("minecraft:stick", [option.item for option in table.reward_options])
        self.assertEqual(table.reward, "minecraft:bread")
        self.assertNotIn("minecraft:stick", [option.item for option in wooden.reward_options])

    def test_reward_grows_with_effort_but_stays_within_the_limit(self):
        campaign = self.builder.build(DIAMOND, 1, {})
        lightest = min(campaign.stages, key=lambda stage: stage.effort)
        heaviest = max(campaign.stages, key=lambda stage: stage.effort)
        self.assertLessEqual(lightest.reward_count, heaviest.reward_count)
        self.assertLessEqual(heaviest.reward_count, REWARD_MAX)

    def test_tool_reward_is_not_above_the_players_tier(self):
        builder = CampaignBuilder(self.kb, reward_pool=((IRON, 1), ("minecraft:bread", 2)))
        campaign = builder.build(STONE, 1, {})
        for stage in campaign.stages:
            self.assertNotIn(IRON, [option.item for option in stage.reward_options])


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class GoalCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.builder = CampaignBuilder(cls.kb)

    def test_beginner_is_offered_basic_tools_and_stations(self):
        candidates = self.builder.propose_goals({})
        items = [candidate.item for candidate in candidates]
        self.assertIn(TABLE, items)
        self.assertIn(FURNACE, items)
        self.assertIn(WOODEN, items)
        # Золотая кирка той же ступени, что деревянная, но дороже — её не предлагают.
        self.assertNotIn("minecraft:golden_pickaxe", items)

    def test_next_tier_is_offered_and_owned_is_not(self):
        candidates = self.builder.propose_goals({IRON: 1, TABLE: 1, FURNACE: 1})
        items = [candidate.item for candidate in candidates]
        self.assertIn(DIAMOND, items)
        for item in (WOODEN, STONE, IRON, TABLE, FURNACE):
            self.assertNotIn(item, items)

    def test_candidates_are_proven_and_bounded(self):
        for candidate in self.builder.propose_goals({}):
            self.assertTrue(1 <= candidate.stages <= MAX_STAGES)
            campaign = self.builder.build(candidate.item, 1, {})
            self.assertTrue(campaign.feasible)
            self.assertEqual(len(campaign.stages), candidate.stages)


if __name__ == "__main__":
    unittest.main()
