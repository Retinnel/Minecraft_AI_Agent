"""Проверки резолвера: сопоставление свободной речи с сущностями онтологии.

Здесь закрепляются не столько отдельные удачные разборы, сколько принятая политика:
родовое слово ведёт к уточняющему вопросу, несуществующий предмет — к отказу, а запрещённая
сущность сначала опознаётся и лишь потом отклоняется валидатором. Различие между «не знаю
такого» и «знаю и не дам» держится именно на этих проверках.
"""

import unittest
from pathlib import Path

from kb.graph import KnowledgeBase
from resolver.resolve import AMBIGUOUS, NOT_FOUND, RESOLVED, Resolver

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class ResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resolver = Resolver(KnowledgeBase.load(KB_PATH))

    def resolve(self, text, action="give_item"):
        return self.resolver.resolve(text, action=action)

    # --- обычные запросы ---

    def test_exact_name(self):
        result = self.resolve("дай алмаз")
        self.assertEqual(result.status, RESOLVED)
        self.assertEqual(result.thing, "minecraft:diamond")

    def test_count_does_not_affect_target(self):
        """Известный промах прошлой версии: «5 алмазов» резолвилось в блок алмазов."""
        result = self.resolve("дай мне 5 алмазов")
        self.assertEqual(result.thing, "minecraft:diamond")

    def test_word_form(self):
        """Стеммер снимает падеж, а беглую гласную закрывает нечёткое сравнение основ."""
        result = self.resolve("мне бы 10 наковален")
        self.assertEqual(result.thing, "minecraft:anvil")

    def test_colloquial_name(self):
        self.assertEqual(self.resolve("дай гэпл").thing, "minecraft:golden_apple")
        self.assertEqual(self.resolve("нужна реда").thing, "minecraft:redstone")

    def test_adjective_narrows_target(self):
        result = self.resolve("хочу алмазную кирку")
        self.assertEqual(result.thing, "minecraft:diamond_pickaxe")

    def test_explicit_identifier(self):
        result = self.resolve("дай minecraft:emerald")
        self.assertEqual(result.thing, "minecraft:emerald")
        self.assertEqual(result.method, "explicit")

    # --- политика: родовое слово требует уточнения ---

    def test_generic_word_is_ambiguous(self):
        """Ключевая проверка: захардкоженного умолчания «меч» → алмазный меч больше нет."""
        result = self.resolve("дай меч")
        self.assertEqual(result.status, AMBIGUOUS)
        self.assertGreater(len(result.candidates), 1)
        self.assertTrue(all(c.thing.endswith("_sword") for c in result.candidates))

    def test_generic_words_across_classes(self):
        for text in ("нужна кирка", "дай доски", "дай лодку"):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text).status, AMBIGUOUS)

    def test_synonym_does_not_outrank_class(self):
        """Синоним, оказавшийся названием класса, в индекс не попадает.

        Иначе «меч» разрешалось бы в алмазный меч только потому, что синоним когда-то
        записали именно ему.
        """
        names = self.resolver.index._names
        aliases = {name.text for name in names if name.thing == "minecraft:diamond_sword"}
        self.assertNotIn("меч", aliases)
        self.assertIn("Алмазный меч", aliases)

    # --- политика: несуществующее отличается от неоднозначного ---

    def test_unknown_item_is_not_found(self):
        for text in ("дай мне звёздный меч", "выдай сто алмазных драконов"):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text).status, NOT_FOUND)

    def test_unknown_creature_is_not_found(self):
        self.assertEqual(self.resolve("призови кракена", "summon_entity").status, NOT_FOUND)

    def test_empty_query(self):
        self.assertEqual(self.resolve("").status, NOT_FOUND)

    # --- политика: запрещённое опознаётся, а не теряется ---

    def test_restricted_entity_is_recognised(self):
        """Без этого запрет недостижим: система ответила бы «не знаю такого».

        Официальное название — «Эндер-дракон», игрок говорит «дракон края». Связь задана
        рукописным слоем kb/aliases.json.
        """
        result = self.resolve("призови дракона края", "summon_entity")
        self.assertEqual(result.status, RESOLVED)
        self.assertEqual(result.thing, "minecraft:ender_dragon")

    def test_restricted_block_is_recognised(self):
        result = self.resolve("дай командный блок")
        self.assertEqual(result.thing, "minecraft:command_block")

    # --- вид сущности отбирается по действию ---

    def test_action_filters_kind(self):
        self.assertEqual(self.resolve("призови зомби", "summon_entity").thing, "minecraft:zombie")
        # Предмет не может быть призван, существо не может быть выдано.
        self.assertNotEqual(self.resolve("призови алмаз", "summon_entity").thing, "minecraft:diamond")
        self.assertNotEqual(self.resolve("дай зомби", "give_item").thing, "minecraft:zombie")


if __name__ == "__main__":
    unittest.main()
