"""Проактивный канал кампании: завершение этапа и следующее поручение без обращения игрока.

Мод опрашивает GET /api/rp/ каждые несколько секунд. Здесь проверяется, что закрытый по
инвентарю этап уходит в мод наградой с репликой персонажа, а следующее поручение — отдельным
объявлением; модель заменена заглушкой, реплики — шаблонные.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import advice, campaign, challenges
from api.player_state import apply_log_to_player_state, reset_player_state
from models.log_entry import LogEntry
from orchestrator.llm import ModelUnavailable
from orchestrator.narrator import Narrator
from orchestrator.session import Session

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"

TABLE = "minecraft:crafting_table"
WOODEN = "minecraft:wooden_pickaxe"
STONE = "minecraft:stone_pickaxe"


def silent(messages):
    raise ModelUnavailable("в тестах модель не формулирует")


def snapshot(**counts):
    apply_log_to_player_state(LogEntry(
        level="INFO", event_type="inventory_snapshot",
        event_data={"counts": dict(counts), "hotbar": [], "armor": [], "offhand": []}))


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class CampaignAnnounceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.patches = [
            patch.object(challenges, "CHALLENGE_STATE_PATH", root / "challenge_state.json"),
            patch.object(campaign, "CAMPAIGN_STATE_PATH", root / "campaign_state.json"),
            patch.object(advice, "_assistant_narrator", return_value=Narrator(ask_text=silent)),
            patch.object(advice, "_assistant_session", return_value=Session()),
        ]
        for item in self.patches:
            item.start()
        reset_player_state()
        challenges.reset_challenge_state()
        campaign.reset_campaign_state()
        self.narrator = Narrator(ask_text=silent)

    def tearDown(self):
        campaign.reset_campaign_state()
        challenges.reset_challenge_state()
        for item in self.patches:
            item.stop()
        self.temp_dir.cleanup()

    def start(self):
        result = campaign.start_campaign(STONE, 1, inventory={}, persona="smith")
        self.assertTrue(result["ok"], result.get("error"))
        # Старт озвучен репликой чата: его события уже забраны.
        campaign.consume_campaign_events()

    # --- рассказчик по событиям ---

    def test_completed_stage_gets_the_persona_text_and_the_next_brief_is_queued(self):
        self.start()
        snapshot(**{TABLE: 1})
        campaign.refresh_campaign_progress()
        queued = campaign.narrate_events(self.narrator)
        self.assertEqual(queued, 1)

        pending = challenges.get_challenge_state()["pending_rewards"]
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["response"].startswith("Кузнец Торвальд:"))
        self.assertIn("Этап 1 из 3 пройден", pending[0]["response"])

        announcement = campaign.consume_announcement()
        self.assertEqual(announcement["type"], campaign.EVENT_STAGE_STARTED)
        self.assertEqual(announcement["stage"], 2)
        self.assertIn("Этап 2 из 3", announcement["response"])
        self.assertIn("Деревянная кирка", announcement["response"])
        self.assertTrue(announcement["plan_tree"])
        self.assertIsNone(campaign.consume_announcement())

        active = campaign.get_active_campaign()
        self.assertTrue(active["stages"][0]["texts"]["done"])
        self.assertTrue(active["stages"][1]["texts"]["brief"])

    def test_finished_campaign_is_announced_once(self):
        self.start()
        snapshot(**{TABLE: 1, WOODEN: 1, STONE: 1})
        campaign.refresh_campaign_progress()
        campaign.narrate_events(self.narrator)
        kinds = []
        while True:
            announcement = campaign.consume_announcement()
            if announcement is None:
                break
            kinds.append(announcement["type"])
        self.assertEqual(kinds, [campaign.EVENT_STAGE_STARTED, campaign.EVENT_STAGE_STARTED,
                                 campaign.EVENT_CAMPAIGN_COMPLETED])
        self.assertEqual(len(challenges.get_challenge_state()["pending_rewards"]), 3)

    def test_cancellation_is_not_announced(self):
        self.start()
        campaign.cancel_campaign()
        self.assertEqual(campaign.narrate_events(self.narrator), 0)
        self.assertIsNone(campaign.consume_announcement())

    # --- канал опроса ---

    async def test_poll_delivers_reward_then_brief(self):
        self.start()
        snapshot(**{TABLE: 1})

        first = await advice.get_rp_response()
        self.assertEqual(first.source, "challenge")
        self.assertTrue(first.execute)
        self.assertEqual(first.item_id, campaign.get_active_campaign()["stages"][0]["reward"])
        self.assertIn("Этап 1 из 3 пройден", first.response)

        second = await advice.get_rp_response()
        self.assertEqual(second.source, "campaign-template")
        self.assertFalse(second.execute)
        self.assertIn("Этап 2 из 3", second.response)
        self.assertEqual(second.campaign["current"], 2)
        self.assertTrue(second.plan_tree)

        third = await advice.get_rp_response()
        self.assertNotIn(third.source, ("challenge", "campaign", "campaign-template"))


if __name__ == "__main__":
    unittest.main()
