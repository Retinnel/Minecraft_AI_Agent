"""Режим «Сделай сам» на сервере: допуск валидатором, вопрос «Начать?», запуск и его цикл.

Мод подменяется упрощённой игрой (`tests/game_sim.py`): она исполняет задачи и присылает
отчёты с инвентарём, как это делает AutopilotController. Модель извлечения — заглушка.
Состояние запуска пишется во временную папку, рабочий мир не трогается.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from api import advice, autopilot  # noqa: E402
from api.autopilot import (DEFAULTS, DONE, FAILED, PAUSED, PENDING, RUNNING, STOPPED,  # noqa: E402
                           Autopilot)
from api.player_state import reset_player_state  # noqa: E402
from game_sim import SimGame  # noqa: E402
from kb.graph import KnowledgeBase  # noqa: E402
from models.autopilot import TaskReport  # noqa: E402
from models.groq_response import ChatMessage  # noqa: E402
from orchestrator import Pipeline, Proposal  # noqa: E402
from orchestrator.extractor import LanguageModelIntent  # noqa: E402
from orchestrator.llm import ModelUnavailable  # noqa: E402
from orchestrator.responder import Responder  # noqa: E402
from orchestrator.session import Session  # noqa: E402
from solver.executor import ExecutionPlanner  # noqa: E402
from solver.validator import ACT, CREATIVE, EXECUTE_PLAN, MODE_ACTIONS, REJECT, SURVIVAL  # noqa: E402

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"
STONE_PICKAXE = "minecraft:stone_pickaxe"
ON = {"enabled": True, "singleplayer": True, "available": True, "baritone": True, "max_effort": 30}


def config(**changes):
    result = json.loads(json.dumps(DEFAULTS))
    result.update({"enabled": True, **changes})
    return result


class Stub:
    def __init__(self, proposal):
        self.proposal = proposal

    def extract(self, utterance, **context):
        return self.proposal


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Isolated(unittest.TestCase):
    """Запуск во временной папке и с включённым режимом."""

    settings = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp.name) / "autopilot.json"
        self.patches = [
            patch.object(Autopilot, "_path", lambda _self: Path(self.temp.name) / _self._state_file),
            patch.object(autopilot, "settings", lambda path=None: self.settings or config()),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()


# --- валидатор и конвейер ---

@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class ExecutionVerdictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def handle(self, target, *, mode=SURVIVAL, flags=ON, inventory=None, text="сделай сам"):
        pipeline = Pipeline(Stub(Proposal(EXECUTE_PLAN, target)), self.kb)
        return pipeline.handle(text, mode=mode, inventory=inventory or {}, autopilot=flags)

    def test_only_survival_offers_execute_plan(self):
        self.assertIn(EXECUTE_PLAN, MODE_ACTIONS[SURVIVAL])
        self.assertNotIn(EXECUTE_PLAN, MODE_ACTIONS[CREATIVE])

    def test_model_sees_execute_plan_only_when_enabled(self):
        pipeline = Pipeline(Stub(None), self.kb)
        self.assertNotIn(EXECUTE_PLAN, pipeline.actions_for(SURVIVAL))
        self.assertIn(EXECUTE_PLAN, pipeline.actions_for(SURVIVAL, autopilot=True))
        self.assertNotIn(EXECUTE_PLAN, pipeline.actions_for(CREATIVE, autopilot=True))

    def test_stone_pickaxe_is_offered_with_confirmation(self):
        turn = self.handle("каменная кирка")
        self.assertEqual(turn.outcome, ACT)
        self.assertEqual(turn.target, STONE_PICKAXE)
        self.assertIn("Начать?", turn.autopilot_summary)
        self.assertIn("клавиша K", turn.autopilot_summary)
        self.assertEqual(turn.autopilot["status"], PENDING)
        self.assertIsNone(turn.command)

    def test_disabled_mode_is_refused(self):
        turn = self.handle("каменная кирка", flags={**ON, "enabled": False})
        self.assertEqual(turn.outcome, REJECT)
        self.assertIn("выключен", turn.reason)

    def test_multiplayer_and_lan_are_refused(self):
        turn = self.handle("факел", flags={**ON, "singleplayer": False})
        self.assertEqual(turn.outcome, REJECT)
        self.assertIn("только в одиночной игре", turn.reason)

    def test_without_the_mod_nothing_runs(self):
        turn = self.handle("факел", flags={**ON, "singleplayer": None, "available": False})
        self.assertEqual(turn.outcome, REJECT)
        self.assertIn("с модом", turn.reason)

    def test_creative_suggests_giving(self):
        turn = self.handle("каменная кирка", mode=CREATIVE, text="сделай сам каменную кирку")
        self.assertEqual(turn.outcome, REJECT)
        self.assertIn("дай", turn.reason)

    def test_diamond_pickaxe_is_refused_with_reason(self):
        turn = self.handle("алмазная кирка")
        self.assertEqual(turn.outcome, REJECT)
        self.assertIn("Не возьмусь", turn.reason)
        self.assertIn("Могу построить план", turn.reason)

    def test_nether_goal_is_refused(self):
        turn = self.handle("незеритовый слиток")
        self.assertEqual(turn.outcome, REJECT)
        self.assertIn("Незер", turn.reason)

    def test_effort_limit_comes_from_settings(self):
        turn = self.handle("каменная кирка", flags={**ON, "max_effort": 3})
        self.assertEqual(turn.outcome, REJECT)
        self.assertIn("трудоёмкость", turn.reason)

    def test_goal_already_in_inventory(self):
        turn = self.handle("каменная кирка", inventory={STONE_PICKAXE: 1})
        self.assertEqual(turn.outcome, REJECT)
        self.assertIn("уже есть", turn.reason)

    def test_without_baritone_the_player_is_told(self):
        turn = self.handle("каменная кирка", flags={**ON, "baritone": False})
        self.assertEqual(turn.outcome, ACT)
        self.assertIn("Baritone не установлен", turn.autopilot_summary)

    def test_explicit_words_beat_the_model_guess(self):
        pipeline = Pipeline(Stub(Proposal("plan_goal", "каменная кирка")), self.kb)
        turn = pipeline.handle("сделай сам каменную кирку", mode=SURVIVAL, inventory={},
                               autopilot=ON)
        self.assertEqual(turn.action, EXECUTE_PLAN)
        pipeline = Pipeline(Stub(Proposal(EXECUTE_PLAN, "каменная кирка")), self.kb)
        turn = pipeline.handle("как сделать каменную кирку", mode=SURVIVAL, inventory={},
                               autopilot=ON)
        self.assertEqual(turn.action, "plan_goal")
        self.assertIsNone(turn.autopilot_summary)

    def test_disabled_mode_keeps_plan_goal(self):
        pipeline = Pipeline(Stub(Proposal("plan_goal", "каменная кирка")), self.kb)
        turn = pipeline.handle("сделай сам каменную кирку", mode=SURVIVAL, inventory={})
        self.assertEqual(turn.action, "plan_goal")

    def test_responder_uses_the_template(self):
        turn = self.handle("каменная кирка")
        reply = Responder(ask_text=lambda messages: "модель не должна вызываться").reply(turn, "сделай сам")
        self.assertEqual(reply.text, turn.autopilot_summary)


# --- запуск ---

@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class RunCycleTests(Isolated):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def setUp(self):
        super().setUp()
        self.settings = None
        self.clock = Clock()
        self.service = self.make()

    def make(self):
        service = Autopilot(clock=self.clock)
        service.use_executor(ExecutionPlanner(self.kb))
        return service

    def report(self, game, task, status="done", reason=None):
        return self.service.report(TaskReport(task_id=task["task_id"], status=status, reason=reason,
                                              inventory=dict(game.inventory),
                                              stations=list(game.stations)))

    def drive(self, game, limit=40, extra=None):
        reply = self.service.next(dict(game.inventory))
        for index in range(limit):
            if reply.get("status") != RUNNING:
                return reply
            task = reply["task"]
            game.apply(task)
            if extra and index == extra[0]:
                for item, count in extra[1].items():
                    game.inventory[item] = game.inventory.get(item, 0) + count
            reply = self.report(game, task)
        return reply

    def test_needs_confirmation(self):
        run = self.service.start(STONE_PICKAXE, 1, baritone=True)
        self.assertEqual(run.status, PENDING)
        self.assertEqual(self.service.next({})["status"], PENDING)
        self.assertEqual(self.service.confirm(run.run_id).status, RUNNING)

    def test_full_run_to_the_stone_pickaxe(self):
        run = self.service.start(STONE_PICKAXE, 1, label="каменная кирка", baritone=True)
        self.service.confirm(run.run_id)
        game = SimGame(self.kb)
        reply = self.drive(game)
        self.assertEqual(reply["status"], DONE, reply)
        self.assertIn("Готово", reply["message"])
        state = self.service.state()
        self.assertEqual(state.steps_done, len(game.done))
        self.assertTrue(any(line.startswith("Шаг 1 из") for line in state.log))
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], DONE)

    def test_extra_planks_shorten_the_run(self):
        full = SimGame(self.kb)
        run = self.service.start(STONE_PICKAXE, 1, baritone=True)
        self.service.confirm(run.run_id)
        self.drive(full)
        self.service = self.make()
        self.state_path.unlink()
        run = self.service.start(STONE_PICKAXE, 1, baritone=True)
        self.service.confirm(run.run_id)
        helped = SimGame(self.kb)
        reply = self.drive(helped, extra=(0, {"minecraft:oak_planks": 16}))
        self.assertEqual(reply["status"], DONE)
        self.assertLess(len(helped.done), len(full.done))

    def test_repeated_poll_returns_the_same_task(self):
        run = self.service.start(STONE_PICKAXE, 1, baritone=True)
        self.service.confirm(run.run_id)
        first = self.service.next({})
        self.assertEqual(self.service.next({})["task"]["task_id"], first["task"]["task_id"])

    def test_failed_mining_retries_then_excludes_the_source(self):
        run = self.service.start(STONE_PICKAXE, 1, baritone=True)
        self.service.confirm(run.run_id)
        game = SimGame(self.kb, {"minecraft:stick": 2, "minecraft:wooden_pickaxe": 1})
        game.stations.append("crafting_table")
        task = self.service.next(dict(game.inventory), stations=list(game.stations))["task"]
        self.assertEqual(task["kind"], "mine")
        blocks = list(task["blocks"])
        for _ in range(autopilot.MAX_RETRIES):
            reply = self.report(game, task, "failed", "not_found")
            self.assertEqual(reply["status"], RUNNING)
            task = reply["task"]
        reply = self.report(game, task, "failed", "not_found")
        state = self.service.state()
        self.assertTrue(set(blocks) <= set(state.excluded_sources))
        # Другого источника булыжника нет — запуск заканчивается с объяснением.
        self.assertEqual(reply["status"], FAILED)
        self.assertIn("Дальше не могу", reply["message"])

    def test_player_input_pauses_and_resume_continues(self):
        run = self.service.start(STONE_PICKAXE, 1, baritone=True)
        self.service.confirm(run.run_id)
        game = SimGame(self.kb)
        task = self.service.next({})["task"]
        reply = self.report(game, task, "interrupted", "player_input")
        self.assertEqual(reply["status"], PAUSED)
        self.assertIn("ты взял управление", self.service.state().message)
        self.assertEqual(self.service.next({})["status"], PAUSED)
        self.assertEqual(self.service.resume().status, RUNNING)
        self.assertEqual(self.drive(game)["status"], DONE)

    def test_stop_and_cancel(self):
        run = self.service.start(STONE_PICKAXE, 1, baritone=True)
        self.assertEqual(self.service.stop(run_id=run.run_id).status, STOPPED)
        self.assertEqual(self.service.state().message, "Хорошо, не начинаю.")
        self.assertIsNone(self.service.confirm(run.run_id))

    def test_run_time_limit(self):
        run = self.service.start(STONE_PICKAXE, 1, baritone=True)
        self.service.confirm(run.run_id)
        self.clock.now += 11 * 60
        reply = self.service.next({})
        self.assertEqual(reply["status"], FAILED)
        self.assertIn("Время вышло", reply["message"])

    def test_server_restart_marks_the_run_stopped(self):
        run = self.service.start(STONE_PICKAXE, 1, baritone=True)
        self.service.confirm(run.run_id)
        self.service.next({})
        restarted = self.make()
        state = restarted.state()
        self.assertEqual(state.status, STOPPED)
        self.assertIsNone(state.current_task)
        self.assertEqual(restarted.next({})["status"], STOPPED)
        self.assertEqual(restarted.resume().status, RUNNING)

    def test_without_baritone_it_waits_for_the_player(self):
        run = self.service.start(STONE_PICKAXE, 1, baritone=False)
        self.service.confirm(run.run_id)
        reply = self.service.next({})
        self.assertEqual(reply["status"], "waiting")
        self.assertIn("Принеси", reply["message"])
        # Игрок принёс брёвна — дальше крафт идёт сам.
        reply = self.service.next({"minecraft:oak_log": 4})
        self.assertEqual(reply["status"], RUNNING)
        self.assertEqual(reply["task"]["kind"], "craft")

    def test_announcements_are_templates_for_the_poll(self):
        run = self.service.start(STONE_PICKAXE, 1, baritone=True)
        self.service.confirm(run.run_id)
        self.assertIn("Начинаю", self.service.consume_announcement()["response"])
        self.service.next({})
        self.assertIn("Шаг 1", self.service.consume_announcement()["response"])
        self.assertIsNone(self.service.consume_announcement())

    def test_without_confirmation_setting_starts_at_once(self):
        self.settings = config(require_confirmation=False)
        run = self.service.start(STONE_PICKAXE, 1, baritone=True)
        self.assertEqual(run.status, RUNNING)


# --- HTTP и чат ---

@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class AutopilotApiTests(Isolated):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def setUp(self):
        super().setUp()
        self.settings = None
        reset_player_state()
        advice.logs_db.clear()
        advice._response_history.clear()
        advice._conversation_history.clear()
        self.session = Session()
        self.session.set_mode(SURVIVAL)
        self.service = Autopilot()
        self.service.use_executor(ExecutionPlanner(self.kb))
        self.pipeline = Pipeline(Stub(Proposal(EXECUTE_PLAN, "каменная кирка")), self.kb)
        self.more = [
            patch.object(autopilot, "_shared", self.service),
            patch.object(autopilot, "_memory", lambda: None),
            patch.object(autopilot, "_position", lambda: None),
            patch.object(advice, "_assistant_pipeline", return_value=self.pipeline),
            patch.object(advice, "_assistant_session", return_value=self.session),
            patch.object(advice, "_assistant_responder",
                         return_value=Responder(ask_text=self.no_model)),
            patch.object(advice, "_world_memory", lambda game: None),
        ]
        for item in self.more:
            item.start()
        from main import app
        self.client = TestClient(app)

    def tearDown(self):
        for item in reversed(self.more):
            item.stop()
        super().tearDown()

    @staticmethod
    def no_model(messages):
        raise ModelUnavailable("в тестах модель не формулирует")

    def chat(self, text, **fields):
        import asyncio
        return asyncio.run(advice.player_chat_message(ChatMessage(text=text, **fields)))

    def test_http_cycle_start_confirm_next_report_done(self):
        started = self.client.post("/api/autopilot/start", json={
            "goal": "каменная кирка", "singleplayer": True, "autopilot_available": True,
            "baritone": True}).json()
        self.assertTrue(started["ok"], started)
        self.assertIn("Начать?", started["response"])
        run_id = started["autopilot"]["run_id"]
        self.assertEqual(self.client.post("/api/autopilot/confirm",
                                          json={"run_id": run_id}).json()["run"]["status"], RUNNING)
        game = SimGame(self.kb)
        reply = self.client.get("/api/autopilot/next", params={"run_id": run_id}).json()
        for _ in range(40):
            if reply.get("status") != RUNNING:
                break
            game.apply(reply["task"])
            reply = self.client.post("/api/autopilot/report", json={
                "run_id": run_id, "task_id": reply["task"]["task_id"], "status": "done",
                "inventory": game.inventory, "stations": game.stations}).json()
        self.assertEqual(reply["status"], DONE)
        state = self.client.get("/api/autopilot/state").json()
        self.assertEqual(state["run"]["status"], DONE)

    def test_http_next_takes_nearby_stations(self):
        run = self.service.start(STONE_PICKAXE, 1, baritone=True)
        self.service.confirm(run.run_id)
        inventory = {"minecraft:stick": 2, "minecraft:wooden_pickaxe": 1, "minecraft:cobblestone": 3}
        with patch.object(advice, "_inventory_counts", lambda: dict(inventory)):
            reply = self.client.get("/api/autopilot/next",
                                    params={"run_id": run.run_id, "stations": "crafting_table"}).json()
        self.assertEqual(reply["task"]["kind"], "craft")
        self.assertEqual(reply["task"]["station"], "crafting_table")

    def test_http_start_refuses_on_a_server(self):
        started = self.client.post("/api/autopilot/start", json={
            "goal": "каменная кирка", "singleplayer": False, "autopilot_available": True}).json()
        self.assertFalse(started["ok"])
        self.assertIn("одиночной", started["reason"])

    def test_bad_report_status(self):
        response = self.client.post("/api/autopilot/report", json={"status": "maybe"})
        self.assertEqual(response.status_code, 400)

    def test_chat_offer_then_yes(self):
        result = self.chat("сделай сам каменную кирку", singleplayer=True,
                           autopilot_available=True, baritone=True)
        self.assertIn("Начать?", result.response)
        self.assertEqual(result.autopilot["status"], PENDING)
        result = self.chat("да")
        self.assertIn("Начинаю", result.response)
        self.assertEqual(result.autopilot["status"], RUNNING)
        result = self.chat("стоп")
        self.assertIn("Остановился", result.response)
        self.assertEqual(self.service.state().status, STOPPED)
        result = self.chat("продолжай")
        self.assertIn("Продолжаю", result.response)

    def test_chat_offer_then_no(self):
        self.chat("сделай сам каменную кирку", singleplayer=True, autopilot_available=True)
        result = self.chat("нет")
        self.assertIn("не начинаю", result.response)
        self.assertEqual(self.service.state().status, STOPPED)

    def test_yes_without_a_run_goes_to_the_pipeline(self):
        self.assertIsNone(advice._autopilot_command("да"))

    def test_disabled_setting_is_reported_in_chat(self):
        self.settings = config(enabled=False)
        result = self.chat("сделай сам каменную кирку", singleplayer=True,
                           autopilot_available=True)
        self.assertIsNone(self.service.state())
        self.assertNotIn("Начать?", result.response)

    def test_progress_comes_through_the_poll(self):
        self.chat("сделай сам каменную кирку", singleplayer=True, autopilot_available=True,
                  baritone=True)
        self.chat("да")
        self.client.get("/api/autopilot/next")
        import asyncio
        polled = asyncio.run(advice.get_rp_response())
        self.assertEqual(polled.source, "autopilot")
        self.assertIn("Шаг 1", polled.response)


if __name__ == "__main__":
    unittest.main()


MOD_DIR = (Path(__file__).resolve().parent.parent / "mod" / "minecraft-mod" / "src" / "client"
           / "java" / "com" / "example" / "assistant")
AUTOPILOT_SOURCES = ("AutopilotController.java", "AutopilotTask.java", "CraftingActions.java",
                     "SmeltingActions.java", "SafetyMonitor.java", "BaritoneBridge.java",
                     "BaritoneCalls.java")


@unittest.skipUnless(MOD_DIR.exists(), "исходников мода нет")
class ModContractTests(unittest.TestCase):
    """Договор с модом, который проверяется без запуска игры."""

    def source(self, name):
        return (MOD_DIR / name).read_text(encoding="utf-8")

    def test_mod_accepts_exactly_the_server_task_kinds(self):
        import re
        from solver.executor import TASK_KINDS
        declared = re.search(r"KINDS = Set\.of\(([^)]*)\)", self.source("AutopilotTask.java")).group(1)
        self.assertEqual(set(re.findall(r'"([a-z_]+)"', declared)), set(TASK_KINDS))

    def test_autopilot_never_sends_commands_or_chat(self):
        for name in AUTOPILOT_SOURCES:
            text = self.source(name)
            with self.subTest(source=name):
                for banned in ("sendCommand", "sendChat", "connection.send", '"#'):
                    self.assertNotIn(banned, text)

    def test_baritone_types_live_in_one_class(self):
        for path in MOD_DIR.glob("*.java"):
            if path.name == "BaritoneCalls.java":
                continue
            with self.subTest(source=path.name):
                self.assertNotIn("import baritone", path.read_text(encoding="utf-8"))
