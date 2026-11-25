"""Помощник — отдельный игрок, исполняющий план: сервер, чат, память, процесс.

Процесс Node здесь не запускается: игру заменяет упрощённая игра (`tests/game_sim.py`),
которая исполняет задачи и отчитывается инвентарём, как это делает companion/src/runner.js.
Настоящий прогон на сервере Minecraft — eval/companion_bench.py.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from api import advice, autopilot, companion  # noqa: E402
from api.autopilot import COMPANION, DONE, PENDING, RUNNING, STOPPED, Autopilot  # noqa: E402
from api.player_state import reset_player_state  # noqa: E402
from game_sim import SimGame  # noqa: E402
from kb.graph import KnowledgeBase  # noqa: E402
from memory.adapters.minecraft import container, observations  # noqa: E402
from memory.model import COMPANION as COMPANION_SOURCE, MOD, Position  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
from models.autopilot import TaskReport  # noqa: E402
from models.groq_response import ChatMessage  # noqa: E402
from orchestrator import Pipeline, Proposal  # noqa: E402
from orchestrator.llm import ModelUnavailable  # noqa: E402
from orchestrator.responder import Responder  # noqa: E402
from orchestrator.session import Session  # noqa: E402
from solver.executor import ExecutionPlanner  # noqa: E402
from orchestrator.pipeline import PLAN_GOAL  # noqa: E402
from solver.validator import ACT, EXECUTE_PLAN, REJECT, SURVIVAL  # noqa: E402

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "kb" / "game.ttl"
STONE_PICKAXE = "minecraft:stone_pickaxe"
IRON_INGOT = "minecraft:iron_ingot"
HERE = Position(dimension="minecraft:overworld", x=0, y=64, z=0)


def companion_config(**changes):
    result = json.loads(json.dumps(companion.DEFAULTS))
    result.update({"enabled": True, **changes})
    return result


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Stub:
    def __init__(self, proposal):
        self.proposal = proposal

    def extract(self, utterance, **context):
        return self.proposal


# --- настройки, пульс, процесс ---

class SettingsTests(unittest.TestCase):
    def read(self, section):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            path.write_text(json.dumps({"companion": section}), encoding="utf-8")
            return companion.settings(path)

    def test_defaults_are_off_and_local(self):
        config = self.read({})
        self.assertFalse(config["enabled"])
        self.assertEqual((config["host"], config["port"], config["username"]),
                         ("127.0.0.1", 25565, "Helper"))
        self.assertEqual(config["delivery"], "drop")

    def test_bad_values_fall_back_or_are_clamped(self):
        config = self.read({"port": 999999, "delivery": "teleport", "min_health": -5,
                            "allowed_kinds": ["mine", "summon"], "timeouts_s": {"mine": 1}})
        self.assertEqual(config["port"], 65535)
        self.assertEqual(config["delivery"], "drop")
        self.assertEqual(config["min_health"], 1)
        self.assertEqual(config["allowed_kinds"], ["mine"])
        self.assertEqual(config["timeouts_s"]["mine"], 5)

    def test_repo_settings_have_the_section_switched_off(self):
        config = companion.settings()
        self.assertFalse(config["enabled"])


class LinkTests(unittest.TestCase):
    def test_heartbeat_expires(self):
        clock = Clock()
        link = companion.Link(clock)
        self.assertFalse(link.connected())
        link.beat({"username": "Helper", "position": {"x": 1, "y": 64, "z": 2}})
        self.assertTrue(link.connected())
        self.assertEqual(link.position().x, 1)
        clock.now += companion.HEARTBEAT_TIMEOUT_S + 1
        self.assertFalse(link.connected())


class ProcessTests(unittest.TestCase):
    def test_launch_needs_dependencies(self):
        process = companion.Process()
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(companion, "COMPANION_DIR", Path(temp)):
            result = process.launch(companion_config(), "http://127.0.0.1:8000")
        self.assertFalse(result["ok"])
        self.assertIn("npm install", result["reason"])

    def test_launch_passes_the_world_address_and_stops(self):
        process = companion.Process()
        fake = MagicMock()
        fake.poll.return_value = None
        fake.pid = 4242
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(companion, "COMPANION_DIR", Path(temp)), \
                patch.object(companion, "LOG_PATH", Path(temp) / "companion.log"), \
                patch.object(companion.subprocess, "Popen", return_value=fake) as popen:
            (Path(temp) / "node_modules").mkdir()
            result = process.launch(companion_config(port=54321, username="Bob"), "http://x:1")
            self.assertTrue(result["ok"])
            env = popen.call_args.kwargs["env"]
            self.assertEqual((env["MC_PORT"], env["MC_USERNAME"], env["ASSISTANT_URL"]),
                             ("54321", "Bob", "http://x:1"))
            self.assertTrue(process.running())
            self.assertEqual(process.launch(companion_config(), "http://x:1")["already"], True)
            self.assertTrue(process.shutdown())
            fake.terminate.assert_called_once()


class DeliveryTests(unittest.TestCase):
    def test_modes(self):
        with patch.object(companion, "settings", lambda path=None: companion_config(delivery="drop")):
            self.assertEqual(companion.delivery(), {"mode": "drop"})
        with patch.object(companion, "settings",
                          lambda path=None: companion_config(delivery="chest", delivery_chest="10 64 -3")):
            found = companion.delivery()
        self.assertEqual(found["mode"], "chest")
        self.assertEqual((found["position"]["x"], found["position"]["z"]), (10, -3))

    def test_chest_by_memory_label_and_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MemoryStore(Path(temp) / "memory.jsonl", game="Minecraft")
            store.record_container("minecraft:chest", Position(dimension="minecraft:overworld", x=5, y=70, z=5),
                                   {"minecraft:dirt": 3}, label="Сундук у дома")
            with patch.object(companion, "settings",
                              lambda path=None: companion_config(delivery="chest", delivery_chest="у дома")):
                found = companion.delivery(store)
            self.assertEqual(found["mode"], "chest")
            self.assertEqual(found["position"]["y"], 70)
            with patch.object(companion, "settings",
                              lambda path=None: companion_config(delivery="chest", delivery_chest="на луне")):
                missing = companion.delivery(store)
        self.assertEqual(missing["mode"], "drop")
        self.assertIn("не найден", missing["note"])


# --- память: источник «companion» ---

class MemorySourceTests(unittest.TestCase):
    def test_companion_observations_and_chests_are_marked(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MemoryStore(Path(temp) / "memory.jsonl", game="Minecraft")
            observations(store, {"source": "companion", "blocks": [
                {"id": "minecraft:iron_ore", "x": 1, "y": 40, "z": 1}]})
            observations(store, {"blocks": [{"id": "minecraft:coal_ore", "x": 2, "y": 50, "z": 2}]})
            fact = container(store, {"source": "companion", "block": "minecraft:chest",
                                     "x": 3, "y": 64, "z": 3, "items": {"minecraft:oak_log": 4}})
            sources = {item.subject: item.source for item in store.facts()}
        self.assertEqual(sources["minecraft:iron_ore"], COMPANION_SOURCE)
        self.assertEqual(sources["minecraft:coal_ore"], MOD)
        self.assertEqual(fact.source, COMPANION_SOURCE)

    def test_unknown_source_is_the_mod(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MemoryStore(Path(temp) / "memory.jsonl", game="Minecraft")
            observations(store, {"source": "hacker", "blocks": [
                {"id": "minecraft:iron_ore", "x": 1, "y": 40, "z": 1}]})
            self.assertEqual(store.facts()[0].source, MOD)


# --- запуск помощника: тот же план, своя доставка ---

@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class CompanionRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = companion_config()
        self.patch = patch.object(Autopilot, "_path", lambda _self: Path(self.temp.name) / _self._state_file)
        self.patch.start()
        self.service = Autopilot(executor=COMPANION, state_file="companion.json",
                                 config=lambda: self.config)
        self.service.use_executor(ExecutionPlanner(self.kb))

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def drive(self, game, limit=60, deliver="done"):
        reply = self.service.next(dict(game.inventory), stations=list(game.stations))
        kinds = []
        for _ in range(limit):
            if reply.get("status") != RUNNING:
                return reply, kinds
            task = reply["task"]
            kinds.append(task["kind"])
            if task["kind"] == autopilot.TASK_DELIVER:
                status = deliver
            else:
                game.apply(task)
                status = "done"
            reply = self.service.report(TaskReport(task_id=task["task_id"], status=status,
                                                   reason=None if status == "done" else "not_found",
                                                   inventory=dict(game.inventory),
                                                   stations=list(game.stations)))
        return reply, kinds

    def test_mines_itself_without_baritone(self):
        run = self.service.start(STONE_PICKAXE, 1, delivery={"mode": "none"})
        self.service.confirm(run.run_id)
        reply, kinds = self.drive(SimGame(self.kb))
        self.assertEqual(reply["status"], DONE, reply)
        self.assertIn("mine", kinds)
        self.assertNotIn("deliver", kinds)
        self.assertEqual(self.service.state().executor, COMPANION)

    def test_delivers_to_the_chest_at_the_end(self):
        chest = {"mode": "chest", "position": {"x": 1, "y": 64, "z": 1}, "where": "1 64 1"}
        run = self.service.start(IRON_INGOT, 1, delivery=chest)
        self.service.confirm(run.run_id)
        reply, kinds = self.drive(SimGame(self.kb))
        self.assertEqual(reply["status"], DONE, reply)
        self.assertEqual(kinds[-1], "deliver")
        self.assertIn("smelt", kinds)
        state = self.service.state()
        self.assertTrue(state.delivered)
        self.assertIn("Положил в сундук", state.message)
        self.assertTrue(state.message.startswith("Помощник: "))

    def test_failed_handover_keeps_items_with_the_helper(self):
        run = self.service.start(STONE_PICKAXE, 1, delivery={"mode": "drop"})
        self.service.confirm(run.run_id)
        reply, kinds = self.drive(SimGame(self.kb), deliver="failed")
        self.assertEqual(reply["status"], DONE)
        self.assertGreaterEqual(kinds.count("deliver"), 3)
        self.assertIn("всё у помощника", self.service.state().message)

    def test_danger_pauses_and_resume_continues(self):
        run = self.service.start(STONE_PICKAXE, 1, delivery={"mode": "none"})
        self.service.confirm(run.run_id)
        game = SimGame(self.kb)
        self.service.consume_announcement()
        task = self.service.next({})["task"]
        self.service.consume_announcement()
        reply = self.service.report(TaskReport(task_id=task["task_id"], status="interrupted",
                                               reason="danger", inventory={}))
        self.assertEqual(reply["status"], autopilot.PAUSED)
        self.assertIn("опасно", self.service.consume_announcement()["response"])
        self.service.resume()
        reply, _ = self.drive(game)
        self.assertEqual(reply["status"], DONE)

    def test_walking_away_from_the_table_is_noticed(self):
        run = self.service.start(STONE_PICKAXE, 1, delivery={"mode": "none"})
        self.service.confirm(run.run_id)
        task = self.service.next({}, stations=["crafting_table"])["task"]
        self.assertEqual(self.service.state().stations, ["crafting_table"])
        self.service.report(TaskReport(task_id=task["task_id"], status="done", inventory={},
                                       stations=[]))
        self.assertEqual(self.service.state().stations, [])

    def test_start_announcement_names_the_helper(self):
        run = self.service.start(STONE_PICKAXE, 1, delivery={"mode": "none"})
        self.service.confirm(run.run_id)
        said = self.service.consume_announcement()
        self.assertIn("Помощник начинает", said["response"])
        self.assertIn("помощник, стоп", said["response"])


# --- чат и конвейер ---

ON = {"enabled": True, "singleplayer": True, "available": True, "baritone": False, "max_effort": 30}
HELPER = {"enabled": True, "connected": True, "name": "Helper", "delivery": "drop", "max_effort": 30,
          "inventory": {}, "position": None}


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class PipelineCompanionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def handle(self, text, *, action=PLAN_GOAL, flags=None, companion_flags=None):
        pipeline = Pipeline(Stub(Proposal(action, "каменная кирка")), self.kb)
        context = dict(flags or ON, companion=companion_flags if companion_flags is not None else HELPER)
        return pipeline.handle(text, mode=SURVIVAL, inventory={}, autopilot=context)

    def test_helper_cue_turns_a_plan_into_a_run_for_the_helper(self):
        turn = self.handle("пусть помощник сделает каменную кирку")
        self.assertEqual(turn.outcome, ACT)
        self.assertEqual(turn.autopilot["executor"], COMPANION)
        self.assertIn("Сделает помощник Helper", turn.autopilot_summary)
        self.assertIn("помощник, стоп", turn.autopilot_summary)
        self.assertIn("принесёт тебе", turn.autopilot_summary)

    def test_bot_cue_works_too(self):
        turn = self.handle("бот, добудь каменную кирку", action=EXECUTE_PLAN)
        self.assertEqual(turn.autopilot["executor"], COMPANION)

    def test_how_to_question_to_the_helper_stays_a_plan(self):
        turn = self.handle("помощник, как сделать каменную кирку?", action=EXECUTE_PLAN)
        self.assertNotEqual((turn.autopilot or {}).get("status"), PENDING)
        self.assertIsNotNone(turn.plan_tree)

    def test_helper_not_in_game_is_refused(self):
        turn = self.handle("пусть помощник сделает каменную кирку",
                           companion_flags={**HELPER, "connected": False})
        self.assertEqual(turn.outcome, REJECT)
        self.assertIn("нет в игре", turn.reason)

    def test_helper_does_not_need_singleplayer(self):
        turn = self.handle("пусть помощник сделает каменную кирку",
                           flags={**ON, "singleplayer": False})
        self.assertEqual(turn.outcome, ACT)

    def test_without_helper_cue_the_player_autopilot_runs(self):
        turn = self.handle("сделай сам каменную кирку", action=EXECUTE_PLAN)
        self.assertEqual(turn.autopilot["executor"], "player")

    def test_only_helper_enabled_takes_plain_do_it_yourself(self):
        turn = self.handle("сделай сам каменную кирку", action=EXECUTE_PLAN,
                           flags={**ON, "enabled": False})
        self.assertEqual(turn.autopilot["executor"], COMPANION)

    def test_disabled_helper_cue_is_just_a_word(self):
        turn = self.handle("пусть помощник сделает каменную кирку",
                           companion_flags={**HELPER, "enabled": False})
        self.assertNotEqual((turn.autopilot or {}).get("executor"), COMPANION)

    def test_plan_is_counted_from_the_helpers_inventory(self):
        rich = {**HELPER, "inventory": {"minecraft:stick": 2, "minecraft:cobblestone": 3,
                                        "minecraft:crafting_table": 1}}
        poor = self.handle("пусть помощник сделает каменную кирку")
        turn = self.handle("пусть помощник сделает каменную кирку", companion_flags=rich)
        self.assertLess(turn.autopilot["total"], poor.autopilot["total"])


@unittest.skipUnless(KB_PATH.exists(), "база знаний не собрана: запусти kb.build")
class CompanionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase.load(KB_PATH)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        reset_player_state()
        advice.logs_db.clear()
        advice._response_history.clear()
        advice._conversation_history.clear()
        self.session = Session()
        self.session.set_mode(SURVIVAL)
        self.store = MemoryStore(Path(self.temp.name) / "memory.jsonl", game="Minecraft")
        self.service = Autopilot(executor=COMPANION, state_file="companion.json",
                                 config=lambda: companion.settings())
        self.service.use_executor(ExecutionPlanner(self.kb))
        self.player = Autopilot(config=lambda: dict(autopilot.DEFAULTS))
        self.player.use_executor(ExecutionPlanner(self.kb))
        self.pipeline = Pipeline(Stub(Proposal(PLAN_GOAL, "каменная кирка")), self.kb)
        self.link = companion.Link()
        self.patches = [
            patch.object(Autopilot, "_path", lambda _self: Path(self.temp.name) / _self._state_file),
            patch.object(companion, "settings", lambda path=None: companion_config(delivery="none")),
            patch.object(companion, "_service", self.service),
            patch.object(companion, "_link", self.link),
            patch.object(companion, "_memory", lambda: self.store),
            patch.object(companion, "_kb", lambda: self.kb),
            patch.object(autopilot, "_shared", self.player),
            patch.object(autopilot, "_memory", lambda: None),
            patch.object(autopilot, "_position", lambda: None),
            patch.object(advice, "_assistant_pipeline", return_value=self.pipeline),
            patch.object(advice, "_assistant_session", return_value=self.session),
            patch.object(advice, "_assistant_responder", return_value=Responder(ask_text=self.no_model)),
            patch.object(advice, "_world_memory", lambda game: self.store),
        ]
        for item in self.patches:
            item.start()
        from main import app
        self.client = TestClient(app)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def no_model(messages):
        raise ModelUnavailable("в тестах модель не формулирует")

    def chat(self, text, **fields):
        import asyncio
        return asyncio.run(advice.player_chat_message(ChatMessage(text=text, **fields)))

    def hello(self, inventory=None):
        return self.client.post("/api/companion/hello", json={
            "username": "Helper", "inventory": inventory or {},
            "position": {"x": 0, "y": 64, "z": 0, "dimension": "minecraft:overworld"}}).json()

    def test_hello_marks_the_helper_present_and_sends_the_scan_list(self):
        reply = self.hello()
        self.assertTrue(reply["ok"])
        self.assertIn("minecraft:iron_ore", reply["scan"])
        self.assertTrue(self.client.get("/api/companion/state").json()["connected"])

    def test_http_cycle_through_the_helper(self):
        self.hello()
        started = self.client.post("/api/companion/start", json={"goal": "каменная кирка"}).json()
        self.assertTrue(started["ok"], started)
        self.assertIn("Сделает помощник", started["response"])
        self.client.post("/api/companion/confirm", json={})
        game = SimGame(self.kb)
        reply = self.client.post("/api/companion/next", json={"inventory": {}, "stations": []}).json()
        for _ in range(40):
            if reply.get("status") != RUNNING:
                break
            game.apply(reply["task"])
            reply = self.client.post("/api/companion/report", json={
                "task_id": reply["task"]["task_id"], "status": "done",
                "inventory": game.inventory, "stations": game.stations}).json()
        self.assertEqual(reply["status"], DONE)
        self.assertIsNone(self.player.state())

    def test_start_refuses_when_the_helper_is_away(self):
        started = self.client.post("/api/companion/start", json={"goal": "каменная кирка"}).json()
        self.assertFalse(started["ok"])
        self.assertIn("нет в игре", started["reason"])

    def test_observations_go_to_memory_as_companion(self):
        reply = self.client.post("/api/companion/observations", json={"blocks": [
            {"id": "minecraft:iron_ore", "x": 4, "y": 30, "z": 4}]}).json()
        self.assertTrue(reply["ok"])
        self.client.post("/api/companion/containers", json={
            "block": "minecraft:chest", "x": 1, "y": 64, "z": 1, "items": {"minecraft:coal": 5}})
        self.assertEqual({fact.source for fact in self.store.facts()}, {COMPANION_SOURCE})

    def test_chat_offer_yes_and_helper_stop(self):
        self.hello()
        result = self.chat("пусть помощник сделает каменную кирку")
        self.assertIn("Сделает помощник", result.response)
        self.assertEqual(result.companion["status"], PENDING)
        result = self.chat("да")
        self.assertIn("Помощник начинает", result.response)
        self.assertEqual(self.service.state().status, RUNNING)
        result = self.chat("помощник, стоп")
        self.assertEqual(self.service.state().status, STOPPED)
        self.assertIsNone(self.player.state())

    def test_plain_stop_reaches_the_helper_too(self):
        self.hello()
        self.chat("пусть помощник сделает каменную кирку")
        self.chat("да")
        self.chat("стоп")
        self.assertEqual(self.service.state().status, STOPPED)

    def test_next_with_a_stale_run_id_is_idle(self):
        self.hello()
        self.client.post("/api/companion/start", json={"goal": "каменная кирка"})
        reply = self.client.post("/api/companion/next", json={"run_id": "old"}).json()
        self.assertEqual(reply["status"], "idle")


if __name__ == "__main__":
    unittest.main()
