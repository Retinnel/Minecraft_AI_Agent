"""Вопросы о мире: обратные запросы к онтологии и их путь через конвейер.

Модель в тестах — заглушка: она только называет вид вопроса и предмет словами игрока.
Проверяется, что ответ целиком берётся из базы знаний и что пересказ, потерявший
название, заменяется текстом решателя.
"""

import unittest
from pathlib import Path

from kb.graph import KnowledgeBase
from orchestrator import Pipeline, Proposal
from orchestrator.extractor import ACTION_HINTS, build_prompt
from orchestrator.responder import MODEL, TEMPLATE, Responder
from solver.validator import ACT, CLARIFY, REJECT, SURVIVAL
from solver.worldfacts import (AVAILABLE, DROPS, SOURCES, USES, WORLD_ACTIONS, WorldFacts,
                               render_answer)

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


def mc(name):
    return f"minecraft:{name}"


class Scripted:
    """Заглушка модели: на любую реплику отвечает заранее заданным намерением."""

    def __init__(self, action, target=None):
        self.proposal = Proposal(action, target=target)

    def extract(self, utterance, **context):
        return self.proposal


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class WorldFactsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.facts = WorldFacts(cls.kb)

    def test_uses_lists_what_the_item_goes_into(self):
        answer = self.facts.uses(mc("gunpowder"))
        self.assertIn(mc("tnt"), answer.items)
        self.assertEqual(answer.total, len(answer.items))
        iron = self.facts.uses(mc("iron_ingot"))
        self.assertGreater(iron.total, len(iron.items))
        # Инструменты и оборудование называются первыми.
        self.assertTrue(any(item.endswith(("_pickaxe", "_axe", "_shovel", "_hoe"))
                            for item in iron.items[:2]))
        self.assertIn("и ещё", render_answer(self.facts.available(
            {mc("iron_ingot"): 8, mc("crafting_table"): 1, mc("stick"): 2}), self.kb))

    def test_sources_tell_reliable_from_unreliable_and_name_the_tool(self):
        diamond = self.facts.sources(mc("diamond"))
        self.assertIn(mc("diamond_ore"), diamond.items)
        self.assertIn("железная кирка", diamond.notes[mc("diamond_ore")])
        gunpowder = self.facts.sources(mc("gunpowder"))
        self.assertEqual(gunpowder.items, [])
        self.assertIn(mc("creeper"), gunpowder.rare)
        self.assertIn("не каждый раз", render_answer(gunpowder, self.kb))
        self.assertIn("в Незере", gunpowder.notes[mc("ghast")])

    def test_built_blocks_are_not_named_as_sources(self):
        obsidian = self.facts.sources(mc("obsidian"))
        self.assertNotIn(mc("ender_chest"), obsidian.items + obsidian.rare)

    def test_unpacking_a_storage_block_is_not_a_way_to_get_the_item(self):
        # Алмаз «делается» из алмазного блока, но это распаковка, а не способ получения.
        recipes = self.facts._real_recipes(mc("diamond"))
        self.assertFalse(any(
            mc("diamond_block") in ingredient.alternatives
            for recipe in recipes for ingredient in recipe.inputs))
        bread = self.facts.sources(mc("bread"))
        self.assertTrue(bread.craftable)
        self.assertIn("изготавливается", render_answer(bread, self.kb))

    def test_drops_of_creature_and_block(self):
        self.assertIn(mc("gunpowder"), self.facts.drops(mc("creeper")).rare)
        self.assertEqual(self.facts.drops(mc("iron_ore")).items, [mc("raw_iron")])

    def test_available_needs_ingredients_and_the_station(self):
        kit = {mc("iron_ingot"): 8, mc("stick"): 2}
        without_table = self.facts.available(kit)
        self.assertNotIn(mc("iron_pickaxe"), without_table.items)
        with_table = self.facts.available({**kit, mc("crafting_table"): 1})
        self.assertIn(mc("iron_pickaxe"), with_table.items)
        self.assertEqual(self.facts.available({}).items, [])
        self.assertIn("ничего не собрать", render_answer(self.facts.available({}), self.kb))

    def test_ingredients_share_one_stock(self):
        # На кирку нужно три слитка и две палки: двух слитков мало, хотя «слитки есть».
        short = self.facts.available({mc("iron_ingot"): 2, mc("stick"): 2,
                                      mc("crafting_table"): 1})
        self.assertNotIn(mc("iron_pickaxe"), short.items)


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class WorldPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def ask(self, action, target=None, **kwargs):
        return Pipeline(Scripted(action, target), self.kb).handle("…", **kwargs)

    def test_question_is_answered_from_the_knowledge_base(self):
        turn = self.ask(USES, "порох")
        self.assertEqual(turn.outcome, ACT)
        self.assertEqual(turn.target, mc("gunpowder"))
        self.assertIsNone(turn.command)
        self.assertIn("динамит", turn.world_summary)
        self.assertEqual(turn.stages["world"]["question"], USES)

    def test_drops_resolve_creatures(self):
        turn = self.ask(DROPS, "крипер")
        self.assertEqual(turn.target, mc("creeper"))
        self.assertIn("порох", turn.world_summary)

    def test_forbidden_to_summon_is_not_forbidden_to_ask_about(self):
        turn = self.ask(DROPS, "дракон края")
        self.assertEqual(turn.outcome, ACT)
        self.assertEqual(turn.target, mc("ender_dragon"))

    def test_available_takes_no_target_and_reads_the_inventory(self):
        turn = self.ask(AVAILABLE, None, inventory={mc("oak_log"): 3})
        self.assertEqual(turn.outcome, ACT)
        self.assertIn("дубовые доски", turn.world_summary)

    def test_unknown_subject_is_rejected_and_missing_subject_is_asked_for(self):
        self.assertEqual(self.ask(SOURCES, "флюгегехаймен").outcome, REJECT)
        self.assertEqual(self.ask(USES, None).outcome, CLARIFY)

    def test_ambiguous_subject_is_clarified_and_the_answer_continues_the_question(self):
        pipeline = Pipeline(Scripted(USES, "меч"), self.kb)
        first = pipeline.handle("что можно сделать из меча")
        self.assertEqual(first.outcome, CLARIFY)
        self.assertTrue(first.options)
        second = pipeline.handle("алмазный", pending=first)
        self.assertEqual(second.outcome, ACT)
        self.assertEqual(second.action, USES)
        self.assertEqual(second.target, mc("diamond_sword"))

    def test_questions_are_available_in_survival_too(self):
        pipeline = Pipeline(Scripted(SOURCES, "алмаз"), self.kb)
        for action in WORLD_ACTIONS:
            self.assertIn(action, pipeline.actions_for(SURVIVAL))
        self.assertEqual(pipeline.handle("…", mode=SURVIVAL).outcome, ACT)

    def test_model_is_told_about_every_question(self):
        prompt = build_prompt(tuple(ACTION_HINTS))
        for action in WORLD_ACTIONS:
            self.assertIn(f'"{action}"', prompt)

    def test_retelling_must_keep_every_named_item(self):
        turn = self.ask(USES, "порох")
        full = Responder(ask_text=lambda m: "Порох идёт на фейерверк, огненный заряд и динамит.")
        self.assertEqual(full.reply(turn, "…").source, MODEL)
        lossy = Responder(ask_text=lambda m: "Из пороха делают динамит и много чего ещё.")
        reply = lossy.reply(turn, "…")
        self.assertEqual(reply.source, TEMPLATE)
        self.assertIn("фейерверк", reply.text)


if __name__ == "__main__":
    unittest.main()
