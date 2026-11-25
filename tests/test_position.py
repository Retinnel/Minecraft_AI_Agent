"""Позиция игрока: координаты в событиях мода, место гибели, отдельный мир для другой игры.

Координаты ставит система по сообщениям мода. Пересказ, потерявший число, заменяется
текстом фактов — это проверяется здесь на разборе смерти.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import advice, campaign, challenges, player_state, worlds
from api.challenges import reset_challenge_state
from api.player_state import (apply_log_to_player_state, current_position, get_player_state,
                              progress_snapshot, reset_player_state, restore_progress)
from kb.graph import KnowledgeBase
from memory.model import NETHER, OVERWORLD, Position
from models.log_entry import LogEntry
from orchestrator import Pipeline, Proposal
from orchestrator import session as sessions
from orchestrator.llm import ModelUnavailable
from orchestrator.responder import TEMPLATE, MODEL, Responder, keeps_numbers, numbers_in
from orchestrator.session import Session

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


def log(event_type, event_data):
    entry = LogEntry(level="INFO", event_type=event_type, event_data=event_data)
    apply_log_to_player_state(entry)
    return entry


class PositionModelTests(unittest.TestCase):
    def test_parse_requires_all_three_coordinates(self):
        self.assertIsNone(Position.parse({"x": 1, "y": 2}))
        self.assertIsNone(Position.parse({"x": "a", "y": 2, "z": 3}))
        self.assertIsNone(Position.parse({"x": True, "y": 2, "z": 3}))
        self.assertIsNone(Position.parse({"x": 1e12, "y": 2, "z": 3}))
        self.assertIsNone(Position.parse("x 1 y 2 z 3"))

    def test_parse_floors_and_defaults_to_the_overworld(self):
        position = Position.parse({"x": -0.5, "y": 64.9, "z": 10})
        self.assertEqual(position, Position(OVERWORLD, -1, 64, 10))
        self.assertEqual(Position.parse({"x": 0, "y": 0, "z": 0, "dimension": "the_nether"}).dimension,
                         NETHER)

    def test_description_uses_a_typographic_minus(self):
        position = Position(OVERWORLD, -120, -58, 340)
        self.assertEqual(position.describe(), "x −120, y −58, z 340, Верхний мир")
        self.assertEqual(position.describe(with_dimension=False), "x −120, y −58, z 340")

    def test_distance_and_direction(self):
        here = Position(OVERWORLD, 0, 64, 0)
        self.assertEqual(Position(OVERWORLD, 0, 10, 100).distance(here), 100)
        self.assertEqual(Position(OVERWORLD, 0, 64, 100).direction_from(here), "на юг")
        self.assertEqual(Position(OVERWORLD, 0, 64, -100).direction_from(here), "на север")
        self.assertEqual(Position(OVERWORLD, 100, 64, 0).direction_from(here), "на восток")
        self.assertEqual(Position(OVERWORLD, -100, 64, 100).direction_from(here), "на юго-запад")
        self.assertEqual(Position(OVERWORLD, -100, 64, 100).relative(here),
                         "около 141 блоков на юго-запад")
        self.assertEqual(Position(OVERWORLD, 3, 64, 2).relative(here), "совсем рядом")

    def test_no_distance_between_dimensions(self):
        here = Position(OVERWORLD, 0, 64, 0)
        there = Position(NETHER, 10, 64, 10)
        self.assertIsNone(there.distance(here))
        self.assertIsNone(there.relative(here))


class PlayerPositionTests(unittest.TestCase):
    def setUp(self):
        reset_player_state()

    def tearDown(self):
        reset_player_state()

    def test_state_event_updates_the_last_position(self):
        self.assertIsNone(current_position())
        log("state", {"health": 20, "food": 20, "dimension": "minecraft:overworld",
                      "x": 12, "y": 64, "z": -40})
        self.assertEqual(current_position(), Position(OVERWORLD, 12, 64, -40))
        # Состояние без координат (старый мод) позицию не стирает.
        log("state", {"health": 20, "food": 20})
        self.assertEqual(current_position(), Position(OVERWORLD, 12, 64, -40))

    def test_death_keeps_its_place(self):
        log("death", {"cause": "lava", "dimension": "minecraft:overworld",
                      "x": -30, "y": -50, "z": 115})
        death = get_player_state()["last_death"]
        self.assertEqual(death["position"], {"dimension": OVERWORLD, "x": -30, "y": -50, "z": 115})

    def test_death_without_coordinates_has_no_place(self):
        log("death", {"cause": "lava"})
        self.assertIsNone(get_player_state()["last_death"]["position"])

    def test_position_survives_a_restart(self):
        log("state", {"x": 1, "y": 2, "z": 3, "dimension": "minecraft:the_nether"})
        saved = progress_snapshot()
        reset_player_state()
        restore_progress(saved)
        self.assertEqual(current_position(), Position(NETHER, 1, 2, 3))


class NumbersTests(unittest.TestCase):
    def test_numbers_are_read_with_any_minus(self):
        self.assertEqual(numbers_in("x −120, y -58, z 340"), [-120, -58, 340])

    def test_lost_or_changed_coordinate_is_detected(self):
        self.assertTrue(keeps_numbers("Ты погиб на x −30, y −50, z 115.", [-30, -50, 115]))
        self.assertFalse(keeps_numbers("Ты погиб на x 30, y −50, z 115.", [-30, -50, 115]))
        self.assertFalse(keeps_numbers("Ты погиб в лаве.", [-30, -50, 115]))

    def test_phrase_falls_back_to_facts_when_a_number_is_lost(self):
        facts = "Место гибели: x −30, y −50, z 115, Верхний мир. Причина смерти: лава."
        responder = Responder(ask_text=lambda messages: "Ты сгорел в лаве, будь осторожнее.")
        reply = responder.phrase(facts, numbers=[-30, -50, 115])
        self.assertEqual((reply.text, reply.source), (facts, TEMPLATE))
        responder = Responder(ask_text=lambda messages: "Сгорел в лаве на x -30, y -50, z 115.")
        self.assertEqual(responder.phrase(facts, numbers=[-30, -50, 115]).source, MODEL)


class StubIntent:
    def extract(self, utterance, **context):
        return Proposal("none")


def silent(messages):
    raise ModelUnavailable("в тестах модель не формулирует")


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class DeathPlaceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = Pipeline(StubIntent(), KnowledgeBase.load(KB_PATH))

    def setUp(self):
        advice.logs_db.clear()
        advice._processed_log_ids.clear()
        advice._death_analyzer_instance = None
        reset_player_state()
        self.patches = [
            patch.object(advice, "_assistant_pipeline", return_value=self.pipeline),
            patch.object(advice, "_assistant_responder", return_value=Responder(ask_text=silent)),
            patch.object(advice, "_assistant_session", return_value=Session()),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        advice.logs_db.clear()
        advice._death_analyzer_instance = None
        reset_player_state()

    async def test_postmortem_names_the_place(self):
        advice.logs_db.append(log("death", {"cause": "lava", "dimension": "minecraft:overworld",
                                            "x": -30, "y": -50, "z": 115}))
        reply = await advice.get_rp_response()
        self.assertIn("Место гибели: x −30, y −50, z 115, Верхний мир.", reply.response)
        self.assertTrue(any("место гибели" in line for line in reply.explanation))


class GameWorldTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.patches = [
            patch.object(worlds, "WORLDS_DIR", root / "worlds"),
            patch.object(challenges, "CHALLENGE_STATE_PATH", root / "challenge_state.json"),
            patch.object(campaign, "CAMPAIGN_STATE_PATH", root / "campaign_state.json"),
        ]
        for item in self.patches:
            item.start()
        self.forget()

    def tearDown(self):
        self.forget()
        for item in self.patches:
            item.stop()
        self.temp_dir.cleanup()

    def forget(self):
        worlds._current = None
        worlds._dialogue = []
        worlds._last_plan = {}
        sessions.replace(sessions.Session())
        reset_player_state()
        reset_challenge_state()
        campaign.reset_campaign_state()

    async def test_nothing_is_created_without_a_world(self):
        self.assertIsNone(worlds.ensure_game_world("Ведьмак 3"))
        self.assertFalse((Path(self.temp_dir.name) / "worlds").exists())

    async def test_switching_game_creates_its_own_world(self):
        worlds.enter("sp:Тест", "Тест")
        await advice.player_chat_message(advice.ChatMessage(text="я играю в Ведьмак 3"))
        listed = {world["id"]: world for world in worlds.listing()}
        self.assertIn("game:ведьмак 3", listed)
        self.assertEqual(listed["game:ведьмак 3"]["name"], "Ведьмак 3")
        # Текущим остаётся мир Minecraft: разговор и его история не переезжают.
        self.assertEqual(worlds.current()["id"], "sp:Тест")

    async def test_memory_lives_in_the_world_of_the_game(self):
        worlds.enter("sp:Тест", "Тест")
        own = worlds.memory_dir("Minecraft")
        other = worlds.memory_dir("Ведьмак 3")
        self.assertEqual(own, Path(worlds.current()["dir"]))
        self.assertNotEqual(other, own)
        self.assertEqual(worlds.memory_dir("ведьмак 3"), other)
        # Из другого мира Minecraft память той игры та же.
        worlds.enter("sp:Другой", "Другой")
        self.assertEqual(worlds.memory_dir("Ведьмак 3"), other)

    async def test_entering_a_game_world_talks_about_that_game(self):
        worlds.enter("sp:Тест", "Тест")
        worlds.ensure_game_world("Elden Ring")
        state = worlds.enter("game:elden ring")
        self.assertEqual(state["game"], "Elden Ring")
        self.assertEqual(worlds.memory_dir("Elden Ring"), Path(worlds.current()["dir"]))

    async def test_minecraft_never_gets_a_game_world(self):
        worlds.enter("sp:Тест", "Тест")
        self.assertIsNone(worlds.ensure_game_world("minecraft"))
        self.assertEqual(len(worlds.listing()), 1)


if __name__ == "__main__":
    unittest.main()
