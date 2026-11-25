"""Адаптер Minecraft: сканер ценных блоков и учёт хранилищ → факты памяти.

Значения приходят от мода; сервер принимает только блоки из своего списка и известные базе
знаний предметы — произвольная строка из запроса в память не попадает.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import memory
from api import advice, campaign, challenges, worlds
from api import memory as memory_api
from api.challenges import reset_challenge_state
from api.player_state import apply_log_to_player_state, reset_player_state
from kb.graph import KnowledgeBase
from memory.adapters.minecraft import SCAN_BLOCKS, container, observations, scan_list
from memory.config import DEFAULTS
from memory.model import CONTAINER, OVERWORLD, RESOURCE, STALE, Position
from memory.store import MemoryStore
from models.groq_response import ChatMessage
from models.log_entry import LogEntry
from orchestrator import Pipeline, Proposal
from orchestrator import session as sessions
from orchestrator.pipeline import RECALL
from orchestrator.responder import Responder

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


def block(block_id, x, y, z, dimension="minecraft:overworld"):
    return {"id": block_id, "x": x, "y": y, "z": z, "dimension": dimension}


class AdapterWithoutKbTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(None)

    def test_scan_list_follows_settings(self):
        listed = scan_list(DEFAULTS)
        self.assertEqual((listed["radius"], listed["interval_s"], listed["enabled"]), (24, 5, True))
        self.assertIn("minecraft:diamond_ore", listed["blocks"])
        self.assertIn("minecraft:nether_portal", listed["blocks"])
        self.assertNotIn("minecraft:stone", listed["blocks"])

    def test_only_listed_blocks_are_accepted(self):
        result = observations(self.store, {"blocks": [
            block("minecraft:diamond_ore", 1, -58, 1), block("diamond_ore", 2, -58, 1),
            block("minecraft:stone", 3, 3, 3), block("minecraft:iron_ore", "x", 1, 1), "мусор"]})
        self.assertEqual((result["added"], result["skipped"]), (1, 3))
        self.assertEqual(self.store.facts(RESOURCE)[0].amount, 2)

    def test_removed_blocks_make_records_stale(self):
        observations(self.store, {"blocks": [block("minecraft:diamond_ore", 1, -58, 1)]})
        result = observations(self.store, {"blocks": [], "removed": [
            {"x": 1, "y": -58, "z": 1, "dimension": "minecraft:overworld"}]})
        self.assertEqual(result["stale"], 1)
        self.assertEqual(self.store.facts(RESOURCE, statuses=(STALE,))[0].subject,
                         "minecraft:diamond_ore")

    def test_container_needs_a_storage_block_and_coordinates(self):
        self.assertIsNone(container(self.store, {"block": "minecraft:furnace", "x": 0, "y": 0, "z": 0}))
        self.assertIsNone(container(self.store, {"block": "minecraft:chest", "x": 0}))
        fact = container(self.store, {"block": "minecraft:red_shulker_box", "x": 1, "y": 2, "z": 3,
                                      "items": {"minecraft:iron_ingot": 5, "minecraft:torch": "x"}})
        self.assertEqual(fact.contents, {"minecraft:iron_ingot": 5})


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class AdapterWithKbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def test_every_scanned_block_exists_in_the_knowledge_base(self):
        self.assertEqual(scan_list(DEFAULTS, self.kb)["blocks"], list(SCAN_BLOCKS))

    def test_labels_come_from_the_knowledge_base(self):
        store = MemoryStore(None)
        observations(store, {"blocks": [block("minecraft:deepslate_diamond_ore", 1, -58, 1)]}, self.kb)
        self.assertEqual(store.facts(RESOURCE)[0].label, "Глубинносланцевая алмазная руда")
        fact = container(store, {"block": "minecraft:chest", "x": 12, "y": 64, "z": -40,
                                 "items": {"minecraft:iron_ingot": 5, "minecraft:unobtainium": 3}},
                         self.kb)
        self.assertEqual((fact.label, fact.contents), ("Сундук", {"minecraft:iron_ingot": 5}))


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class ModEndpointsTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.pipeline_stub = Pipeline(None, self.kb)
        self.patches = [
            patch.object(worlds, "WORLDS_DIR", root / "worlds"),
            patch.object(challenges, "CHALLENGE_STATE_PATH", root / "challenge_state.json"),
            patch.object(campaign, "CAMPAIGN_STATE_PATH", root / "campaign_state.json"),
            patch.object(advice, "_assistant_pipeline", return_value=self.pipeline_stub),
        ]
        for item in self.patches:
            item.start()
        self.forget()
        worlds.enter("sp:Тест", "Тест")

    def tearDown(self):
        self.forget()
        for item in self.patches:
            item.stop()
        self.temp_dir.cleanup()

    def forget(self):
        worlds._current = None
        worlds._dialogue = []
        worlds._last_plan = {}
        memory.reset()
        sessions.replace(sessions.Session())
        advice._response_history.clear()
        advice._conversation_history.clear()
        reset_player_state()
        reset_challenge_state()
        campaign.reset_campaign_state()

    async def test_scan_list_endpoint(self):
        listed = await memory_api.get_scan_list()
        self.assertEqual(listed["radius"], 24)
        self.assertIn("minecraft:diamond_ore", listed["blocks"])

    async def test_observations_reach_the_world_memory_and_survive_a_restart(self):
        result = await memory_api.post_observations({"blocks": [
            block("minecraft:diamond_ore", -120, -58, 340), block("minecraft:diamond_ore", -121, -58, 340)]})
        self.assertEqual((result["ok"], result["added"]), (True, 1))
        self.forget()
        worlds.enter("sp:Тест", "Тест")
        fact = memory.shared().facts(RESOURCE)[0]
        self.assertEqual((fact.label, fact.amount), ("Алмазная руда", 2))

    async def test_mod_observations_go_to_minecraft_even_when_talking_about_another_game(self):
        sessions.shared().set_game("Ведьмак 3")
        await memory_api.post_observations({"blocks": [block("minecraft:diamond_ore", 1, -58, 1)]})
        self.assertEqual(len(memory.shared("Minecraft").facts(RESOURCE)), 1)
        self.assertEqual(len(memory.shared("Ведьмак 3")), 0)

    async def test_overlay_reads_minecraft_memory_while_talking_about_another_game(self):
        await memory_api.post_observations({"blocks": [block("minecraft:diamond_ore", 1, -58, 1)]})
        sessions.shared().set_game("Ведьмак 3")
        self.assertEqual((await memory_api.list_memory())["total"], 0)
        listed = await memory_api.list_memory(game="Minecraft", kind="resource")
        self.assertEqual((listed["game"], listed["total"]), ("Minecraft", 1))
        self.assertIn("Алмазная руда", listed["facts"][0]["description"])

    def test_chat_page_has_a_memory_card_that_inserts_text_safely(self):
        page = (Path(__file__).resolve().parent.parent / "static" / "chat.html").read_text(encoding="utf-8")
        self.assertIn("<h2>Память</h2>", page)
        self.assertIn('fetch("/api/memory/?"', page)
        # Описание может содержать текст с экрана: только textContent, не innerHTML.
        self.assertIn("text.textContent = fact.description", page)

    async def test_container_endpoint(self):
        result = await memory_api.post_container({"block": "minecraft:chest", "x": 12, "y": 64,
                                                  "z": -40, "dimension": "minecraft:overworld",
                                                  "items": {"minecraft:iron_ingot": 5}})
        self.assertIn("Железный слиток ×5", result["fact"]["description"])
        with self.assertRaises(Exception):
            await memory_api.post_container({"block": "minecraft:stone", "x": 1, "y": 1, "z": 1})

    async def test_disabled_memory_ignores_the_mod(self):
        with patch("memory.config.settings", return_value={**DEFAULTS, "enabled": False}):
            result = await memory_api.post_observations({"blocks": [block("minecraft:diamond_ore", 1, 1, 1)]})
            listed = await memory_api.get_scan_list()
        self.assertFalse(result["ok"])
        self.assertFalse(listed["enabled"])
        self.assertEqual(len(memory.shared()), 0)

    async def test_where_are_the_diamonds_after_a_scan(self):
        """Критерий этапа: «где алмазы» отвечает координатами."""
        apply_log_to_player_state(LogEntry(level="INFO", event_type="state", event_data={
            "dimension": "minecraft:overworld", "x": 0, "y": 64, "z": 200}))
        await memory_api.post_observations({"blocks": [block("minecraft:deepslate_diamond_ore", -120, -58, 340)]})

        class Stub:
            def extract(self, utterance, **context):
                return Proposal(RECALL, "алмазы")

        with (patch.object(advice, "_assistant_pipeline", return_value=Pipeline(Stub(), self.kb)),
              patch.object(advice, "_assistant_responder",
                           return_value=Responder(ask_text=lambda messages: ""))):
            answer = await advice.player_chat_message(ChatMessage(text="где алмазы?"))
        self.assertEqual(answer.outcome, "act")
        self.assertIn("x −120, y −58, z 340", answer.response)
        self.assertIn("на юго-запад", answer.response)
        self.assertFalse(answer.execute)


if __name__ == "__main__":
    unittest.main()
