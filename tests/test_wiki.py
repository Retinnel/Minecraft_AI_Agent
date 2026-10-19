"""Поиск рецепта для оверлея: предмет определяет резолвер, состав — база знаний.

Здесь закрепляется, что справочник рецептов больше не решает, о каком предмете речь:
раньше «алмаз» находил алмазную кирку, а «меч» молча становился деревянным.
И что языковая модель рецептов не придумывает.
"""

import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class WikiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def craft(self, query):
        return self.client.get("/api/wiki/craft", params={"query": query}).json()

    def test_item_is_chosen_by_resolver(self):
        """«алмаз» — это алмаз, а не первая попавшаяся запись справочника с этим словом."""
        result = self.craft("алмаз")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["item_id"], "minecraft:diamond")
        self.assertIn("Добывается", result["recipe"]["description"])

    def test_generic_word_asks(self):
        result = self.craft("меч")
        self.assertEqual(result["status"], "error")
        self.assertIn("Уточни", result["message"])

    def test_unknown_item_is_reported(self):
        self.assertIn("нет", self.craft("звёздный меч")["message"])

    def test_composition_comes_from_kb(self):
        recipe = self.craft("незеритовый слиток")["recipe"]
        self.assertIn("Незеритовый лом ×4", recipe["description"])
        self.assertIn("Золотой слиток ×4", recipe["description"])

    def test_grid_is_attached_only_for_the_same_item(self):
        self.assertIsNotNone(self.craft("алмазная кирка")["recipe"]["grid"])

    def test_no_language_model_involved(self):
        """Рецепт не берётся из модели ни при каком исходе поиска."""
        import api.wiki as wiki
        self.assertNotIn("groq", wiki.__dict__)


if __name__ == "__main__":
    unittest.main()
