"""Окно ассистента: свой разговор на каждую игру, команд не отдаёт."""

import unittest

from api import window
from orchestrator.pipeline import Pipeline, Proposal
from orchestrator.responder import Responder
from desktop.window import page


class Extractor:
    def __init__(self, action, target=None):
        self.action, self.target, self.calls = action, target, []

    def extract(self, utterance, **kwargs):
        self.calls.append(kwargs)
        return Proposal(action=self.action, target=self.target)


def responder():
    return Responder(ask_text=lambda messages: "")


class WindowTests(unittest.TestCase):
    def setUp(self):
        window._sessions.clear()
        self.addCleanup(window._sessions.clear)

    def test_game_name_is_canonical(self):
        self.assertEqual(window.canonical("террария"), "Terraria")
        self.assertEqual(window.canonical("майнкрафт"), "Minecraft")
        self.assertEqual(window.canonical(""), "Minecraft")
        self.assertEqual(window.canonical("Elden  Ring"), "Elden Ring")

    def test_games_with_knowledge_base_are_listed(self):
        names = [entry["game"] for entry in window.games_list()]
        self.assertEqual(names[0], "Minecraft")
        self.assertIn("Terraria", names)

    def test_plan_for_the_second_world_without_touching_the_chat_session(self):
        from api import advice
        before = advice._assistant_session().game
        extractor = Extractor("plan_goal", "золотая кирка")
        result = window.answer("как сделать золотую кирку", "террария", Pipeline(extractor),
                               responder())
        self.assertEqual((result["game"], result["outcome"]), ("Terraria", "act"))
        self.assertTrue(result["knowledge_base"])
        self.assertIn("Золотая кирка", result["plan_tree"])
        self.assertEqual(advice._assistant_session().game, before)

    def test_each_game_has_its_own_conversation(self):
        pipeline = Pipeline(Extractor("plan_goal", "кирка"))
        asked = window.answer("хочу кирку", "Terraria", pipeline, responder())
        self.assertEqual(asked["outcome"], "clarify")
        self.assertIn("Кошмарная кирка", asked["options"])
        self.assertIsNotNone(window._session("Terraria").pending)
        self.assertIsNone(window._session("Minecraft").pending)

    def test_window_never_gives_commands(self):
        result = window.answer("дай алмаз", "Minecraft", Pipeline(Extractor("give_item", "алмаз")),
                               responder())
        self.assertEqual(result["outcome"], "reject")
        self.assertNotIn("command", result)

    def test_why_uses_the_window_session(self):
        pipeline = Pipeline(Extractor("give_item", "алмаз"))
        window.answer("дай алмаз", "Minecraft", pipeline, responder())
        result = window.answer("почему?", "Minecraft", pipeline, responder())
        self.assertEqual(result["source"], "rule")
        self.assertIn("режим survival", result["response"])

    def test_page_address_carries_the_game(self):
        self.assertEqual(page("http://127.0.0.1:8000/"), "http://127.0.0.1:8000/mini")
        self.assertTrue(page("http://127.0.0.1:8000", "Elden Ring").endswith("/mini?game=Elden%20Ring"))


if __name__ == "__main__":
    unittest.main()


class WindowScreenTests(unittest.TestCase):
    def setUp(self):
        window._sessions.clear()
        self.addCleanup(window._sessions.clear)

    def test_screen_facts_reach_the_extractor_and_the_explanation(self):
        extractor = Extractor("plan_goal", "золотая кирка")
        seen = "игра: Terraria; под курсором или выделено: Золотая кирка"
        result = window.answer("как это сделать", "Terraria", Pipeline(extractor), responder(),
                               screen=seen)
        self.assertEqual(extractor.calls[0]["screen"], seen)
        self.assertEqual(result["explanation"][0], "по снимку экрана: " + seen)
        self.assertEqual(result["screen"], seen)

    def test_without_screen_the_call_is_as_before(self):
        extractor = Extractor("plan_goal", "золотая кирка")
        result = window.answer("как сделать золотую кирку", "Terraria", Pipeline(extractor),
                               responder())
        self.assertNotIn("screen", extractor.calls[0])
        self.assertIsNone(result["screen"])


class WindowVoiceTests(unittest.TestCase):
    def test_vocabulary_of_the_named_game_is_used_for_repair(self):
        from api import speech
        from resolver.speech import repair
        terraria = speech._vocabulary("Terraria")
        minecraft = speech._vocabulary(None)
        self.assertIsNot(terraria, minecraft)
        self.assertEqual(repair("сделайкошмарнуюкирку", terraria), "сделай кошмарную кирку")
        # У игры без базы знаний словаря нет своего: берётся общий, правка не падает.
        self.assertIsNotNone(speech._vocabulary("Elden Ring"))


class OtherGameQuestionTests(unittest.TestCase):
    """Игра без базы знаний: вопрос, названный моделью планом, уходит в поиск, а не в отказ."""

    def test_plan_or_world_question_is_searched(self):
        from search.engines import Hit
        from search.service import SearchAnswer

        class Search:
            def __init__(self):
                self.queries = []

            def search(self, query, game=None):
                self.queries.append((query, game))
                return SearchAnswer(query=query, engine="stub", hits=[Hit(
                    title="Iron ore", url="https://wiki.factorio.com/Iron_ore", content="Mined by drills.",
                    score=0.9, engine="stub")])

        for action in ("plan_goal", "ask_about", "ask_sources"):
            search = Search()
            turn = Pipeline(Extractor(action, "iron ore"), search=search).handle(
                "как это добыть?", game="Factorio")
            self.assertEqual((turn.outcome, turn.action), ("act", "search_web"), action)
            self.assertEqual(turn.stages["rule"], "question_to_search")
            self.assertEqual(search.queries[0][1], "Factorio")

    def test_giving_in_another_game_is_still_refused(self):
        turn = Pipeline(Extractor("give_item", "iron ore")).handle("дай руду", game="Factorio")
        self.assertEqual(turn.outcome, "reject")
