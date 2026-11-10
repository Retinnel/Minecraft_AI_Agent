"""Разбор смерти в канале опроса: снимок инвентаря, событие смерти, ответ моду.

Инвентарь для разбора берётся тот, с которым игрок погиб: снимок, пришедший после смерти,
уже пуст и разбор менять не должен.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from api import advice
from api.player_state import apply_log_to_player_state, get_player_state, reset_player_state
from kb.graph import KnowledgeBase
from models.log_entry import LogEntry
from orchestrator import Pipeline, Proposal
from orchestrator.llm import ModelUnavailable
from orchestrator.responder import MODEL, TEMPLATE, Responder
from orchestrator.session import Session

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"


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


def snapshot(counts, armor=()):
    log("inventory_snapshot", {
        "counts": dict(counts), "hotbar": [], "offhand": [],
        "armor": [{"slot": "chestplate", "item_id": item, "count": 1} for item in armor]})


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class PostMortemPollTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = Pipeline(StubIntent(), KnowledgeBase.load(KB_PATH))

    def setUp(self):
        advice.logs_db.clear()
        advice._processed_log_ids.clear()
        advice._death_analyzer_instance = None
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

    async def test_death_is_analysed_against_the_inventory_at_that_moment(self):
        snapshot({"minecraft:iron_ingot": 8, "minecraft:crafting_table": 1})
        log("death", {"cause": "explosion.player"})
        # После смерти инвентарь пуст: этот снимок на разбор влиять не должен.
        snapshot({})

        self.assertEqual(get_player_state()["last_death"]["inventory"]["counts"]
                         ["minecraft:iron_ingot"], 8)

        reply = await advice.get_rp_response()
        self.assertEqual(reply.source, "postmortem-template")
        self.assertEqual(reply.level, "CRITICAL")
        self.assertIn("взрыв", reply.response)
        self.assertIn("железный нагрудник", reply.response)
        self.assertFalse(reply.execute)
        self.assertTrue(reply.plan_tree)
        self.assertTrue(any("нагрудник" in line.lower() for line in reply.explanation))

    async def test_death_is_analysed_once(self):
        snapshot({"minecraft:iron_ingot": 8, "minecraft:crafting_table": 1})
        log("death", {"cause": "mob_attack"})
        first = await advice.get_rp_response()
        second = await advice.get_rp_response()
        self.assertTrue(first.source.startswith("postmortem"))
        self.assertFalse(second.source.startswith("postmortem"))

    async def test_worn_armor_is_reported(self):
        snapshot({"minecraft:iron_chestplate": 1}, armor=["minecraft:iron_chestplate"])
        log("death", {"cause": "mob_attack"})
        reply = await advice.get_rp_response()
        self.assertTrue(any("ближний бой" in line for line in reply.explanation))
        self.assertNotIn("Брони на тебе не было", reply.response)

    async def test_model_retelling_must_keep_the_measure(self):
        snapshot({"minecraft:iron_ingot": 8, "minecraft:crafting_table": 1})
        log("death", {"cause": "mob_attack"})
        keeps = Responder(ask_text=lambda m: "Эх. А ведь слитков хватало на железный нагрудник.")
        with patch.object(advice, "_assistant_responder", return_value=keeps):
            reply = await advice.get_rp_response()
        self.assertEqual(reply.source, "postmortem")
        self.assertIn("нагрудник", reply.response)

    def test_retelling_that_loses_the_measure_falls_back_to_the_facts(self):
        loses = Responder(ask_text=lambda m: "Бывает. В следующий раз будь осторожнее.")
        reply = loses.phrase("Причина смерти: взрыв. Собирался железный нагрудник.",
                             ["Железный нагрудник"])
        self.assertEqual(reply.source, TEMPLATE)
        self.assertIn("нагрудник", reply.text)
        kept = Responder(ask_text=lambda m: "Железного нагрудника тебе не хватило.")
        self.assertEqual(kept.phrase("…", ["Железный нагрудник"]).source, MODEL)


if __name__ == "__main__":
    unittest.main()
