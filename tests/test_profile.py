"""Профиль игрока: наблюдения поверх игр, черты по правилам с порогом свидетельств."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from api import profile
from orchestrator.pipeline import Turn
from orchestrator.responder import Responder


def _feed(state, game, action, outcome="act", times=1, category=None):
    for _ in range(times):
        profile.observe(state, game, action, outcome, category)


def _keys(state):
    return {trait["key"] for trait in profile.traits(state)}


class TraitRuleTests(unittest.TestCase):
    def test_no_trait_from_a_single_remark(self):
        state = profile._empty()
        _feed(state, "Minecraft", "plan_goal", times=profile.MIN_EVIDENCE - 1)
        self.assertEqual(profile.traits(state), [])
        self.assertEqual(profile.as_prompt(state), "")

    def test_self_reliant_player(self):
        state = profile._empty()
        _feed(state, "Minecraft", "plan_goal", times=6)
        _feed(state, "Minecraft", "give_item", times=1)
        self.assertIn("self_reliant", _keys(state))
        self.assertNotIn("wants_ready", _keys(state))

    def test_player_who_wants_things_ready(self):
        state = profile._empty()
        _feed(state, "Minecraft", "give_item", times=5)
        _feed(state, "Minecraft", "execute_plan", times=2)
        _feed(state, "Minecraft", "plan_goal", times=1)
        self.assertIn("wants_ready", _keys(state))

    def test_mixed_behaviour_gives_no_verdict(self):
        state = profile._empty()
        _feed(state, "Minecraft", "plan_goal", times=4)
        _feed(state, "Minecraft", "give_item", times=4)
        self.assertFalse({"self_reliant", "wants_ready"} & _keys(state))

    def test_evidence_from_different_games_adds_up(self):
        state = profile._empty()
        _feed(state, "Minecraft", "plan_goal", times=3)
        _feed(state, "Terraria", "plan_goal", times=3)
        keys = _keys(state)
        self.assertIn("self_reliant", keys)
        self.assertIn("games", keys)
        games = next(t for t in profile.traits(state) if t["key"] == "games")
        self.assertIn("Terraria — 3", games["basis"])

    def test_clarifications_and_focus(self):
        state = profile._empty()
        _feed(state, "Minecraft", "plan_goal", outcome="clarify", times=3)
        _feed(state, "Minecraft", "plan_goal", times=5, category=profile.WEAPONS)
        keys = _keys(state)
        self.assertIn("generic_names", keys)
        self.assertIn("focus_weapons", keys)

    def test_repeated_death_cause(self):
        state = profile._empty()
        state["deaths"] = 4
        state["threats"] = {"падение": 3, "взрыв": 1}
        trait = next(t for t in profile.traits(state) if t["key"] == "dies_of")
        self.assertIn("падение", trait["text"])
        self.assertEqual(trait["basis"], "3 из 4 смертей")

    def test_small_talk_is_not_a_request(self):
        state = profile._empty()
        _feed(state, "Minecraft", None, outcome="none", times=10)
        self.assertEqual(state["kinds"], {profile.TALK: 10})
        self.assertEqual(profile.traits(state), [])

    def test_every_trait_names_its_basis(self):
        state = profile._empty()
        _feed(state, "Minecraft", "ask_drops", times=6)
        _feed(state, "Minecraft", "start_campaign", times=5)
        for trait in profile.traits(state):
            self.assertTrue(trait["basis"])
            self.assertGreaterEqual(trait["evidence"], profile.MIN_EVIDENCE)


class CategoryTests(unittest.TestCase):
    def test_category_by_generic_word_works_across_games(self):
        self.assertEqual(profile.category("minecraft:diamond_sword"), profile.WEAPONS)
        self.assertEqual(profile.category("terraria:iron_broadsword"), profile.WEAPONS)
        self.assertIsNone(profile.category("somegame:rainbow"))
        self.assertEqual(profile.category("terraria:wooden_sword"), profile.WEAPONS)
        self.assertEqual(profile.category("minecraft:iron_chestplate"), profile.ARMOR)
        self.assertEqual(profile.category("terraria:gold_pickaxe"), profile.TOOLS)
        self.assertIsNone(profile.category("minecraft:torch"))
        self.assertIsNone(profile.category(None))

    def test_tool_is_decided_by_the_ontology_first(self):
        kb = SimpleNamespace(tool_profile=lambda item: object())
        self.assertEqual(profile.category("somegame:drill", kb), profile.TOOLS)


class StorageTests(unittest.TestCase):
    def test_nothing_is_written_without_an_active_world(self):
        with patch.object(profile, "_enabled", return_value=False), \
                patch.object(profile, "_save") as save:
            profile.observe_turn("Minecraft", Turn(outcome="act", action="plan_goal"))
            profile.observe_death("Minecraft", "падение")
            self.assertEqual(profile.prompt_line(), "")
            save.assert_not_called()

    def test_same_death_is_counted_once(self):
        state = profile._empty()
        with patch.object(profile, "_enabled", return_value=True), \
                patch.object(profile, "_save"), patch.object(profile, "_state", state), \
                patch.object(profile, "_last_death_key", None):
            profile.observe_death("Minecraft", "падение", key="t1")
            profile.observe_death("Minecraft", "падение", key="t1")
            profile.observe_death("Minecraft", "падение", key="t2")
            self.assertEqual(state["deaths"], 2)


class ResponderProfileTests(unittest.TestCase):
    def test_profile_reaches_only_free_talk(self):
        seen = []

        def ask(messages):
            seen.append(messages[0]["content"])
            return "Привет!"

        responder = Responder(ask_text=ask)
        line = "Об игроке (по наблюдениям во всех играх): предпочитает делать сам."
        responder.reply(Turn(outcome="none"), "привет", profile=line)
        responder.reply(Turn(outcome="none", game="Terraria"), "привет", profile=line)
        self.assertTrue(all(line in prompt for prompt in seen))
        # В подсказке профиль стоит в конце: неизменное начало остаётся в кэше провайдера.
        self.assertTrue(seen[1].endswith(line))

        seen.clear()
        responder.reply(Turn(outcome="reject", action="give_item", reason="Нельзя."), "дай",
                        profile=line)
        self.assertFalse(any(line in prompt for prompt in seen))


if __name__ == "__main__":
    unittest.main()
