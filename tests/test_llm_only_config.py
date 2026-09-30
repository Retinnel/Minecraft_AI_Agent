"""Проверки конфигурации «только языковая модель».

Сама модель здесь подменяется: проверяется разбор её ответа и то, что конфигурация
действительно принимает сказанное на веру — без резолва по базе знаний и без проверок.
Именно это отличие и измеряется в сравнении с гибридом, поэтому оно должно быть
зафиксировано тестом, а не держаться на честном слове.
"""

import unittest
from unittest.mock import patch

from eval.configs import llm_only
from eval.configs._llm import ModelUnavailable


class LlmOnlyConfigTests(unittest.TestCase):
    def predict_with(self, payload):
        with patch.object(llm_only, "ask_json", return_value=payload):
            return llm_only.predict("реплика")

    def test_give_is_taken_at_face_value(self):
        prediction = self.predict_with(
            {"action": "give_item", "id": "minecraft:diamond", "count": 5})
        self.assertEqual(prediction.outcome, "act")
        self.assertEqual(prediction.item_id, "minecraft:diamond")
        self.assertEqual(prediction.count, 5)
        self.assertEqual(prediction.command, "/give @s minecraft:diamond 5")

    def test_summon_builds_a_summon_command(self):
        prediction = self.predict_with(
            {"action": "summon_entity", "id": "minecraft:zombie", "count": 1})
        self.assertEqual(prediction.entity_id, "minecraft:zombie")
        self.assertIsNone(prediction.item_id)
        self.assertEqual(prediction.command, "/summon minecraft:zombie ~ ~ ~")

    def test_hallucinated_identifier_is_not_filtered(self):
        # Ключевое свойство конфигурации: несуществующий предмет проходит насквозь.
        # В гибриде тот же случай даёт отказ, и в этом вся разница.
        prediction = self.predict_with(
            {"action": "give_item", "id": "minecraft:star_sword", "count": 1})
        self.assertEqual(prediction.outcome, "act")
        self.assertEqual(prediction.item_id, "minecraft:star_sword")

    def test_forbidden_thing_is_not_filtered(self):
        prediction = self.predict_with(
            {"action": "summon_entity", "id": "minecraft:ender_dragon", "count": 1})
        self.assertEqual(prediction.outcome, "act")

    def test_excessive_count_is_not_clamped(self):
        prediction = self.predict_with(
            {"action": "give_item", "id": "minecraft:diamond", "count": 1000})
        self.assertEqual(prediction.count, 1000)

    def test_chat_is_not_an_action(self):
        prediction = self.predict_with({"action": "none", "id": None, "count": 1})
        self.assertEqual(prediction.outcome, "none")

    def test_malformed_count_falls_back_to_one(self):
        prediction = self.predict_with(
            {"action": "give_item", "id": "minecraft:diamond", "count": "много"})
        self.assertEqual(prediction.count, 1)

    def test_empty_answer_is_not_an_action(self):
        self.assertEqual(self.predict_with({}).outcome, "none")

    def test_channel_failure_is_not_a_decision(self):
        """Недоступность модели помечается отдельно и в статистику решений не входит."""
        with patch.object(llm_only, "ask_json", side_effect=ModelUnavailable("нет сети")):
            prediction = llm_only.predict("реплика")
        self.assertEqual(prediction.outcome, "error")
        self.assertIn("нет сети", prediction.details["error"])


if __name__ == "__main__":
    unittest.main()
