"""Замер: насколько короче планы, когда планировщик знает запасы хранилищ.

Для набора целей план строится дважды из пустого инвентаря: без памяти и с памятью мира из
eval/memory_fixture.py (сундук с типичными запасами середины игры и увиденная руда).
Сравниваются число действий и трудоёмкость (вес шагов планировщика). Модель не участвует,
результат воспроизводим.

Запуск: python -m eval.plan_memory [--report]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.memory_fixture import HOME, fixture_store  # noqa: E402
from kb.graph import KnowledgeBase  # noqa: E402
from solver.locator import Locator  # noqa: E402
from solver.planner import Planner  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPORTS_DIR = ROOT / "reports"
KB_PATH = ROOT.parent / "data" / "kb" / "game.ttl"

# Цели, о которых чаще всего спрашивают в начале и середине игры.
GOALS = ["minecraft:iron_pickaxe", "minecraft:iron_sword", "minecraft:shield", "minecraft:bucket",
         "minecraft:furnace", "minecraft:torch", "minecraft:bow", "minecraft:iron_chestplate",
         "minecraft:diamond_pickaxe", "minecraft:crafting_table", "minecraft:chest",
         "minecraft:compass"]


def measure(kb: KnowledgeBase, goals: List[str] = GOALS) -> List[Dict[str, Any]]:
    planner, locator = Planner(kb), Locator(kb)
    store = fixture_store()
    storage, where = locator.storage(store, HOME)
    known = locator.known_sources(store, HOME)
    rows = []
    for goal in goals:
        if kb.label(goal) is None:
            continue
        plain = planner.plan(goal, 1, {})
        stocked = planner.plan(goal, 1, {}, storage=storage, where=where, known=known)
        taken = [step for step in stocked.taken() if not step.children]
        rows.append({"goal": goal, "label": kb.label(goal),
                     "feasible": [plain.feasible, stocked.feasible],
                     "actions": [len(plain.actions()), len(stocked.actions())],
                     "effort": [plain.effort, stocked.effort],
                     "taken": {step.item: step.count for step in taken},
                     "seen_sources": sorted({step.source for step in stocked.actions()
                                             if step.where and step.source})})
    return rows


def summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    actions = [(row["actions"][0], row["actions"][1]) for row in rows]
    effort = [(row["effort"][0], row["effort"][1]) for row in rows]

    def share(pairs):
        return [1 - after / before for before, after in pairs if before]

    return {"goals": len(rows),
            "shorter": sum(1 for before, after in actions if after < before),
            "longer": sum(1 for before, after in actions if after > before),
            "actions_before": sum(b for b, _ in actions), "actions_after": sum(a for _, a in actions),
            "effort_before": sum(b for b, _ in effort), "effort_after": sum(a for _, a in effort),
            "median_actions_cut": round(statistics.median(share(actions)) * 100, 1) if actions else 0.0,
            "median_effort_cut": round(statistics.median(share(effort)) * 100, 1) if effort else 0.0}


def format_report(rows: List[Dict[str, Any]], total: Dict[str, Any], kb: KnowledgeBase) -> str:
    lines = ["# Длина планов с памятью мира и без", "",
             "План из пустого инвентаря; с памятью — запасы сундука и увиденные руды из "
             "`eval/memory_fixture.py`. Действие — шаг, который игрок выполняет (добыть, "
             "скрафтить, выплавить); взять из сундука действием не считается, но весит 1 "
             "в трудоёмкости.", "",
             f"Целей: {total['goals']}; план короче: {total['shorter']}; длиннее: {total['longer']}.",
             f"Действий всего: {total['actions_before']} → {total['actions_after']}; "
             f"трудоёмкость: {total['effort_before']} → {total['effort_after']}.",
             f"Медиана сокращения: действий {total['median_actions_cut']}%, "
             f"трудоёмкости {total['median_effort_cut']}%.", "",
             "| Цель | Действий без → с | Трудоёмкость без → с | Взять из сундука |",
             "|---|---|---|---|"]
    for row in rows:
        taken = ", ".join(f"{kb.label(item) or item} ×{count}" for item, count in row["taken"].items())
        lines.append(f"| {row['label']} | {row['actions'][0]} → {row['actions'][1]} | "
                     f"{row['effort'][0]} → {row['effort'][1]} | {taken or '—'} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Длина планов с памятью мира и без.")
    parser.add_argument("--report", action="store_true", help="сохранить отчёт в eval/reports/")
    args = parser.parse_args()
    if not KB_PATH.exists():
        print("База знаний не собрана: запусти kb.build")
        return 1
    kb = KnowledgeBase.load(KB_PATH)
    rows = measure(kb)
    total = summary(rows)
    report = format_report(rows, total, kb)
    print(report)
    if args.report:
        REPORTS_DIR.mkdir(exist_ok=True)
        name = f"{datetime.now().strftime('%Y-%m-%d')}-plan-memory"
        (REPORTS_DIR / f"{name}.md").write_text(report, encoding="utf-8")
        (REPORTS_DIR / f"{name}.json").write_text(
            json.dumps({"summary": total, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"Отчёт сохранён в {REPORTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
