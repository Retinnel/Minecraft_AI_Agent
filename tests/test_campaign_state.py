"""Проверки состояния кампании.

Кампания живёт поверх заданий: этап — активное задание, прогресс и награда — их механизм.
Здесь закрепляется то, что добавлено сверху: порядок этапов, переход по награде, события для
проактивного канала, отказы и сохранение. Решатель и модель не подменяются: решатель
настоящий, модели нет вовсе.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import campaign, challenges
from api.player_state import apply_log_to_player_state, reset_player_state
from api.tool_service import execute_tool_call
from models.log_entry import LogEntry
from solver.campaign import REWARD_POOL

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"

TABLE = "minecraft:crafting_table"
WOODEN = "minecraft:wooden_pickaxe"
STONE = "minecraft:stone_pickaxe"


def state_with(**counts):
    return {"kill_counts": {}, "inventory": {"counts": dict(counts)}}


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class CampaignStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.patches = [
            patch.object(challenges, "CHALLENGE_STATE_PATH", root / "challenge_state.json"),
            patch.object(campaign, "CAMPAIGN_STATE_PATH", root / "campaign_state.json"),
        ]
        for item in self.patches:
            item.start()
        reset_player_state()
        challenges.reset_challenge_state()
        campaign.reset_campaign_state()

    def tearDown(self):
        campaign.reset_campaign_state()
        challenges.reset_challenge_state()
        for item in self.patches:
            item.stop()
        self.temp_dir.cleanup()

    def start_stone_pickaxe(self, **inventory):
        result = campaign.start_campaign(STONE, 1, inventory=dict(inventory), persona="smith")
        self.assertTrue(result["ok"], result.get("error"))
        return result["campaign"]

    # --- запуск ---

    def test_start_materializes_the_first_stage_as_a_challenge(self):
        record = self.start_stone_pickaxe()
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["current"], 1)
        self.assertEqual([stage["target"] for stage in record["stages"]], [TABLE, WOODEN, STONE])
        self.assertEqual([stage["status"] for stage in record["stages"]],
                         ["active", "pending", "pending"])
        self.assertEqual(record["persona"], "smith")

        active = challenges.get_active_challenge()
        self.assertIsNotNone(active)
        self.assertEqual(active["goal_target_id"], TABLE)
        self.assertEqual(active["source"], campaign.SOURCE)
        self.assertEqual(active["id"], record["stages"][0]["challenge_id"])
        self.assertEqual(active["reward_item_id"], record["stages"][0]["reward"])

    def test_start_announces_the_first_brief(self):
        self.start_stone_pickaxe()
        events = campaign.consume_campaign_events()
        self.assertEqual([event["type"] for event in events], [campaign.EVENT_STAGE_STARTED])
        self.assertIn("Этап 1 из 3", events[0]["text"])
        self.assertIn("Верстак", events[0]["text"])
        # События забираются один раз.
        self.assertEqual(campaign.consume_campaign_events(), [])

    def test_stage_carries_plan_and_reason(self):
        record = self.start_stone_pickaxe()
        wooden = record["stages"][1]
        self.assertEqual(wooden["unlocks"], "tool")
        self.assertIn("Открывает добычу", campaign.stage_reason(wooden))
        self.assertTrue(wooden["plan_tree"])
        self.assertTrue(wooden["reward_options"])

    # --- продвижение ---

    def test_completed_stage_is_rewarded_and_the_next_one_starts(self):
        record = self.start_stone_pickaxe()
        campaign.consume_campaign_events()

        state = campaign.refresh_campaign_progress(state_with(**{TABLE: 1}))
        active = state["active"]
        self.assertEqual(active["current"], 2)
        self.assertEqual(active["stages"][0]["status"], "completed")
        self.assertEqual(active["stages"][1]["status"], "active")

        challenge = challenges.get_active_challenge()
        self.assertEqual(challenge["goal_target_id"], WOODEN)

        pending = challenges.get_challenge_state()["pending_rewards"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["challenge_id"], record["stages"][0]["challenge_id"])
        self.assertEqual(pending[0]["item_id"], record["stages"][0]["reward"])
        self.assertIn("Этап 1 из 3 пройден", pending[0]["response"])

        events = campaign.consume_campaign_events()
        self.assertEqual([event["type"] for event in events],
                         [campaign.EVENT_STAGE_COMPLETED, campaign.EVENT_STAGE_STARTED])
        self.assertEqual(events[0]["reward"], record["stages"][0]["reward"])
        self.assertEqual(events[1]["stage"], 2)

    def test_campaign_runs_to_completion(self):
        self.start_stone_pickaxe()
        campaign.refresh_campaign_progress(state_with(**{TABLE: 1}))
        campaign.refresh_campaign_progress(state_with(**{TABLE: 1, WOODEN: 1}))
        state = campaign.refresh_campaign_progress(state_with(**{TABLE: 1, WOODEN: 1, STONE: 1}))

        self.assertIsNone(state["active"])
        self.assertIsNone(challenges.get_active_challenge())
        finished = state["history"][-1]
        self.assertEqual(finished["status"], "completed")
        self.assertTrue(all(stage["status"] == "completed" for stage in finished["stages"]))
        self.assertEqual(len(challenges.get_challenge_state()["pending_rewards"]), 3)

        kinds = [event["type"] for event in campaign.consume_campaign_events()]
        self.assertEqual(kinds[-1], campaign.EVENT_CAMPAIGN_COMPLETED)

    def test_stage_already_satisfied_is_skipped_in_the_same_refresh(self):
        # Пока игрок делал верстак, он успел и кирку: второй этап закрывается сразу.
        self.start_stone_pickaxe()
        state = campaign.refresh_campaign_progress(state_with(**{TABLE: 1, WOODEN: 1}))
        self.assertEqual(state["active"]["current"], 3)
        self.assertEqual([stage["status"] for stage in state["active"]["stages"]],
                         ["completed", "completed", "active"])

    def test_progress_is_visible_before_completion(self):
        result = campaign.start_campaign("minecraft:cobblestone", 8,
                                         inventory={TABLE: 1, WOODEN: 1})
        self.assertTrue(result["ok"], result.get("error"))
        state = campaign.refresh_campaign_progress(state_with(**{"minecraft:cobblestone": 3}))
        stage = campaign.current_stage(state["active"])
        self.assertEqual(stage["progress_count"], 3)
        self.assertEqual(stage["status"], "active")

    def test_inventory_snapshot_event_drives_progress(self):
        self.start_stone_pickaxe()
        apply_log_to_player_state(LogEntry(
            level="INFO", event_type="inventory_snapshot",
            event_data={"counts": {TABLE: 1}, "hotbar": [], "armor": [], "offhand": []},
        ))
        state = campaign.refresh_campaign_progress()
        self.assertEqual(state["active"]["current"], 2)

    def test_narrator_text_replaces_the_template(self):
        self.start_stone_pickaxe()
        campaign.set_stage_text(1, "done", "Верстак готов, ученик. Держи хлеб.")
        campaign.refresh_campaign_progress(state_with(**{TABLE: 1}))
        pending = challenges.get_challenge_state()["pending_rewards"]
        self.assertEqual(pending[0]["response"], "Верстак готов, ученик. Держи хлеб.")
        done = next(e for e in campaign.consume_campaign_events()
                    if e["type"] == campaign.EVENT_STAGE_COMPLETED)
        self.assertEqual(done["text"], "Верстак готов, ученик. Держи хлеб.")

    # --- отказы ---

    def test_second_campaign_is_refused_while_one_is_active(self):
        self.start_stone_pickaxe()
        result = campaign.start_campaign("minecraft:furnace", 1, inventory={})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "active_exists")

    def test_campaign_is_refused_while_a_plain_challenge_is_active(self):
        created = challenges.create_challenge(goal_type="collect", goal_target_id="minecraft:oak_log",
                                              goal_count=4, reward_item_id="minecraft:bread",
                                              reward_count=1)
        self.assertTrue(created["ok"])
        result = campaign.start_campaign(STONE, 1, inventory={})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "challenge_active")

    def test_unreachable_goal_is_refused_with_the_solver_reason(self):
        result = campaign.start_campaign("minecraft:bedrock", 1, inventory={})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "infeasible")
        self.assertIn("не достать", result["error"])
        self.assertIsNone(campaign.get_active_campaign())

    # --- отмена ---

    def test_cancel_closes_the_stage_challenge_too(self):
        record = self.start_stone_pickaxe()
        result = campaign.cancel_campaign()
        self.assertTrue(result["ok"])
        self.assertIsNone(campaign.get_active_campaign())
        self.assertIsNone(challenges.get_active_challenge())
        history = campaign.get_campaign_state()["history"]
        self.assertEqual(history[-1]["id"], record["id"])
        self.assertEqual(history[-1]["status"], "cancelled")

    def test_cancelling_the_challenge_directly_cancels_the_campaign(self):
        record = self.start_stone_pickaxe()
        challenges.cancel_active_challenge(record["stages"][0]["challenge_id"])
        state = campaign.refresh_campaign_progress()
        self.assertIsNone(state["active"])
        self.assertEqual(state["history"][-1]["status"], "cancelled")

    # --- сохранение ---

    def test_state_survives_a_reload(self):
        record = self.start_stone_pickaxe()
        campaign.refresh_campaign_progress(state_with(**{TABLE: 1}))
        campaign.reset_campaign_state()
        self.assertIsNone(campaign.get_active_campaign())
        campaign.load_campaign_state()
        restored = campaign.get_active_campaign()
        self.assertEqual(restored["id"], record["id"])
        self.assertEqual(restored["current"], 2)

    # --- награды и кандидаты ---

    def test_every_pool_reward_can_actually_be_issued(self):
        # Награду выдаёт старый путь по точному идентификатору; пул обязан через него проходить.
        for item, _ in REWARD_POOL:
            with self.subTest(item=item):
                result = execute_tool_call("give_item", {"item_query": item, "count": 2})
                self.assertTrue(result["ok"], result.get("error"))
                self.assertEqual(result["item_id"], item)

    def test_candidates_are_labelled(self):
        candidates = campaign.propose_goals({})
        self.assertTrue(candidates)
        self.assertIn(TABLE, [candidate["item"] for candidate in candidates])
        self.assertTrue(all(candidate["label"] for candidate in candidates))

    def test_prompt_context_names_the_current_stage(self):
        self.start_stone_pickaxe()
        context = campaign.get_campaign_prompt_context()
        self.assertIn("Текущий этап 1", context)
        self.assertIn("Верстак", context)


if __name__ == "__main__":
    unittest.main()
