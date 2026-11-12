"""Предупреждение об угрозе: существо рядом → класс угрозы → защита, достижимая из инвентаря.

Проверяется и сам вывод, и канал опроса: предупреждение приходит без реплики игрока, не
повторяется, пока его содержание не изменилось, и вспоминается в разборе смерти.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from api import advice
from api.player_state import apply_log_to_player_state, reset_player_state
from kb.graph import KnowledgeBase
from models.log_entry import LogEntry
from orchestrator import Pipeline, Proposal
from orchestrator.llm import ModelUnavailable
from orchestrator.responder import Responder
from orchestrator.session import Session
from solver.forewarn import ThreatAdvisor, normalize_mob, render_forewarning
from solver.postmortem import DeathAnalyzer

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"

SHIELD_KIT = {"minecraft:oak_planks": 6, "minecraft:iron_ingot": 1, "minecraft:crafting_table": 1}


class StubIntent:
    def extract(self, utterance, **context):
        return Proposal("none")


def silent(messages):
    raise ModelUnavailable("в тестах модель не формулирует")


def log(event_type, event_data):
    entry = LogEntry(level="INFO", event_type=event_type, event_data=event_data)
    advice.logs_db.append(entry)
    apply_log_to_player_state(entry)
    return entry


def snapshot(counts, offhand=()):
    log("inventory_snapshot", {
        "counts": dict(counts), "hotbar": [], "armor": [],
        "offhand": [{"slot": "offhand", "item_id": item, "count": 1} for item in offhand]})


class NormalizeTests(unittest.TestCase):
    def test_mod_and_page_spellings_meet(self):
        self.assertEqual(normalize_mob("entity.minecraft.creeper"), "minecraft:creeper")
        self.assertEqual(normalize_mob("minecraft:creeper"), "minecraft:creeper")
        self.assertEqual(normalize_mob("Creeper"), "minecraft:creeper")
        self.assertEqual(normalize_mob(""), "")


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class AdvisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)
        cls.advisor = ThreatAdvisor(DeathAnalyzer(cls.kb))

    def test_shield_craftable_from_inventory_is_offered(self):
        warning = self.advisor.assess(["entity.minecraft.creeper"], SHIELD_KIT)
        self.assertEqual(warning.threat, "explosion")
        self.assertTrue(warning.actionable)
        self.assertEqual(warning.state.craftable[0].item, "minecraft:shield")
        text = render_forewarning(warning, self.kb)
        self.assertIn("крипер", text)
        self.assertIn("взрыв", text)
        self.assertIn("щит", text)

    def test_shield_lying_idle_is_named_differently(self):
        warning = self.advisor.assess(["minecraft:skeleton"], {"minecraft:shield": 1})
        self.assertEqual(warning.threat, "ranged")
        self.assertEqual(warning.state.unused[0].item, "minecraft:shield")
        self.assertIn("Возьми в руку: щит", render_forewarning(warning, self.kb))

    def test_nothing_to_offer_means_not_actionable(self):
        empty = self.advisor.assess(["minecraft:creeper"], {})
        self.assertFalse(empty.actionable)
        protected = self.advisor.assess(["minecraft:skeleton"], {"minecraft:shield": 1},
                                        held=["minecraft:shield"])
        self.assertFalse(any(m.item == "minecraft:shield"
                             for m in protected.state.unused + protected.state.craftable))

    def test_fastest_killer_is_named_first(self):
        warning = self.advisor.assess(["minecraft:zombie", "minecraft:creeper"], SHIELD_KIT)
        self.assertEqual(warning.threat, "explosion")
        self.assertEqual(warning.mobs, ["minecraft:creeper"])

    def test_unlisted_hostile_fights_in_melee(self):
        warning = self.advisor.assess(["minecraft:zombie"], {"minecraft:iron_ingot": 8,
                                                             "minecraft:crafting_table": 1})
        self.assertEqual(warning.threat, "combat")
        self.assertTrue(warning.actionable)

    def test_unknown_or_harmless_gives_nothing(self):
        self.assertIsNone(self.advisor.assess(["minecraft:no_such_mob"], SHIELD_KIT))
        self.assertIsNone(self.advisor.assess([], SHIELD_KIT))


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class ForewarnPollTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = Pipeline(StubIntent(), KnowledgeBase.load(KB_PATH))

    def setUp(self):
        advice.logs_db.clear()
        advice._processed_log_ids.clear()
        advice._death_analyzer_instance = None
        advice._last_forewarning = None
        reset_player_state()
        self.patches = [
            patch.object(advice, "_assistant_pipeline", return_value=self.pipeline),
            patch.object(advice, "_assistant_responder", return_value=Responder(ask_text=silent)),
            patch.object(advice, "_assistant_session", return_value=Session()),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        advice.logs_db.clear()
        advice._death_analyzer_instance = None
        advice._last_forewarning = None

    def near(self, *mobs):
        log("near_hostile", {"radius": 14.0, "count": len(mobs), "types": list(mobs)})

    async def test_warning_arrives_without_a_player_utterance(self):
        snapshot(SHIELD_KIT)
        self.near("entity.minecraft.creeper")
        reply = await advice.get_rp_response()
        self.assertEqual(reply.source, "forewarn")
        self.assertEqual(reply.level, "WARNING")
        self.assertIn("щит", reply.response)
        self.assertEqual(reply.threats, ["Крипер"])
        self.assertTrue(reply.plan_tree)
        self.assertFalse(reply.execute)

    async def test_same_warning_is_not_repeated(self):
        snapshot(SHIELD_KIT)
        self.near("minecraft:creeper")
        first = await advice.get_rp_response()
        self.near("minecraft:creeper")
        second = await advice.get_rp_response()
        self.assertEqual(first.source, "forewarn")
        self.assertNotEqual(second.source, "forewarn")

    async def test_new_content_passes_at_once(self):
        snapshot(SHIELD_KIT)
        self.near("minecraft:creeper")
        await advice.get_rp_response()
        # Игрок собрал щит, но в руку не взял: совет теперь другой.
        snapshot({"minecraft:shield": 1})
        self.near("minecraft:creeper")
        reply = await advice.get_rp_response()
        self.assertEqual(reply.source, "forewarn")
        self.assertIn("Возьми в руку", reply.response)

    async def test_silence_when_there_is_nothing_to_offer(self):
        snapshot({})
        self.near("minecraft:creeper")
        reply = await advice.get_rp_response()
        self.assertNotEqual(reply.source, "forewarn")

    async def test_postmortem_recalls_the_warning(self):
        snapshot(SHIELD_KIT)
        self.near("minecraft:creeper")
        await advice.get_rp_response()
        log("death", {"cause": "explosion.player"})
        snapshot({})
        reply = await advice.get_rp_response()
        self.assertTrue(reply.source.startswith("postmortem"))
        self.assertIn("Предупреждение об этой угрозе было", reply.response)
        self.assertIn("щит", reply.response)

    async def test_mobs_seen_before_death_do_not_warn_after_it(self):
        snapshot(SHIELD_KIT)
        self.near("minecraft:creeper")
        log("death", {"cause": "explosion.player"})
        first = await advice.get_rp_response()
        second = await advice.get_rp_response()
        self.assertTrue(first.source.startswith("postmortem"))
        self.assertNotEqual(second.source, "forewarn")


if __name__ == "__main__":
    unittest.main()
