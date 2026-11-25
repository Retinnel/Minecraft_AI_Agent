"""Действия памяти в чате: «запомни», «где я видел», «забудь» — в Minecraft и в другой игре.

Модель подменяется заглушкой: она только называет действие и предмет словами игрока.
Закрепляется распределение ответственности: координаты ставит система по сведениям мода,
факты отбирает память, забывание требует явных слов, а факт памяти никогда не становится
командой игре.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import memory
from api import advice, campaign, challenges, logs, worlds
from api.challenges import reset_challenge_state
from api.player_state import reset_player_state
from kb.graph import KnowledgeBase
from memory.model import CONTAINER, NOTE, OVERWORLD, PLACE, REMOVED, Position
from memory.store import MemoryStore
from models.groq_response import ChatMessage
from models.log_entry import LogEntry
from orchestrator import Pipeline, Proposal
from orchestrator import session as sessions
from orchestrator.extractor import build_prompt
from orchestrator.pipeline import FORGET_FACT, OTHER_GAME_ACTIONS, RECALL, REMEMBER, SEARCH_WEB
from orchestrator.responder import MODEL, TEMPLATE, Responder
from search.engines import Hit
from search.service import WebSearch
from solver.validator import ACT, CLARIFY, CREATIVE, REJECT, SURVIVAL, Validator

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


def at(x, y, z, dimension=OVERWORLD):
    return Position(dimension, x, y, z)


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


class StubIntent:
    def __init__(self, proposal):
        self.proposal = proposal

    def extract(self, utterance, **context):
        return self.proposal


class FakeEngine:
    name = "tavily"

    def __init__(self):
        self.queries = []

    def search(self, query, *, max_results=5):
        self.queries.append(query)
        return [Hit(title="Keira Metz", url="https://witcher.fandom.com/keira",
                    content="Keira Metz cure lab Velen witcher potion", score=0.9,
                    engine="tavily")]


class PromptTests(unittest.TestCase):
    def test_memory_actions_are_offered_in_both_modes(self):
        for mode in (CREATIVE, SURVIVAL):
            available = Validator.available_actions(None, mode)
            for action in (REMEMBER, RECALL, FORGET_FACT):
                self.assertIn(action, available)

    def test_prompt_describes_memory_actions(self):
        prompt = build_prompt((REMEMBER, RECALL, FORGET_FACT, SEARCH_WEB))
        for action in (REMEMBER, RECALL, FORGET_FACT):
            self.assertIn(f'"{action}"', prompt)

    def test_other_game_search_leaves_memory_to_memory(self):
        prompt = build_prompt(OTHER_GAME_ACTIONS, game="Ведьмак 3")
        self.assertIn("сюда не относятся", prompt.split('"search_web" —')[1].split("\n")[0])
        # Без действий памяти подсказка поиска для другой игры прежняя.
        self.assertNotIn("просил запомнить", build_prompt((SEARCH_WEB,), game="Ведьмак 3"))


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class MemoryPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def setUp(self):
        self.store = MemoryStore(None, world="sp:Тест")

    def turn(self, utterance, proposal, *, game=None, position=None, search=None, store=True,
             mode=CREATIVE):
        pipeline = Pipeline(StubIntent(proposal), self.kb, search=search)
        return pipeline.handle(utterance, game=game, position=position, mode=mode,
                               memory=self.store if store else None)

    # --- запомнить ---

    def test_remember_here_marks_the_place_from_the_mod(self):
        turn = self.turn("запомни, тут портал в Незер", Proposal(REMEMBER, "портал в Незер"),
                         position=at(88, 64, -210))
        self.assertEqual(turn.outcome, ACT)
        self.assertEqual(turn.memory_summary,
                         "Запомнил: портал в Незер, x 88, y 64, z −210, Верхний мир.")
        self.assertEqual(tuple(turn.memory_numbers), (88, 64, -210))
        fact = self.store.facts(PLACE)[0]
        self.assertEqual((fact.label, fact.position, fact.text), ("портал в Незер", at(88, 64, -210), None))
        self.assertIsNone(turn.command)

    def test_remember_without_known_position_is_a_note(self):
        turn = self.turn("запомни, тут база", Proposal(REMEMBER, "база"))
        self.assertEqual(turn.outcome, ACT)
        self.assertEqual(self.store.facts(NOTE)[0].label, "база")
        self.assertTrue(any("координат не присылал" in line for line in turn.explanation))

    def test_remember_without_here_keeps_the_words(self):
        turn = self.turn("запомни: железо в пещере за рекой", Proposal(REMEMBER, "железо"),
                         position=at(0, 64, 0))
        fact = self.store.facts(NOTE)[0]
        self.assertEqual((fact.label, fact.text, fact.position),
                         ("железо", "железо в пещере за рекой", None))
        self.assertEqual(turn.memory_summary, "Запомнил: железо.")

    def test_remember_in_another_game_ignores_coordinates(self):
        turn = self.turn("запомни: ключ-меч у благодати Церковь Элле", Proposal(REMEMBER, "ключ-меч"),
                         game="Elden Ring", position=at(1, 2, 3))
        self.assertEqual(turn.outcome, ACT)
        self.assertEqual(turn.memory_summary, "Запомнил: ключ-меч. Спроси «где ключ-меч» — напомню.")
        self.assertIsNone(self.store.facts(NOTE)[0].position)

    def test_remember_needs_something_to_remember(self):
        self.assertEqual(self.turn("запомни", Proposal(REMEMBER, None)).outcome, CLARIFY)

    def test_no_memory_means_refusal(self):
        turn = self.turn("где я видел алмазы", Proposal(RECALL, "алмазы"), store=False)
        self.assertEqual(turn.outcome, REJECT)

    # --- вспомнить ---

    def test_where_did_i_see_diamonds_finds_the_ore(self):
        self.store.observe_blocks([("minecraft:diamond_ore", at(-120, -58, 340)),
                                   ("minecraft:diamond_ore", at(-121, -58, 340))],
                                  labels={"minecraft:diamond_ore": "Алмазная руда"}, now=ago(30))
        self.store.observe_blocks([("minecraft:iron_ore", at(10, 20, 10))], now=ago(1))
        turn = self.turn("где я видел алмазы?", Proposal(RECALL, "алмазы"), position=at(0, 64, 200))
        self.assertEqual(turn.outcome, ACT)
        self.assertIn("minecraft:diamond_ore", turn.stages["memory"]["subjects"])
        self.assertEqual(len(turn.memory), 1)
        self.assertTrue(turn.memory_summary.startswith(
            "Алмазная руда, 2 блока: x −120, y −58, z 340, Верхний мир, около 184 блоков на юго-запад"))
        self.assertEqual(tuple(turn.memory_numbers), (-120, -58, 340))
        self.assertIsNone(turn.command)

    def test_items_in_a_chest_are_recalled(self):
        self.store.record_container("minecraft:chest", at(12, 64, -40),
                                    {"minecraft:iron_ingot": 5}, label="Сундук")
        turn = self.turn("где мои железные слитки", Proposal(RECALL, "железные слитки"))
        self.assertEqual(turn.outcome, ACT)
        self.assertEqual(turn.memory[0].kind, CONTAINER)
        self.assertIn("Железный слиток ×5", turn.memory_summary)

    def test_where_did_i_die(self):
        self.store.record_death(at(1, 1, 1), "fall", label="падение с высоты", now=ago(60))
        self.store.record_death(at(-30, -50, 115), "lava", {"minecraft:iron_ingot": 9},
                                label="огонь или лава", now=ago(12))
        turn = self.turn("где я умер?", Proposal(RECALL, "смерть"), position=at(0, 64, 0))
        self.assertEqual(turn.outcome, ACT)
        self.assertTrue(turn.memory_summary.startswith(
            "Гибель (огонь или лава): x −30, y −50, z 115, Верхний мир"))
        self.assertIn("12 минут назад", turn.memory_summary)
        self.assertIn("Железный слиток ×9", turn.memory_summary)

    def test_nothing_remembered_in_minecraft_is_said_plainly(self):
        turn = self.turn("где я видел алмазы?", Proposal(RECALL, "алмазы"))
        self.assertEqual(turn.outcome, REJECT)
        self.assertIn("ничего нет", turn.reason)
        death = self.turn("где я умер?", Proposal(RECALL, None))
        self.assertEqual((death.outcome, death.reason), (REJECT, "В памяти этого мира ничего нет о гибели."))

    def test_other_game_note_is_recalled(self):
        self.store.note("ключ-меч", "у благодати Церковь Элле")
        turn = self.turn("где ключ-меч?", Proposal(RECALL, "ключ-меч"), game="Elden Ring")
        self.assertEqual(turn.outcome, ACT)
        self.assertIn("у благодати Церковь Элле", turn.memory_summary)
        self.assertIn("по твоим словам", turn.memory_summary)

    def test_other_game_falls_back_to_search(self):
        engine = FakeEngine()
        turn = self.turn("что говорила Кейра про лекарство?", Proposal(RECALL, "Кейра лекарство"),
                         game="Ведьмак 3", search=WebSearch([engine]))
        self.assertEqual(turn.action, SEARCH_WEB)
        self.assertEqual(turn.stages["memory"]["fallback"], SEARCH_WEB)
        self.assertTrue(engine.queries)

    def test_recognised_dialogue_is_found_with_a_mark(self):
        self.store.record_dialogue("Кейра", "Лекарство варится из корня мандрагоры")
        turn = self.turn("что говорила Кейра про лекарство?", Proposal(RECALL, "Кейра лекарство"),
                         game="Ведьмак 3")
        self.assertEqual(turn.outcome, ACT)
        self.assertIn("распознано по снимку экрана", turn.memory_summary)

    def test_memory_works_in_survival(self):
        self.store.note("база", position=at(0, 64, 0))
        self.assertEqual(self.turn("где база", Proposal(RECALL, "база"), mode=SURVIVAL).outcome, ACT)

    # --- забыть ---

    def test_forget_needs_explicit_words(self):
        self.store.note("база", position=at(0, 64, 0))
        turn = self.turn("база больше не нужна", Proposal(FORGET_FACT, "база"))
        self.assertEqual(turn.outcome, CLARIFY)
        self.assertEqual(len(self.store.facts(PLACE)), 1)

    def test_forget_removes_the_mark(self):
        fact = self.store.note("база", position=at(0, 64, 0))
        turn = self.turn("забудь про базу", Proposal(FORGET_FACT, "база"))
        self.assertEqual((turn.outcome, turn.memory_summary), (ACT, "Забыл отметку «база»."))
        self.assertEqual(fact.status, REMOVED)
        self.assertEqual(self.turn("где база", Proposal(RECALL, "база")).outcome, REJECT)

    def test_forget_unknown(self):
        self.assertEqual(self.turn("забудь про базу", Proposal(FORGET_FACT, "база")).outcome, REJECT)
        self.assertEqual(self.turn("забудь", Proposal(FORGET_FACT, None)).outcome, CLARIFY)

    def test_forget_asks_when_several_marks_fit(self):
        self.store.note("портал в Незер", position=at(0, 64, 0))
        self.store.note("портал в Край", position=at(50, 64, 0))
        turn = self.turn("забудь портал", Proposal(FORGET_FACT, "портал"))
        self.assertEqual(turn.outcome, CLARIFY)
        self.assertEqual(len(self.store.facts(PLACE)), 2)

    # --- пересказ ---

    def test_paraphrase_losing_coordinates_falls_back_to_the_template(self):
        self.store.note("база", position=at(12, 64, -40))
        turn = self.turn("где база", Proposal(RECALL, "база"))
        lost = Responder(ask_text=lambda messages: "База где-то рядом, иди на север.").reply(turn, "где база")
        self.assertEqual((lost.source, lost.text), (TEMPLATE, turn.memory_summary))
        kept = Responder(ask_text=lambda messages: "База на x 12, y 64, z -40.").reply(turn, "где база")
        self.assertEqual(kept.source, MODEL)


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class MemoryChatTests(unittest.IsolatedAsyncioTestCase):
    """Сквозной путь через `/api/rp/chat`: позиция из события мода, память мира на диске."""

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

    def tearDown(self):
        self.forget()
        logs.logs_db.clear()
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

    async def chat(self, text, proposal):
        pipeline = Pipeline(StubIntent(proposal), self.kb)
        responder = Responder(ask_text=lambda messages: "")
        with (patch.object(advice, "_assistant_pipeline", return_value=pipeline),
              patch.object(advice, "_assistant_responder", return_value=responder)):
            return await advice.player_chat_message(ChatMessage(text=text))

    async def test_mark_survives_a_restart_and_is_recalled(self):
        worlds.enter("sp:Тест", "Тест")
        await logs.add_log(LogEntry(level="INFO", event_type="state", event_data={
            "health": 20, "food": 20, "dimension": "minecraft:overworld",
            "x": 88, "y": 64, "z": -210}))
        answer = await self.chat("запомни, тут портал в Незер", Proposal(REMEMBER, "портал в Незер"))
        self.assertEqual(answer.response, "Запомнил: портал в Незер, x 88, y 64, z −210, Верхний мир.")
        self.assertFalse(answer.execute)
        self.assertIsNone(answer.command)
        self.assertEqual(answer.memory[0]["label"], "портал в Незер")

        self.forget()
        worlds.enter("sp:Тест", "Тест")
        answer = await self.chat("где портал?", Proposal(RECALL, "портал"))
        self.assertEqual(answer.outcome, "act")
        self.assertIn("x 88, y 64, z −210", answer.response)
        self.assertIn("по твоим словам", answer.response)

    async def test_disabled_memory_refuses(self):
        worlds.enter("sp:Тест", "Тест")
        with patch("memory.config.settings", return_value={"enabled": False}):
            answer = await self.chat("где портал?", Proposal(RECALL, "портал"))
        self.assertEqual(answer.outcome, "reject")


if __name__ == "__main__":
    unittest.main()
