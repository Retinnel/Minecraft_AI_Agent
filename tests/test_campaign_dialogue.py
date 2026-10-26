"""Кампания в диалоге: конвейер, рассказчик и игровой чат.

Модель подменяется заглушками на всех трёх участках — извлечение намерения, выбор цели,
реплики персонажа. Всё остальное настоящее: онтология, решатель кампании, состояние
заданий. Главная проверка — на декоративность: что бы модель ни сказала, цепочка этапов
от этого не меняется, а выбор цели не выходит за пределы кандидатов решателя.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import advice, campaign, challenges
from api.player_state import reset_player_state
from kb.graph import KnowledgeBase
from models.groq_response import ChatMessage
from orchestrator import Pipeline, Proposal
from orchestrator.extractor import LanguageModelIntent
from orchestrator.llm import ModelUnavailable
from orchestrator.narrator import MODEL, TEMPLATE, Narrator, load_personas
from orchestrator.pipeline import PROPOSE_GOAL, START_CAMPAIGN
from orchestrator.responder import Responder
from orchestrator.session import Session
from solver.campaign import CampaignBuilder
from solver.validator import ACT, REJECT, SURVIVAL

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"

TABLE = "minecraft:crafting_table"
WOODEN = "minecraft:wooden_pickaxe"
STONE = "minecraft:stone_pickaxe"
FURNACE = "minecraft:furnace"


class StubIntent:
    def __init__(self, proposal, choice=None):
        self.proposal = proposal
        self.choice = choice
        self.chosen_from = None

    def extract(self, utterance, **context):
        return self.proposal

    def choose(self, candidates, state=""):
        self.chosen_from = candidates
        if isinstance(self.choice, Exception):
            raise self.choice
        return self.choice


class PlainIntent:
    """Извлекатель без выбора цели: модель, которая умеет только разбирать реплику."""

    def __init__(self, proposal):
        self.proposal = proposal

    def extract(self, utterance, **context):
        return self.proposal


def silent(messages):
    raise ModelUnavailable("в тестах модель не формулирует")


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class CampaignPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def test_named_goal_becomes_a_proven_campaign(self):
        pipeline = Pipeline(StubIntent(Proposal(START_CAMPAIGN, "каменная кирка", 1)), self.kb)
        turn = pipeline.handle("веди меня к каменной кирке", inventory={})
        self.assertEqual(turn.outcome, ACT)
        self.assertEqual(turn.action, START_CAMPAIGN)
        self.assertEqual(turn.target, STONE)
        self.assertEqual(turn.campaign.targets, [TABLE, WOODEN, STONE])
        self.assertTrue(turn.plan_tree)
        self.assertFalse(turn.proposed)

    def test_campaign_is_available_in_survival(self):
        pipeline = Pipeline(StubIntent(Proposal(START_CAMPAIGN, "каменная кирка", 1)), self.kb)
        turn = pipeline.handle("веди меня к каменной кирке", mode=SURVIVAL, inventory={})
        self.assertEqual(turn.outcome, ACT)

    def test_unreachable_goal_is_refused_with_the_solver_reason(self):
        pipeline = Pipeline(StubIntent(Proposal(START_CAMPAIGN, "коренная порода", 1)), self.kb)
        turn = pipeline.handle("веди меня к коренной породе", inventory={})
        self.assertEqual(turn.outcome, REJECT)
        self.assertIn("не достать", turn.reason)
        self.assertIsNone(turn.campaign)

    def test_proposed_goal_is_chosen_among_solver_candidates(self):
        intent = StubIntent(Proposal(PROPOSE_GOAL), choice={"goal": "Печь", "why": "без печи нет железа"})
        turn = Pipeline(intent, self.kb).handle("дай цель", inventory={})
        self.assertEqual(turn.outcome, ACT)
        self.assertEqual(turn.action, START_CAMPAIGN)
        self.assertEqual(turn.target, FURNACE)
        self.assertTrue(turn.proposed)
        self.assertTrue(turn.stages["choice"]["accepted"])
        self.assertIn(FURNACE, [candidate["item"] for candidate in intent.chosen_from])

    def test_choice_outside_the_candidates_falls_back_to_the_cheapest(self):
        intent = StubIntent(Proposal(PROPOSE_GOAL), choice={"goal": "Незеритовый меч", "why": "круто"})
        turn = Pipeline(intent, self.kb).handle("дай цель", inventory={})
        self.assertEqual(turn.outcome, ACT)
        self.assertEqual(turn.target, TABLE)
        self.assertFalse(turn.stages["choice"]["accepted"])

    def test_choice_failure_still_yields_a_campaign(self):
        intent = StubIntent(Proposal(PROPOSE_GOAL), choice=RuntimeError("сеть"))
        turn = Pipeline(intent, self.kb).handle("дай цель", inventory={})
        self.assertEqual(turn.outcome, ACT)
        self.assertEqual(turn.target, TABLE)

    def test_extractor_without_choice_still_proposes(self):
        turn = Pipeline(PlainIntent(Proposal(PROPOSE_GOAL)), self.kb).handle("дай цель", inventory={})
        self.assertEqual(turn.outcome, ACT)
        self.assertTrue(turn.proposed)

    def test_model_does_not_influence_the_stages(self):
        # Два разных «ответа модели» об одной цели дают одну и ту же цепочку, равную той, что
        # решатель строит сам: модель называет цель, но не решает, как к ней идти.
        direct = CampaignBuilder(self.kb).build(STONE, 1, {})
        for words in ("каменная кирка", "каменную кирку"):
            turn = Pipeline(StubIntent(Proposal(START_CAMPAIGN, words, 1)), self.kb).handle(
                "веди меня", inventory={})
            self.assertEqual(turn.outcome, ACT, turn.reason)
            self.assertEqual(turn.campaign.targets, direct.targets)
            self.assertEqual([s.reward for s in turn.campaign.stages],
                             [s.reward for s in direct.stages])


STAGE = {
    "index": 2, "total": 3, "target": WOODEN, "label": "Деревянная кирка", "count": 1,
    "unlocks": "tool", "for_label": "Булыжник", "source_label": None,
    "reward": "minecraft:bread", "reward_label": "Хлеб", "reward_count": 2, "texts": {},
}
CAMPAIGN = {"id": "campaign-1", "title": "Кампания: Каменная кирка", "label": "Каменная кирка",
            "count": 1, "persona": "smith", "current": 2, "texts": {},
            "stages": [{"label": "Верстак", "status": "completed"}, STAGE,
                       {"label": "Каменная кирка", "status": "pending"}]}


class NarratorTests(unittest.TestCase):
    def test_template_carries_the_facts_and_the_persona(self):
        narrator = Narrator(ask_text=silent)
        brief = narrator.brief(CAMPAIGN, STAGE)
        self.assertEqual(brief.source, TEMPLATE)
        self.assertTrue(brief.text.startswith("Кузнец Торвальд:"))
        self.assertIn("Этап 2 из 3", brief.text)
        self.assertIn("Деревянная кирка", brief.text)
        self.assertIn("Открывает добычу: Булыжник", brief.text)

    def test_done_names_the_reward(self):
        done = Narrator(ask_text=silent).done(CAMPAIGN, STAGE)
        self.assertIn("Хлеб ×2", done.text)

    def test_proposed_intro_explains_why(self):
        intro = Narrator(ask_text=silent).intro(CAMPAIGN, proposed=True, why="без кирки никуда")
        self.assertIn("Предлагаю цель", intro.text)
        self.assertIn("без кирки никуда", intro.text)

    def test_model_text_is_used_when_it_keeps_the_essentials(self):
        narrator = Narrator(ask_text=lambda messages: "Ну что, ученик, деревянная кирка — и камень твой.")
        brief = narrator.brief(CAMPAIGN, STAGE)
        self.assertEqual(brief.source, MODEL)
        self.assertTrue(brief.text.startswith("Кузнец Торвальд:"))

    def test_model_text_without_the_essentials_is_replaced_by_the_template(self):
        narrator = Narrator(ask_text=lambda messages: "Иди и сделай что-нибудь полезное.")
        brief = narrator.brief(CAMPAIGN, STAGE)
        self.assertEqual(brief.source, TEMPLATE)
        self.assertIn("Деревянная кирка", brief.text)

    def test_model_sees_only_facts_not_the_player(self):
        seen = []

        def spy(messages):
            seen.extend(messages)
            return "Хлеб твой, ученик."

        Narrator(ask_text=spy).done(CAMPAIGN, STAGE)
        system = seen[0]["content"]
        self.assertIn("Кузнец Торвальд", system)
        self.assertIn("Награда выдана: Хлеб ×2", system)
        self.assertIn("Пройдено: Верстак", system)

    def test_unknown_persona_falls_back_to_the_first_preset(self):
        narrator = Narrator(ask_text=silent)
        key, persona = narrator.persona("nobody")
        self.assertEqual(key, narrator.persona_keys()[0])
        self.assertTrue(persona["name"])

    def test_presets_are_loaded_from_the_prompt_config(self):
        personas = load_personas()
        self.assertEqual(set(personas) >= {"smith", "elder", "cartographer"}, True)
        for persona in personas.values():
            self.assertEqual(set(persona), {"name", "role", "style", "address"})


class ScriptedModel:
    def __init__(self, answers):
        self.answers = answers

    def __call__(self, prompt, utterance):
        answer = self.answers.get(utterance, {"action": "none"})
        return answer.pop(0) if isinstance(answer, list) else answer


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class CampaignChatTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.patches = [
            patch.object(challenges, "CHALLENGE_STATE_PATH", root / "challenge_state.json"),
            patch.object(campaign, "CAMPAIGN_STATE_PATH", root / "campaign_state.json"),
        ]
        for item in self.patches:
            item.start()
        advice.logs_db.clear()
        advice._response_history.clear()
        advice._conversation_history.clear()
        reset_player_state()
        challenges.reset_challenge_state()
        campaign.reset_campaign_state()
        self.session = Session()

    def tearDown(self):
        campaign.reset_campaign_state()
        challenges.reset_challenge_state()
        for item in self.patches:
            item.stop()
        self.temp_dir.cleanup()

    async def chat(self, text, answers):
        pipeline = Pipeline(LanguageModelIntent(ask=ScriptedModel(answers)), self.kb)
        with (
            patch.object(advice, "_assistant_pipeline", return_value=pipeline),
            patch.object(advice, "_assistant_responder", return_value=Responder(ask_text=silent)),
            patch.object(advice, "_assistant_narrator", return_value=Narrator(ask_text=silent)),
            patch.object(advice, "_assistant_session", return_value=self.session),
        ):
            return await advice.player_chat_message(ChatMessage(text=text))

    async def test_named_goal_starts_a_campaign_and_the_persona_speaks(self):
        result = await self.chat("веди меня к каменной кирке",
                                 {"веди меня к каменной кирке":
                                  {"action": "start_campaign", "target": "каменная кирка", "count": 1}})
        self.assertEqual(result.outcome, "act")
        self.assertTrue(result.response.startswith("Кузнец Торвальд:"))
        self.assertIn("Этап 1 из 3", result.response)
        self.assertIn("Верстак", result.response)
        self.assertFalse(result.execute)

        self.assertEqual(result.campaign["current"], 1)
        self.assertEqual(result.campaign["total"], 3)
        self.assertEqual(result.campaign["persona_name"], "Кузнец Торвальд")
        self.assertTrue(result.plan_tree)

        active = campaign.get_active_campaign()
        self.assertEqual(active["goal"], STONE)
        self.assertTrue(active["texts"]["intro"])
        self.assertTrue(active["stages"][0]["texts"]["brief"])
        # Старт озвучен этой репликой: проактивному каналу повторять нечего.
        self.assertEqual(campaign.consume_campaign_events(), [])
        self.assertIn("Кампания", self.session.summary())

    async def test_assistant_proposes_a_goal(self):
        result = await self.chat("дай цель", {
            "дай цель": {"action": "propose_goal", "target": None, "count": 1},
            "": {"goal": "Печь", "why": "без печи железа не выплавить"},
        })
        self.assertEqual(result.outcome, "act")
        self.assertIn("Предлагаю цель", result.response)
        self.assertIn("без печи железа не выплавить", result.response)
        self.assertEqual(campaign.get_active_campaign()["goal"], FURNACE)

    async def test_persona_command_changes_the_speaker(self):
        switched = await self.chat("персонаж: старейшина", {})
        self.assertIn("Старейшина Марта", switched.response)
        self.assertEqual(self.session.persona, "elder")
        result = await self.chat("веди меня к печи", {
            "веди меня к печи": {"action": "start_campaign", "target": "печь", "count": 1}})
        self.assertTrue(result.response.startswith("Старейшина Марта:"))
        self.assertEqual(campaign.get_active_campaign()["persona"], "elder")

    async def test_second_campaign_is_refused_not_replaced(self):
        answers = {"веди меня к каменной кирке":
                   {"action": "start_campaign", "target": "каменная кирка", "count": 1}}
        await self.chat("веди меня к каменной кирке", dict(answers))
        result = await self.chat("веди меня к каменной кирке", dict(answers))
        self.assertEqual(result.outcome, "reject")
        self.assertIn("ещё идёт", result.response)

    async def test_cancel_command_ends_the_campaign(self):
        await self.chat("веди меня к каменной кирке",
                        {"веди меня к каменной кирке":
                         {"action": "start_campaign", "target": "каменная кирка", "count": 1}})
        result = await self.chat("отмени кампанию", {})
        self.assertIn("отменена", result.response)
        self.assertIsNone(campaign.get_active_campaign())
        self.assertIsNone(challenges.get_active_challenge())


if __name__ == "__main__":
    unittest.main()
