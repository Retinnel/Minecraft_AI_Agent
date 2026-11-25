"""Адаптер экрана: реплика персонажа с кадра попадает в память игры и находится через recall.

Захват и модель зрения подменены; сохраняется только распознанный текст, снимок — нет.
Ответ обязан называть источник: текст распознан по снимку и может быть неточным.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import memory
from api import advice, campaign, challenges, vision as vision_api, worlds
from api.challenges import reset_challenge_state
from api.player_state import reset_player_state
from kb.graph import KnowledgeBase
from memory.adapters.screen import enabled, remember_scene
from memory.config import DEFAULTS
from memory.model import CONFIDENCE, DIALOGUE, SCREEN
from memory.store import MemoryStore
from models.groq_response import ChatMessage
from orchestrator import Pipeline, Proposal
from orchestrator import session as sessions
from orchestrator.pipeline import RECALL
from orchestrator.responder import Responder
from PIL import Image
from vision.scene import Description, SceneFacts
from vision.watcher import ScreenWatcher

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"

KEIRA = {"game": "The Witcher 3", "scene": "dialogue", "focus": None, "items": [],
         "dialogue": {"speaker": "Кейра Мец", "line": "Лекарство варится из корня мандрагоры.",
                      "options": ["Спасибо", "Уйти"]},
         "text": [], "summary": "Диалог с Кейрой."}


def scene(payload=KEIRA):
    return SceneFacts.from_payload(payload)


class FakeModel:
    model = "fake-vision"
    available = True

    def __init__(self, payload=KEIRA):
        self.payload = payload

    def describe(self, image, hint=None):
        return Description(SceneFacts.from_payload(self.payload), 900.0, {})


class StubIntent:
    def __init__(self, proposal):
        self.proposal = proposal

    def extract(self, utterance, **context):
        return self.proposal


class ScreenAdapterTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(None, game="Ведьмак 3")

    def test_dialogue_line_is_stored_as_a_screen_fact(self):
        facts = remember_scene(self.store, scene())
        self.assertEqual(len(facts), 1)
        fact = facts[0]
        self.assertEqual((fact.kind, fact.source, fact.label), (DIALOGUE, SCREEN, "Кейра Мец"))
        self.assertEqual(fact.confidence, CONFIDENCE[SCREEN])
        self.assertEqual(fact.text, "Лекарство варится из корня мандрагоры.")

    def test_the_same_line_is_not_stored_twice(self):
        remember_scene(self.store, scene())
        remember_scene(self.store, scene())
        facts = self.store.facts(DIALOGUE)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].seen_count, 2)

    def test_scenes_without_a_line_are_ignored(self):
        self.assertEqual(remember_scene(self.store, scene({"game": "Tarkov", "scene": "inventory"})), [])
        short = {**KEIRA, "dialogue": {"speaker": "Кейра", "line": "Да."}}
        self.assertEqual(remember_scene(self.store, scene(short)), [])
        self.assertEqual(len(self.store), 0)

    def test_settings_switch(self):
        self.assertTrue(enabled(DEFAULTS))
        self.assertFalse(enabled({**DEFAULTS, "screen_facts": False}))
        self.assertFalse(enabled({**DEFAULTS, "enabled": False}))


class WatcherHookTests(unittest.TestCase):
    def watcher(self):
        return ScreenWatcher(FakeModel(), capture_factory=lambda title: None, guard=lambda: None)

    def test_every_new_description_reaches_the_hook(self):
        watcher, seen = self.watcher(), []
        watcher.on_facts = seen.append
        watcher.upload(Image.new("RGB", (640, 360), (40, 90, 50)))
        self.assertEqual(seen[0].dialogue.speaker, "Кейра Мец")

    def test_a_failing_hook_does_not_break_the_watcher(self):
        watcher = self.watcher()
        watcher.on_facts = lambda facts: 1 / 0
        description = watcher.upload(Image.new("RGB", (640, 360), (40, 90, 50)))
        self.assertEqual(description.facts.scene, "dialogue")


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class ScreenMemoryChatTests(unittest.IsolatedAsyncioTestCase):
    """Критерий этапа: диалог из другой игры находится через recall с пометкой «распознано»."""

    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

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
        worlds.enter("sp:Тест", "Тест")
        sessions.shared().set_game("Ведьмак 3")

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

    async def test_line_from_the_screen_is_recalled_with_a_mark(self):
        vision_api._remember_screen(scene())
        stored = memory.shared("Ведьмак 3").facts(DIALOGUE)
        self.assertEqual(len(stored), 1)
        self.assertEqual(len(memory.shared("Minecraft")), 0)

        pipeline = Pipeline(StubIntent(Proposal(RECALL, "Кейра лекарство")), self.kb)
        responder = Responder(ask_text=lambda messages: "")
        with (patch.object(advice, "_assistant_pipeline", return_value=pipeline),
              patch.object(advice, "_assistant_responder", return_value=responder)):
            answer = await advice.player_chat_message(
                ChatMessage(text="что говорила Кейра про лекарство?"))
        self.assertEqual(answer.outcome, "act")
        self.assertIn("корня мандрагоры", answer.response)
        self.assertIn("распознано по снимку экрана", answer.response)
        self.assertFalse(answer.execute)

    async def test_screen_facts_can_be_switched_off(self):
        with patch("memory.config.settings", return_value={**DEFAULTS, "screen_facts": False}):
            vision_api._remember_screen(scene())
        self.assertEqual(len(memory.shared("Ведьмак 3")), 0)

    async def test_screen_memory_survives_a_restart(self):
        vision_api._remember_screen(scene())
        self.forget()
        worlds.enter("sp:Тест", "Тест")
        self.assertEqual(len(memory.shared("Ведьмак 3").facts(DIALOGUE)), 1)


if __name__ == "__main__":
    unittest.main()
