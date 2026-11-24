"""Миры: у каждого своя история и своё состояние, и они переживают перезапуск.

Перезапуск сервера имитируется обнулением состояния процесса и повторным входом в мир:
всё, что вернулось, прочитано с диска.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import advice, campaign, challenges, player_state, worlds
from api.challenges import reset_challenge_state
from api.player_state import reset_player_state
from kb.graph import KnowledgeBase
from models.groq_response import ChatMessage
from models.log_entry import LogEntry
from orchestrator import Pipeline, Proposal
from orchestrator import session as sessions
from orchestrator.responder import Responder

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


class Scripted:
    """Заглушка модели: отвечает намерением, заданным для реплики."""

    def __init__(self, answers):
        self.answers = answers

    def extract(self, utterance, **context):
        return self.answers.get(utterance, Proposal("none"))


class WorldStateCase(unittest.IsolatedAsyncioTestCase):
    """Общая подготовка: миры пишутся во временную папку, состояние процесса чистое."""

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
        """То же, что остановка процесса: в памяти ничего не остаётся."""
        worlds._current = None
        worlds._dialogue = []
        worlds._last_plan = {}
        sessions.replace(sessions.Session())
        advice._response_history.clear()
        advice._conversation_history.clear()
        reset_player_state()
        reset_challenge_state()
        campaign.reset_campaign_state()


class WorldStoreTests(WorldStateCase):
    async def test_nothing_is_written_until_a_world_is_entered(self):
        worlds.log_message("user", "привет")
        worlds.save()
        self.assertFalse(worlds.active())
        self.assertFalse((Path(self.temp_dir.name) / "worlds").exists())

    async def test_history_survives_a_restart(self):
        worlds.enter("sp:Тест", "Тест")
        worlds.record_exchange("дай меч", {"response": "Какой меч?", "outcome": "clarify",
                                           "source": "chat"})
        self.forget()

        state = worlds.enter("sp:Тест", "Тест")
        self.assertEqual([m["text"] for m in state["history"]], ["дай меч", "Какой меч?"])
        self.assertEqual(state["history"][1]["outcome"], "clarify")
        self.assertEqual(state["messages"], 2)
        self.assertFalse(state["world"]["new"])

    async def test_each_world_has_its_own_history_and_session(self):
        worlds.enter("sp:Первый", "Первый")
        sessions.shared().set_mode("survival")
        worlds.record_exchange("как сделать факел", {
            "response": "Факел: 6 шагов.", "outcome": "act", "plan_tree": "Факел ×1",
            "plan_summary": "Факел: 6 шагов.", "explanation": ["план построен"]})

        second = worlds.enter("sp:Второй", "Второй")
        self.assertTrue(second["world"]["new"])
        self.assertEqual(second["history"], [])
        self.assertEqual(second["plan"], {})
        self.assertEqual(second["assistant_mode"], "creative")
        worlds.record_exchange("привет", {"response": "Привет!", "outcome": "none"})

        first = worlds.enter("sp:Первый", "Первый")
        self.assertEqual(first["assistant_mode"], "survival")
        self.assertEqual(first["plan"]["tree"], "Факел ×1")
        self.assertEqual(first["plan"]["explanation"], ["план построен"])
        self.assertEqual([m["text"] for m in first["history"]],
                         ["как сделать факел", "Факел: 6 шагов."])

    async def test_session_facts_and_window_come_back(self):
        worlds.enter("sp:Тест", "Тест")
        session = sessions.shared()
        session.given["Алмазный меч"] += 1
        session._window.append(("user", "дай алмазный меч"))
        session._window.append(("assistant", "Выдаю."))
        session.persona = "smith"
        worlds.save()
        self.forget()

        worlds.enter("sp:Тест", "Тест")
        restored = sessions.shared()
        self.assertEqual(restored.given["Алмазный меч"], 1)
        self.assertEqual(restored.window()[0], {"role": "user", "content": "дай алмазный меч"})
        self.assertEqual(restored.persona, "smith")
        self.assertIn("Выдано: Алмазный меч ×1", restored.summary())

    async def test_quests_and_kills_belong_to_the_world(self):
        worlds.enter("sp:Первый", "Первый")
        challenges.create_challenge(title="Зомби", goal_type="kill",
                                    goal_target_id="minecraft:zombie", goal_count=3,
                                    reward_item_id="minecraft:bread", reward_count=1)
        player_state.apply_log_to_player_state(LogEntry(
            level="INFO", message="kill", event_type="mob_kill",
            event_data={"entity_id": "minecraft:zombie", "entity_name": "Zombie"}))
        worlds.save()

        worlds.enter("sp:Второй", "Второй")
        self.assertIsNone(challenges.get_active_challenge())
        self.assertEqual(player_state.get_player_state()["kill_counts"], {})

        self.forget()
        worlds.enter("sp:Первый", "Первый")
        self.assertEqual(challenges.get_active_challenge()["title"], "Зомби")
        self.assertEqual(
            player_state.get_player_state()["kill_counts"].get("minecraft:zombie"), 1)

    async def test_world_identifier_cannot_leave_the_store(self):
        """Идентификатор приходит из запроса: в путь идёт только отфильтрованная часть."""
        worlds.enter("../../etc/passwd", "чужой")
        store = (Path(self.temp_dir.name) / "worlds").resolve()
        self.assertEqual(Path(worlds.current()["dir"]).resolve().parent, store)
        self.assertNotEqual(worlds.folder_name("a/b"), worlds.folder_name("a_b"))

    async def test_routine_tips_stay_out_of_the_history(self):
        worlds.enter("sp:Тест", "Тест")
        worlds.record_proactive({"response": "Здоровье в норме.", "source": "assistant"})
        worlds.record_proactive({"response": "Рядом крипер: возьми щит.", "source": "forewarn"})
        self.assertEqual([m["text"] for m in worlds.history()], ["Рядом крипер: возьми щит."])
        self.assertTrue(worlds.history()[0]["proactive"])

    async def test_restore_returns_to_the_last_world(self):
        worlds.enter("sp:Тест", "Тест")
        worlds.record_exchange("привет", {"response": "Привет!", "outcome": "none"})
        self.forget()
        worlds.restore()
        self.assertEqual(worlds.current()["name"], "Тест")
        self.assertEqual(len(worlds.history()), 2)

    async def test_first_start_opens_the_world_without_a_game(self):
        worlds.restore()
        self.assertEqual(worlds.current()["id"], worlds.DEFAULT_WORLD)


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class WorldChatTests(WorldStateCase):
    """Сквозной путь: реплика в чате попадает в историю мира и возвращается после перезапуска."""

    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    async def chat(self, text, answers):
        pipeline = Pipeline(Scripted(answers), self.kb)
        responder = Responder(ask_text=lambda messages: "")
        with (patch.object(advice, "_assistant_pipeline", return_value=pipeline),
              patch.object(advice, "_assistant_responder", return_value=responder)):
            return await advice.player_chat_message(ChatMessage(text=text))

    async def test_plan_and_dialogue_come_back_after_a_restart(self):
        worlds.enter("sp:Тест", "Тест")
        question = "как сделать каменную кирку"
        answer = await self.chat(question, {question: Proposal("plan_goal", "каменная кирка", 1)})
        self.assertTrue(answer.plan_tree)
        self.forget()

        state = worlds.enter("sp:Тест", "Тест")
        self.assertEqual(state["history"][0]["text"], question)
        self.assertEqual(state["history"][1]["text"], answer.response)
        self.assertEqual(state["plan"]["tree"], answer.plan_tree)
        self.assertIn("как получить «Каменная кирка»", sessions.shared().summary())

    async def test_mode_set_by_a_phrase_is_remembered(self):
        worlds.enter("sp:Тест", "Тест")
        await self.chat("режим выживания", {})
        self.forget()
        self.assertEqual(worlds.enter("sp:Тест", "Тест")["assistant_mode"], "survival")


if __name__ == "__main__":
    unittest.main()
