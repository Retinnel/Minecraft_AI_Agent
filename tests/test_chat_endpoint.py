"""Сквозные проверки игрового чата: от реплики до ответа, который получает мод.

Языковая модель подменяется на обоих концах конвейера: на входе — заглушкой извлечения
намерения, на выходе — заглушкой формулирования. Всё между ними работает по-настоящему:
онтология, резолвер, валидатор, планировщик, проверка заданий, сборка команды. Так
проверяется именно то, что отличает систему от «просто прикрученной модели», и без сети.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api import advice, challenges
from api.challenges import get_active_challenge, reset_challenge_state
from api.player_state import apply_log_to_player_state, reset_player_state
from kb.graph import KnowledgeBase
from models.groq_response import ChatMessage
from orchestrator import Pipeline
from orchestrator.extractor import LanguageModelIntent
from orchestrator.llm import ModelUnavailable
from orchestrator.responder import Responder
from orchestrator.session import Session

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


class ScriptedModel:
    """Модель, отвечающая по сценарию: реплика → JSON намерения."""

    def __init__(self, answers):
        self.answers = answers
        self.prompts = []

    def __call__(self, prompt, utterance):
        self.prompts.append(prompt)
        answer = self.answers.get(utterance, {"action": "none"})
        return answer.pop(0) if isinstance(answer, list) else answer


def silent_phrasing(messages):
    """Формулирование недоступно: ответ собирается из шаблона."""
    raise ModelUnavailable("в тестах модель не формулирует")


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class ChatEndpointTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def setUp(self):
        # Задания сохраняются в файл; тесты не должны трогать рабочее состояние игрока.
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(challenges, "CHALLENGE_STATE_PATH",
                                       Path(self.temp_dir.name) / "challenge_state.json")
        self.path_patch.start()
        advice.logs_db.clear()
        advice._response_history.clear()
        advice._conversation_history.clear()
        reset_player_state()
        reset_challenge_state()
        self.session = Session()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def chat(self, text, answers, *, phrasing=silent_phrasing):
        model = ScriptedModel(answers)
        pipeline = Pipeline(LanguageModelIntent(ask=model), self.kb)
        responder = Responder(ask_text=phrasing)
        with (
            patch.object(advice, "_assistant_pipeline", return_value=pipeline),
            patch.object(advice, "_assistant_responder", return_value=responder),
            patch.object(advice, "_assistant_session", return_value=self.session),
        ):
            result = await advice.player_chat_message(ChatMessage(text=text))
        return result, model

    # --- выдача ---

    async def test_give_builds_command_for_the_mod(self):
        result, _ = await self.chat("дай мне 5 алмазов",
                                    {"дай мне 5 алмазов": {"action": "give_item", "target": "алмазов", "count": 5}})
        self.assertEqual(result.mode, "action")
        self.assertTrue(result.execute)
        self.assertEqual(result.action_type, "give")
        self.assertEqual(result.command, "/give @s minecraft:diamond 5")
        self.assertEqual(result.item_count, 5)
        self.assertIn("5", result.response)

    async def test_summon_is_mapped_to_mod_protocol(self):
        result, _ = await self.chat("призови 2 зомби",
                                    {"призови 2 зомби": {"action": "summon_entity", "target": "зомби", "count": 2}})
        self.assertEqual(result.action_type, "summon")
        self.assertEqual(result.entity_id, "minecraft:zombie")
        self.assertEqual(result.entity_count, 2)

    async def test_clamped_count_is_named(self):
        result, _ = await self.chat("дай 1000 алмазов",
                                    {"дай 1000 алмазов": {"action": "give_item", "target": "алмаз", "count": 1000}})
        self.assertEqual(result.item_count, 64)
        self.assertIn("64", result.response)

    # --- уточнение и отказ ---

    async def test_generic_word_asks_and_executes_nothing(self):
        result, _ = await self.chat("дай меч", {"дай меч": {"action": "give_item", "target": "меч", "count": 1}})
        self.assertEqual(result.outcome, "clarify")
        self.assertFalse(result.execute)
        self.assertIsNone(result.command)
        self.assertGreater(len(result.options), 1)

    async def test_restriction_is_explained(self):
        answers = {"призови дракона края": {"action": "summon_entity", "target": "дракон края", "count": 1}}
        result, _ = await self.chat("призови дракона края", answers)
        self.assertEqual(result.outcome, "reject")
        self.assertFalse(result.execute)
        self.assertIn("боссов", result.response)
        self.assertTrue(result.explanation)

    # --- план ---

    async def test_plan_goes_to_overlay_without_command(self):
        answers = {"как сделать алмазную кирку": {"action": "plan_goal", "target": "алмазная кирка", "count": 1}}
        result, _ = await self.chat("как сделать алмазную кирку", answers)
        self.assertEqual(result.outcome, "act")
        self.assertFalse(result.execute)
        self.assertIsNone(result.command)
        self.assertIn("Алмазная кирка", result.plan_tree)
        self.assertIn("отвергнуто", result.plan_alternatives)

    async def test_plan_uses_inventory_from_the_mod(self):
        apply_log_to_player_state(SimpleNamespace(
            event_type="inventory_snapshot", timestamp=None,
            event_data={"counts": {"minecraft:diamond": 3, "minecraft:stick": 2}}))
        answers = {"как сделать алмазную кирку": {"action": "plan_goal", "target": "алмазная кирка", "count": 1}}
        result, _ = await self.chat("как сделать алмазную кирку", answers)
        self.assertIn("1 шаг", result.plan_summary)

    # --- режимы ---

    async def test_mode_command_needs_no_model(self):
        result, model = await self.chat("режим выживания", {})
        self.assertEqual(self.session.mode, "survival")
        self.assertEqual(result.assistant_mode, "survival")
        self.assertEqual(model.prompts, [])

    async def test_survival_hides_give_from_the_model(self):
        """Режим управляет составом действий, предъявляемых модели."""
        self.session.set_mode("survival")
        _, model = await self.chat("дай алмаз", {"дай алмаз": {"action": "plan_goal", "target": "алмаз"}})
        self.assertNotIn('"give_item"', model.prompts[0])
        self.assertIn('"plan_goal"', model.prompts[0])

    async def test_survival_rejects_give_even_if_model_proposes_it(self):
        """Вторая линия: если модель всё же предложит выдачу, её отклонит валидатор."""
        self.session.set_mode("survival")
        result, _ = await self.chat("дай алмаз", {"дай алмаз": {"action": "give_item", "target": "алмаз"}})
        self.assertEqual(result.outcome, "reject")
        self.assertFalse(result.execute)

    # --- задания ---

    async def test_feasible_quest_is_saved(self):
        answers = {"дай задание": {"action": "create_quest", "goal_kind": "collect", "target": "дубовое бревно",
                                   "count": 10, "reward": "золотое яблоко", "reward_count": 1}}
        result, _ = await self.chat("дай задание", answers)
        self.assertEqual(result.outcome, "act")
        active = get_active_challenge()
        self.assertIsNotNone(active)
        self.assertEqual(active["goal_target_id"], "minecraft:oak_log")

    async def test_rejected_quest_gets_one_retry_with_reason(self):
        """Модели возвращается причина отказа и даётся одна попытка предложить другое."""
        answers = {"дай задание": [
            {"action": "create_quest", "goal_kind": "collect", "target": "командный блок",
             "count": 1, "reward": "алмаз", "reward_count": 1},
            {"action": "create_quest", "goal_kind": "collect", "target": "дубовое бревно",
             "count": 10, "reward": "алмаз", "reward_count": 1},
        ]}
        result, model = await self.chat("дай задание", answers)
        self.assertEqual(result.outcome, "act")
        self.assertEqual(len(model.prompts), 2)
        self.assertIn("отклонено", model.prompts[1])

    async def test_impossible_quest_never_reaches_the_player(self):
        bad = {"action": "create_quest", "goal_kind": "collect", "target": "командный блок",
               "count": 1, "reward": "алмаз", "reward_count": 1}
        result, model = await self.chat("дай задание", {"дай задание": [dict(bad), dict(bad)]})
        self.assertEqual(result.outcome, "reject")
        self.assertIsNone(get_active_challenge())
        self.assertEqual(len(model.prompts), 2)

    # --- необратимые действия ---

    async def test_removal_needs_a_request_to_remove(self):
        """Модель, не найдя выдачи в перечне, может схватить ближайший глагол.

        Так «дай 10 факелов» в выживании превращалось в «убираю 10 факелов»: игрок просил
        дать, а у него бы забрали. Удаление выполняется только по явной просьбе.
        """
        self.session.set_mode("survival")
        result, _ = await self.chat("дай 10 факелов", {"дай 10 факелов": {
            "action": "remove_item", "target": "факелов", "count": 10}})
        self.assertEqual(result.outcome, "clarify")
        self.assertFalse(result.execute)
        self.assertIsNone(result.command)

    async def test_explicit_removal_still_works(self):
        result, _ = await self.chat("убери 10 булыжника", {"убери 10 булыжника": {
            "action": "remove_item", "target": "булыжника", "count": 10}})
        self.assertEqual(result.action_type, "clear")
        self.assertEqual(result.command, "/clear @s minecraft:cobblestone 10")

    # --- продолжение диалога ---

    async def test_answer_to_clarification_completes_the_request(self):
        """«алмазный» после «какой меч?» разбирается без модели, по предложенным вариантам."""
        await self.chat("дай меч", {"дай меч": {"action": "give_item", "target": "меч", "count": 1}})
        result, model = await self.chat("алмазный", {})
        self.assertTrue(result.execute)
        self.assertEqual(result.command, "/give @s minecraft:diamond_sword 1")
        self.assertEqual(model.prompts, [])

    async def test_unrelated_reply_drops_the_question(self):
        """Если ответ не подходит ни к одному варианту, это новый запрос, а не догадка."""
        await self.chat("дай меч", {"дай меч": {"action": "give_item", "target": "меч", "count": 1}})
        result, model = await self.chat("дай хлеб", {"дай хлеб": {
            "action": "give_item", "target": "хлеб", "count": 1}})
        self.assertEqual(result.command, "/give @s minecraft:bread 1")
        self.assertEqual(len(model.prompts), 1)
        self.assertIsNone(self.session.pending)

    async def test_previous_exchange_reaches_the_model(self):
        await self.chat("призови зомби", {"призови зомби": {"action": "summon_entity", "target": "зомби"}})
        _, model = await self.chat("и ещё крипера", {"и ещё крипера": {
            "action": "summon_entity", "target": "крипера"}})
        self.assertIn("призови зомби", model.prompts[0])

    # --- разговор и сбои ---

    async def test_small_talk_uses_no_actions(self):
        result, _ = await self.chat("привет", {"привет": {"action": "none"}},
                                    phrasing=lambda messages: "Привет!")
        self.assertEqual(result.outcome, "none")
        self.assertFalse(result.execute)
        self.assertEqual(result.response, "Привет!")

    async def test_unavailable_model_is_not_a_refusal(self):
        def down(prompt, utterance):
            raise ModelUnavailable("нет сети")

        pipeline = Pipeline(LanguageModelIntent(ask=down), self.kb)
        with (
            patch.object(advice, "_assistant_pipeline", return_value=pipeline),
            patch.object(advice, "_assistant_session", return_value=self.session),
        ):
            result = await advice.player_chat_message(ChatMessage(text="дай алмаз"))
        self.assertEqual(result.error, "model_unavailable")
        self.assertIsNone(result.outcome)
        self.assertFalse(result.execute)

    async def test_phrasing_that_drops_the_number_is_replaced(self):
        """Модель может сделать ответ живее, но не может потерять итоговое число."""
        result, _ = await self.chat("дай 1000 алмазов",
                                    {"дай 1000 алмазов": {"action": "give_item", "target": "алмаз", "count": 1000}},
                                    phrasing=lambda messages: "Держи алмазы!")
        self.assertIn("64", result.response)

    # --- сводка фактами ---

    async def test_session_summary_is_built_from_facts(self):
        await self.chat("дай мне 5 алмазов",
                        {"дай мне 5 алмазов": {"action": "give_item", "target": "алмазов", "count": 5}})
        await self.chat("призови дракона края",
                        {"призови дракона края": {"action": "summon_entity", "target": "дракон края"}})
        summary = self.session.summary()
        self.assertIn("Выдано: Алмаз ×5", summary)
        self.assertIn("Отказано", summary)


if __name__ == "__main__":
    unittest.main()
