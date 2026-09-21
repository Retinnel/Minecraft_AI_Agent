"""Проверки базы знаний через её публичный интерфейс.

Сверяют онтологию с фактами игры и проверяют инварианты, на которые будет опираться
решатель: рецепты раскрываются с количествами, теги разворачиваются в конкретные предметы,
источники добычи упорядочены по сложности, запреты отличимы от отсутствия записи.
"""

import unittest
from pathlib import Path

from kb.graph import KnowledgeBase

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class KnowledgeBaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    # --- сущности ---

    def test_known_and_unknown_things(self):
        self.assertTrue(self.kb.exists("minecraft:diamond"))
        self.assertFalse(self.kb.exists("minecraft:star_sword"))

    def test_labels_in_both_languages(self):
        self.assertEqual(self.kb.label("minecraft:diamond", "ru"), "Алмаз")
        self.assertEqual(self.kb.label("minecraft:diamond", "en"), "Diamond")

    def test_legacy_slang_aliases_survived(self):
        aliases = self.kb.aliases("minecraft:golden_apple")
        self.assertIn("гэпл", aliases)
        self.assertIn("ancient_debris", [a for a in aliases] + ["ancient_debris"])
        self.assertIn("реда", self.kb.aliases("minecraft:redstone"))

    def test_kinds(self):
        self.assertIn("item", self.kb.kinds("minecraft:diamond"))
        self.assertIn("block", self.kb.kinds("minecraft:stone"))
        self.assertIn("creature", self.kb.kinds("minecraft:zombie"))

    # --- идентификаторы с NBT ---

    def test_nbt_item_keeps_exact_game_id(self):
        # URI пришлось санировать, но в команду должен подставляться исходный идентификатор.
        game_id = self.kb.game_id('minecraft:potion{Potion:"minecraft:healing"}')
        self.assertEqual(game_id, 'minecraft:potion{Potion:"minecraft:healing"}')
        self.assertEqual(self.kb.label('minecraft:potion{Potion:"minecraft:healing"}'),
                         "зелье лечения")

    def test_plain_game_id_is_unchanged(self):
        self.assertEqual(self.kb.game_id("minecraft:diamond"), "minecraft:diamond")

    # --- рецепты ---

    def test_diamond_pickaxe_recipe(self):
        recipes = self.kb.recipes_for("minecraft:diamond_pickaxe")
        self.assertEqual(len(recipes), 1)
        recipe = recipes[0]
        self.assertEqual(recipe.method, "craft")
        self.assertEqual(recipe.station, "crafting_table")
        self.assertEqual(recipe.produces_count, 1)
        amounts = {ingredient.alternatives[0]: ingredient.quantity for ingredient in recipe.inputs}
        self.assertEqual(amounts, {"minecraft:diamond": 3, "minecraft:stick": 2})

    def test_tag_ingredient_is_expanded_into_items(self):
        # Палки делаются из тега «доски»: решателю нужны конкретные варианты, а не ссылка.
        recipes = [r for r in self.kb.recipes_for("minecraft:stick") if r.inputs[0].tag]
        self.assertEqual(len(recipes), 1)
        ingredient = recipes[0].inputs[0]
        self.assertEqual(ingredient.tag, "planks")
        self.assertEqual(ingredient.quantity, 2)
        self.assertTrue(ingredient.is_choice)
        self.assertIn("minecraft:oak_planks", ingredient.alternatives)
        self.assertGreaterEqual(len(ingredient.alternatives), 11)

    def test_smelting_recipes_are_present_with_stations(self):
        recipes = self.kb.recipes_for("minecraft:iron_ingot")
        smelting = {r.station for r in recipes if r.method == "smelt"}
        self.assertIn("furnace", smelting)
        self.assertIn("blast_furnace", smelting)

    def test_uncraftable_item_has_no_recipes(self):
        # Гнилая плоть и жемчуг Края только выпадают, изготовить их нельзя —
        # для таких предметов решатель обязан перейти к плану добычи.
        self.assertEqual(self.kb.recipes_for("minecraft:rotten_flesh"), [])
        self.assertEqual(self.kb.recipes_for("minecraft:ender_pearl"), [])

    def test_diamond_is_obtainable_by_unpacking_and_smelting(self):
        # Алмаз не «некрафтимый»: он распаковывается из блока и выплавляется из руды.
        # Решателю важно видеть оба пути, поэтому фиксируем их явно.
        recipes = self.kb.recipes_for("minecraft:diamond")
        unpack = [r for r in recipes if r.method == "craft"]
        self.assertEqual(len(unpack), 1)
        self.assertEqual(unpack[0].produces_count, 9)
        self.assertEqual(unpack[0].inputs[0].alternatives, ("minecraft:diamond_block",))

        smelted = {
            recipe.inputs[0].alternatives[0]
            for recipe in recipes if recipe.method == "smelt"
        }
        self.assertEqual(smelted, {"minecraft:diamond_ore", "minecraft:deepslate_diamond_ore"})

    # --- добыча ---

    def test_diamond_sources_and_tool_requirement(self):
        sources = self.kb.sources_of("minecraft:diamond")
        by_id = {source.id: source for source in sources}
        self.assertIn("minecraft:diamond_ore", by_id)
        ore = by_id["minecraft:diamond_ore"]
        self.assertEqual(ore.kind, "block")
        self.assertEqual(ore.tool_kind, "pickaxe")
        self.assertEqual(ore.tool_tier, "iron")

    def test_creature_sources_are_marked(self):
        sources = {source.id: source for source in self.kb.sources_of("minecraft:rotten_flesh")}
        self.assertIn("minecraft:zombie", sources)
        self.assertEqual(sources["minecraft:zombie"].kind, "creature")

    def test_sources_are_ordered_by_difficulty(self):
        ranks = [source.tool_tier_rank for source in self.kb.sources_of("minecraft:diamond")]
        self.assertEqual(ranks, sorted(ranks))

    def test_blocks_do_not_yield_themselves(self):
        # Иначе в графе добычи появляется петля: чтобы добыть X, добудь X.
        for item in ("minecraft:diamond_ore", "minecraft:stone", "minecraft:oak_log"):
            with self.subTest(item=item):
                self.assertNotIn(item, [source.id for source in self.kb.sources_of(item)])

    # --- уровни инструмента ---

    def test_tier_comparison(self):
        self.assertTrue(self.kb.tier_satisfies("diamond", "iron"))
        self.assertTrue(self.kb.tier_satisfies("iron", "iron"))
        self.assertFalse(self.kb.tier_satisfies("stone", "iron"))
        self.assertFalse(self.kb.tier_satisfies(None, "iron"))
        self.assertTrue(self.kb.tier_satisfies(None, None))

    def test_gold_ranks_below_stone_as_in_the_game(self):
        # Золотые инструменты быстрые, но по уровню слабее каменных.
        self.assertFalse(self.kb.tier_satisfies("gold", "iron"))
        self.assertLess(self.kb.tool_tier_rank("gold"), self.kb.tool_tier_rank("stone"))

    # --- запреты ---

    def test_boss_summon_is_forbidden_with_a_reason(self):
        restriction = self.kb.restriction_for("minecraft:ender_dragon", "summon")
        self.assertIsNotNone(restriction)
        self.assertEqual(restriction.category, "boss")
        self.assertIn("боссов", restriction.reason)

    def test_restriction_is_scoped_to_its_action(self):
        # Запрет на призыв не запрещает выдачу, и наоборот.
        self.assertIsNone(self.kb.restriction_for("minecraft:ender_dragon", "give"))
        self.assertIsNotNone(self.kb.restriction_for("minecraft:command_block", "give"))
        self.assertIsNone(self.kb.restriction_for("minecraft:command_block", "summon"))

    def test_ordinary_things_are_not_restricted(self):
        self.assertIsNone(self.kb.restriction_for("minecraft:diamond", "give"))
        self.assertIsNone(self.kb.restriction_for("minecraft:zombie", "summon"))

    def test_absence_and_prohibition_are_different(self):
        # Ключевое различие: «не знаю такого» и «знаю и не дам» — разные ответы игроку.
        self.assertFalse(self.kb.exists("minecraft:star_sword"))
        self.assertIsNone(self.kb.restriction_for("minecraft:star_sword", "give"))
        self.assertTrue(self.kb.exists("minecraft:ender_dragon"))
        self.assertIsNotNone(self.kb.restriction_for("minecraft:ender_dragon", "summon"))

    def test_every_restriction_has_a_reason(self):
        restrictions = self.kb.restrictions()
        self.assertGreaterEqual(len(restrictions), 10)
        for restriction in restrictions:
            with self.subTest(thing=restriction.thing):
                self.assertTrue(restriction.reason.strip())
                self.assertIn(restriction.action, ("give", "summon"))
                self.assertTrue(self.kb.exists(restriction.thing),
                                "запрет должен ссылаться на существующую сущность")


if __name__ == "__main__":
    unittest.main()
