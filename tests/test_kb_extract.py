"""Проверки извлечённых данных игры.

Сверяют результат kb.extract с фактами, известными про Minecraft 1.20.1: рецепты собираются
верно, теги раскрываются рекурсивно, требования к инструменту и выпадение читаются из
лут-таблиц. Это защита от тихой порчи базы знаний при изменении извлекателя.

Если данные ещё не извлечены, тесты пропускаются: jar игры появляется только после сборки
мода, а локаль — после kb.fetch_locale.
"""

import json
import unittest
from pathlib import Path

GAME_DIR = Path(__file__).resolve().parent.parent / "data" / "game"
REQUIRED = ("items.json", "recipes.json", "tags.json", "drops.json", "tools.json")


def _load(name):
    return json.loads((GAME_DIR / name).read_text(encoding="utf-8"))


@unittest.skipUnless(
    all((GAME_DIR / name).exists() for name in REQUIRED),
    "данные игры не извлечены: запусти kb.fetch_locale и kb.extract",
)
class ExtractedGameDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = _load("items.json")
        cls.recipes = _load("recipes.json")
        cls.tags = _load("tags.json")
        cls.drops = _load("drops.json")
        cls.tools = _load("tools.json")
        cls.by_result = {}
        for recipe in cls.recipes:
            cls.by_result.setdefault(recipe["result"]["item"], []).append(recipe)

    # --- объёмы ---

    def test_volumes_are_plausible(self):
        self.assertGreater(len(self.items), 1000)
        self.assertGreater(len(self.recipes), 1000)
        self.assertGreater(len(self.tools), 500)

    def test_every_entry_has_russian_name(self):
        missing = [key for key, entry in self.items.items() if not entry.get("name_ru")]
        self.assertEqual(missing, [], f"без русского названия: {missing[:10]}")

    # --- рецепты ---

    def test_diamond_pickaxe_recipe(self):
        recipes = self.by_result["minecraft:diamond_pickaxe"]
        self.assertEqual(len(recipes), 1)
        recipe = recipes[0]
        self.assertEqual(recipe["method"], "craft")
        self.assertEqual(recipe["station"], "crafting_table")
        self.assertEqual(recipe["result"]["count"], 1)
        amounts = {
            ingredient["alternatives"][0]["id"]: ingredient["count"]
            for ingredient in recipe["inputs"]
        }
        self.assertEqual(amounts, {"minecraft:diamond": 3, "minecraft:stick": 2})

    def test_stick_recipe_yields_four_from_two_planks(self):
        planks = [
            recipe for recipe in self.by_result["minecraft:stick"]
            if recipe["inputs"][0]["alternatives"][0]["id"] == "minecraft:planks"
        ]
        self.assertEqual(len(planks), 1)
        recipe = planks[0]
        self.assertEqual(recipe["result"]["count"], 4)
        self.assertEqual(recipe["inputs"][0]["count"], 2)
        self.assertEqual(recipe["inputs"][0]["alternatives"][0]["kind"], "tag")

    def test_shapeless_recipe_counts_repeated_ingredients(self):
        # Железный слиток из девяти самородков — бесформенный рецепт с повтором.
        nuggets = [
            recipe for recipe in self.by_result["minecraft:iron_ingot"]
            if recipe["inputs"] and recipe["inputs"][0]["alternatives"][0]["id"] == "minecraft:iron_nugget"
        ]
        self.assertEqual(len(nuggets), 1)
        self.assertEqual(nuggets[0]["inputs"][0]["count"], 9)

    def test_smelting_stations_are_distinguished(self):
        # Плавка руды доступна и в печи, и в доменной печи: это разные рецепты, не дубликаты.
        stations = {
            recipe["station"]
            for recipe in self.by_result["minecraft:iron_ingot"]
            if recipe["method"] == "smelt"
            and recipe["inputs"][0]["alternatives"][0]["id"] == "minecraft:iron_ore"
        }
        self.assertEqual(stations, {"furnace", "blast_furnace"})

    def test_every_recipe_has_inputs_and_result(self):
        for recipe in self.recipes:
            with self.subTest(recipe=recipe["id"]):
                self.assertTrue(recipe["inputs"])
                self.assertGreaterEqual(recipe["result"]["count"], 1)
                for ingredient in recipe["inputs"]:
                    self.assertTrue(ingredient["alternatives"])
                    self.assertGreaterEqual(ingredient["count"], 1)

    def test_recipe_references_resolve(self):
        known_items = set(self.items)
        known_tags = set(self.tags["items"])
        for recipe in self.recipes:
            with self.subTest(recipe=recipe["id"]):
                self.assertIn(recipe["result"]["item"], known_items)
                for ingredient in recipe["inputs"]:
                    for alternative in ingredient["alternatives"]:
                        pool = known_tags if alternative["kind"] == "tag" else known_items
                        self.assertIn(alternative["id"], pool)

    # --- теги ---

    def test_plank_tag_lists_all_wood_types(self):
        planks = self.tags["items"]["minecraft:planks"]
        self.assertEqual(len(planks), 11)
        self.assertIn("minecraft:oak_planks", planks)

    def test_nested_tags_are_resolved(self):
        # minecraft:logs ссылается только на другие теги и должен раскрыться до предметов.
        logs = self.tags["items"]["minecraft:logs"]
        self.assertGreater(len(logs), 30)
        self.assertFalse([value for value in logs if value.startswith("#")])
        self.assertIn("minecraft:oak_log", logs)

    # --- добыча ---

    def test_diamond_ore_drops_diamond(self):
        self.assertIn("minecraft:diamond", self.drops["blocks"]["minecraft:diamond_ore"])

    def test_zombie_drops_rotten_flesh(self):
        self.assertIn("minecraft:rotten_flesh", self.drops["entities"]["minecraft:zombie"])

    def test_tool_tiers_match_the_game(self):
        self.assertEqual(self.tools["minecraft:diamond_ore"]["tier"], "iron")
        self.assertEqual(self.tools["minecraft:iron_ore"]["tier"], "stone")
        # Камень добывается любой киркой, уровень не требуется.
        self.assertEqual(self.tools["minecraft:stone"]["tool"], "pickaxe")
        self.assertNotIn("tier", self.tools["minecraft:stone"])

    def test_tool_is_required_only_where_it_gates_the_drop(self):
        # Камень и руды без кирки не выпадают, дерево же добывается руками.
        self.assertTrue(self.tools["minecraft:stone"]["tool_required"])
        self.assertTrue(self.tools["minecraft:diamond_ore"]["tool_required"])
        self.assertFalse(self.tools["minecraft:oak_log"]["tool_required"])

    def test_unreliable_drops_are_flagged(self):
        # Мёртвый куст роняет от нуля до двух палок — на таком источнике план не строят.
        self.assertTrue(self.drops["blocks"]["minecraft:dead_bush"]["minecraft:stick"])
        # Алмазная руда даёт алмаз всегда.
        self.assertFalse(self.drops["blocks"]["minecraft:diamond_ore"]["minecraft:diamond"])

    def test_drop_targets_are_known_items(self):
        known_items = set(self.items)
        for source, items in self.drops["blocks"].items():
            with self.subTest(source=source):
                for item in items:
                    self.assertIn(item, known_items)


if __name__ == "__main__":
    unittest.main()
