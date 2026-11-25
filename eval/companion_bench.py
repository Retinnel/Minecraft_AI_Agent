"""Сравнение исполнителей «Сделай сам» на настоящем сервере Minecraft 1.20.1.

Конфигурации:
    companion         помощник без памяти мира: ищет всё сам вокруг себя;
    companion_memory  помощник с памятью: знает сундук с брёвнами (его открывал игрок)
                      и руды, которые сканер видел вокруг точки появления;
    player_sim        автопилот мода в упрощённой игре (tests/game_sim.py) — настоящего
                      клиента с модом здесь нет, поэтому это симуляция: время не меряется,
                      считаются только успех и пересчёты.

Каждый прогон помощника начинается с одного и того же мира: сервер останавливается, папка
мира восстанавливается из нетронутой копии, сервер запускается заново, консолью ставятся
два сундука (с брёвнами и для сдачи готового). Помощник консоль не использует: он только
ходит, копает, крафтит и отдаёт.

    python -m eval.companion_bench --server-dir /workspace/mcserver --report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

GOALS = {
    "stone_pickaxe": ("minecraft:stone_pickaxe", 1),
    "furnace": ("minecraft:furnace", 1),
    "iron_ingot": ("minecraft:iron_ingot", 1),
}
CONFIGS = ("player_sim", "companion", "companion_memory")

# Сундуки обстановки — рядом с точкой появления мира (сид 20261125).
LOG_CHEST = (-4, 67, -44)
DELIVERY_CHEST = (-10, 67, -44)
LOG_CHEST_ITEMS = {"minecraft:oak_log": 8}


class McServer:
    """Сервер 1.20.1: восстановить мир, запустить, дать команду консоли, остановить."""

    def __init__(self, directory: Path, memory_mb: int = 1200) -> None:
        self.dir = directory
        self.memory_mb = memory_mb
        self.proc: Optional[subprocess.Popen] = None
        self.log_path = directory / "bench-server.log"
        self._lines: List[str] = []

    def reset(self) -> None:
        shutil.rmtree(self.dir / "world", ignore_errors=True)
        shutil.copytree(self.dir / "world_pristine", self.dir / "world")

    def start(self, timeout: float = 120) -> None:
        log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            ["java", "-Xms512M", f"-Xmx{self.memory_mb}M", "-jar", "server.jar", "nogui"],
            cwd=self.dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        self._lines = []

        def pump():
            for line in self.proc.stdout:
                self._lines.append(line)
                log.write(line)
                log.flush()
        threading.Thread(target=pump, daemon=True).start()
        self.wait_for("Done (", timeout)

    def wait_for(self, text: str, timeout: float = 30, since: int = 0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for line in self._lines[since:]:
                if text in line:
                    return line
            if self.proc.poll() is not None:
                raise RuntimeError("сервер Minecraft завершился")
            time.sleep(0.2)
        raise TimeoutError(f"сервер: не дождался «{text}»")

    def command(self, text: str, expect: Optional[str] = None) -> str:
        since = len(self._lines)
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()
        return self.wait_for(expect, 10, since) if expect else ""

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.command("stop")
            self.proc.wait(timeout=60)
        except Exception:
            self.proc.kill()
        self.proc = None


def chest_nbt(items: Dict[str, int]) -> str:
    slots = ",".join(f'{{Slot:{slot}b,id:"{item}",Count:{count}b}}'
                     for slot, (item, count) in enumerate(items.items()))
    return f"{{Items:[{slots}]}}"


def setup_world(server: McServer) -> None:
    """Обстановка прогона: сундук с брёвнами и сундук для сдачи готового."""
    x, y, z = LOG_CHEST
    server.command(f"setblock {x} {y} {z} minecraft:chest{chest_nbt(LOG_CHEST_ITEMS)} replace",
                   expect="Changed the block")
    x, y, z = DELIVERY_CHEST
    server.command(f"setblock {x} {y} {z} minecraft:chest replace", expect="Changed the block")
    server.command("time set day")
    server.command("gamerule doDaylightCycle false")


def chest_items(server: McServer, position) -> Dict[str, int]:
    """Что лежит в сундуке — проверка сдачи (команда консоли, только для замера)."""
    x, y, z = position
    line = server.command(f"data get block {x} {y} {z} Items", expect="block data")
    found: Dict[str, int] = {}
    import re
    for item, count in re.findall(r'id: "([a-z_:]+)"[^}]*?Count: (\d+)b', line):
        found[item] = found.get(item, 0) + int(count)
    for count, item in re.findall(r'Count: (\d+)b, id: "([a-z_:]+)"', line):
        found[item] = found.get(item, 0) + int(count)
    return found


# --- сервер ассистента в этом же процессе ---

class AssistantServer:
    def __init__(self, port: int) -> None:
        import uvicorn
        from main import app
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.1)
        raise RuntimeError("сервер ассистента не поднялся")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def http(port: int, method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
    import urllib.request
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=json.dumps(body or {}).encode() if method == "POST" else None,
                                     headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def companion_settings(delivery: str, minutes: int):
    from api import companion
    base = json.loads(json.dumps(companion.DEFAULTS))
    base.update({"enabled": True, "delivery": delivery, "max_run_minutes": minutes,
                 "max_effort": 60,
                 "delivery_chest": " ".join(map(str, DELIVERY_CHEST)) if delivery == "chest" else None})
    return lambda path=None: base


def memory_settings(enabled: bool):
    from memory.config import DEFAULTS
    values = dict(DEFAULTS, enabled=enabled)
    return lambda path=None: values


def seed_memory(store, server_scan: List[Dict[str, Any]]) -> None:
    """Память «как у игрока»: сундук он открывал, руды видел сканер мода."""
    from memory.model import Position
    x, y, z = LOG_CHEST
    store.record_container("minecraft:chest", Position("minecraft:overworld", x, y, z),
                           dict(LOG_CHEST_ITEMS), label="Сундук")
    found = [(entry["id"], Position.parse(entry)) for entry in server_scan]
    if found:
        store.observe_blocks(found, [], labels={})
    store.save()


def run_companion(goal_key: str, config: str, args, server: McServer) -> Dict[str, Any]:
    import memory
    from api import autopilot, companion, worlds

    goal, count = GOALS[goal_key]
    with_memory = config == "companion_memory"
    server.stop()
    server.reset()
    server.start()
    setup_world(server)
    result: Dict[str, Any] = {"goal": goal_key, "config": config}
    data = Path(tempfile.mkdtemp(prefix="companion-bench-"))
    with ExitStack() as stack:
        stack.enter_context(patch.object(worlds, "WORLDS_DIR", data / "worlds"))
        stack.enter_context(patch.object(companion, "settings", companion_settings(args.delivery, args.minutes)))
        stack.enter_context(patch("memory.config.settings", memory_settings(with_memory)))
        stack.enter_context(patch.object(companion, "LOG_PATH", data / "companion.log"))
        companion.shared()._run = None
        companion.shared()._loaded_for = None
        companion.link().reset()
        memory.reset()
        worlds.enter(f"bench-{goal_key}-{config}", "bench")
        http(args.port, "POST", "/api/rp/mode", {"mode": "survival"})
        if args.delivery == "drop":
            player = subprocess.Popen(["node", str(ROOT / "companion" / "scripts" / "idle_player.js")],
                                      cwd=ROOT / "companion", stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            stack.callback(player.terminate)
        launch = http(args.port, "POST", "/api/companion/launch",
                      {"server_url": f"http://127.0.0.1:{args.port}"})
        stack.callback(lambda: http(args.port, "POST", "/api/companion/shutdown"))
        if not launch.get("ok"):
            raise RuntimeError(f"помощник не запустился: {launch}")
        deadline = time.time() + 90
        while not companion.link().connected():
            if time.time() > deadline:
                raise RuntimeError("помощник не вышел на связь")
            time.sleep(0.5)
        if with_memory:
            # Сканер видел окрестности: руды в радиусе сканирования вокруг точки появления.
            time.sleep(args.scan_wait)
            store = memory.shared("Minecraft")
            seed_memory(store, [])
            result["memory_facts"] = len(store.facts())
        started = http(args.port, "POST", "/api/companion/start", {"goal": goal, "count": count})
        if not started.get("ok"):
            result.update(status="rejected", message=started.get("reason"))
            return result
        result["summary"] = started.get("response")
        begin = time.time()
        http(args.port, "POST", "/api/companion/confirm", {})
        run = None
        while time.time() - begin < args.minutes * 60 + 30:
            run = http(args.port, "GET", "/api/companion/state")["run"]
            if run and run["status"] in (autopilot.DONE, autopilot.FAILED, autopilot.STOPPED, autopilot.PAUSED):
                break
            time.sleep(1)
        elapsed = time.time() - begin
        result.update(status=run["status"] if run else "lost", message=(run or {}).get("message"),
                      seconds=round(elapsed, 1), replans=(run or {}).get("replans"),
                      steps=(run or {}).get("steps_done"), delivered=(run or {}).get("delivered"),
                      excluded=(run or {}).get("excluded_sources"),
                      bot_inventory=companion.link().snapshot.get("inventory"))
        if args.delivery == "chest":
            result["chest"] = chest_items(server, DELIVERY_CHEST)
        log = (data / "companion.log")
        if log.exists():
            text = log.read_text(encoding="utf-8")
            result["failed_tasks"] = text.count(": failed (")
            result["interrupted_tasks"] = text.count(": interrupted (")
            keep = ROOT / "eval" / "reports" / "companion-logs"
            keep.mkdir(parents=True, exist_ok=True)
            (keep / f"{goal_key}-{config}.log").write_text(text, encoding="utf-8")
        if with_memory:
            store = memory.shared("Minecraft")
            result["companion_facts"] = sum(1 for fact in store.facts() if fact.source == "companion")
    result["success"] = result.get("status") == autopilot.DONE and (
        args.delivery == "none" or bool(result.get("delivered")))
    return result


def run_player_sim(goal_key: str) -> Dict[str, Any]:
    """Автопилот мода в упрощённой игре: тот же класс запуска и тот же решатель."""
    from api import autopilot
    from api.autopilot import Autopilot
    from game_sim import SimGame
    from kb.graph import KnowledgeBase
    from models.autopilot import TaskReport
    from solver.executor import ExecutionPlanner

    goal, count = GOALS[goal_key]
    kb = KnowledgeBase.load(ROOT / "data" / "kb" / "game.ttl")
    with tempfile.TemporaryDirectory() as temp, \
            patch.object(Autopilot, "_path", lambda _self: Path(temp) / "autopilot.json"):
        settings = dict(autopilot.DEFAULTS, enabled=True, use_baritone=True, max_effort=60,
                        require_confirmation=False)
        service = Autopilot(config=lambda: settings)
        service.use_executor(ExecutionPlanner(kb, max_effort=60))
        service.start(goal, count, baritone=True)
        game = SimGame(kb)
        reply = service.next(game.inventory, stations=game.stations)
        while reply.get("status") == autopilot.RUNNING:
            task = reply["task"]
            game.apply(task)
            reply = service.report(TaskReport(task_id=task["task_id"], status="done",
                                              inventory=game.inventory, stations=game.stations))
        run = service.state()
    return {"goal": goal_key, "config": "player_sim", "status": run.status,
            "success": run.status == autopilot.DONE, "replans": run.replans,
            "steps": run.steps_done, "seconds": None, "simulated": True}


def render(results: List[Dict[str, Any]], args) -> str:
    lines = [f"# Помощник против автопилота — прогон {date.today().isoformat()}", "",
             "Сервер: vanilla 1.20.1, offline, мирный, сид 20261125; мир восстанавливается из "
             "копии перед каждым прогоном. Доставка: " + args.delivery + ".", "",
             "`player_sim` — автопилот мода в упрощённой игре (tests/game_sim.py): клиента "
             "с модом на этой машине нет, это симуляция, время не меряется.", "",
             "| Цель | Конфигурация | Итог | Время, с | Шагов | Пересчётов | Сбоев задач | Отдал |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['goal']} | {r['config']} | {'✅' if r.get('success') else '❌'} {r.get('status')} "
                     f"| {r.get('seconds') if r.get('seconds') is not None else '—'} | {r.get('steps', '—')} "
                     f"| {r.get('replans', '—')} | {r.get('failed_tasks', '—')} "
                     f"| {'да' if r.get('delivered') else ('—' if r.get('simulated') else 'нет')} |")
    lines += ["", "## Подробности", ""]
    for r in results:
        if r.get("simulated"):
            continue
        lines.append(f"- **{r['goal']} / {r['config']}**: {r.get('message') or ''}"
                     + (f" Сундук сдачи: {r['chest']}." if r.get("chest") else "")
                     + (f" Фактов памяти от помощника: {r['companion_facts']}." if "companion_facts" in r else ""))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-dir", type=Path, default=Path("/workspace/mcserver"))
    parser.add_argument("--goals", default=",".join(GOALS))
    parser.add_argument("--configs", default=",".join(CONFIGS))
    parser.add_argument("--delivery", default="chest", choices=("chest", "drop", "none"))
    parser.add_argument("--minutes", type=int, default=12)
    parser.add_argument("--scan-wait", type=float, default=8.0)
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)

    goals = [goal for goal in args.goals.split(",") if goal]
    configs = [config for config in args.configs.split(",") if config]
    results: List[Dict[str, Any]] = []
    server = McServer(args.server_dir)
    needs_server = any(config != "player_sim" for config in configs)
    try:
        with (AssistantServer(args.port) if needs_server else ExitStack()):
            for goal in goals:
                for config in configs:
                    print(f"→ {goal} / {config}", flush=True)
                    try:
                        result = run_player_sim(goal) if config == "player_sim" else \
                            run_companion(goal, config, args, server)
                    except Exception as error:
                        result = {"goal": goal, "config": config, "status": "error",
                                  "message": str(error), "success": False}
                    print("  ", json.dumps(result, ensure_ascii=False), flush=True)
                    results.append(result)
    finally:
        server.stop()
    if args.report:
        out = ROOT / "eval" / "reports" / f"{date.today().isoformat()}-companion"
        out.with_suffix(".json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        out.with_suffix(".md").write_text(render(results, args), encoding="utf-8")
        print(f"отчёт: {out}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
