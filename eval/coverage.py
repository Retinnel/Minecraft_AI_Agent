"""Замер покрытия и скорости планировщика по всей предметной области.

Строит план для каждого предмета и блока из базы знаний и считает, для какой доли целей
вывод доходит до конца, а также сколько это занимает времени.

Компонентный тест решателя в смысле раздела 16 руководства: проверяется не отдельный
сценарий, а поведение на полном наборе целей.

Запуск:
    .venv/Scripts/python.exe -m eval.coverage --report
"""

from __future__ import annotations

import argparse
import collections
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from kb.graph import KnowledgeBase
from solver.planner import MISSING, Planner

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "eval" / "reports"


SLOW_MS = 100.0
REPEATS = 2

# Почему для цели нет плана. Класс определяется по тому, на чём вывод упёрся, и по виду
# самой цели; порядок проверок — от частного к общему.
DEPENDS = "зависит от предмета без способа получения"
CYCLE = "циклическая зависимость"
TECHNICAL = "техническая форма блока, а не предмет"
CREATIVE = "выдаётся только в творческом режиме"
BUCKET = "набирается ведром или бутылкой"
STRUCTURE = "находка в постройках и раскопках"
CODE = "способ получения задан кодом игры (земледелие, торговля, зелья, обмен)"

TECHNICAL_PARTS = ("wall_", "attached_", "potted_", "_candle_cake", "_cauldron", "_portal",
                   "piston_head", "moving_piston", "_plant", "_wire", "tripwire", "bubble_column",
                   "frosted_ice", "_stem", "_bush", "cave_vines", "tall_seagrass", "bamboo_sapling")
TECHNICAL_NAMES = ("air", "cave_air", "void_air", "fire", "soul_fire", "water", "lava", "beetroots",
                   "carrots", "potatoes", "cocoa", "powder_snow", "end_gateway", "pitcher_crop",
                   "torchflower_crop", "sweet_berry_bush", "candle_cake")
CREATIVE_PARTS = ("_spawn_egg", "command_block", "structure_", "infested_")
CREATIVE_NAMES = ("barrier", "bedrock", "jigsaw", "debug_stick", "light", "spawner",
                  "end_portal_frame", "knowledge_book", "reinforced_deepslate", "budding_amethyst",
                  "petrified_oak_slab", "player_head", "farmland", "dirt_path", "chorus_plant",
                  "bundle", "frogspawn")
STRUCTURE_PARTS = ("pottery_sher", "pottery_shard", "smithing_template", "music_disc", "disc_fragment",
                   "banner_pattern", "_horse_armor")
STRUCTURE_NAMES = ("enchanted_golden_apple", "heart_of_the_sea", "nautilus_shell", "echo_shard",
                   "elytra", "saddle", "name_tag", "trident", "totem_of_undying", "dragon_egg",
                   "sniffer_egg", "nether_star", "sculk_catalyst", "enchanted_book", "map",
                   "filled_map")


def classify(target: str, blocked_on: List[str], reason: str) -> str:
    """Класс причины, по которой для цели нет плана."""
    name = target.split(":", 1)[-1]
    if reason == CYCLE:
        return CYCLE
    if blocked_on and target not in blocked_on:
        return DEPENDS
    if name in TECHNICAL_NAMES or any(part in name for part in TECHNICAL_PARTS):
        return TECHNICAL
    if name in CREATIVE_NAMES or any(part in name for part in CREATIVE_PARTS):
        return CREATIVE
    if name.endswith("_bucket") or name.endswith("_bottle"):
        return BUCKET
    if name in STRUCTURE_NAMES or any(part in name for part in STRUCTURE_PARTS):
        return STRUCTURE
    return CODE


def measure(kb: KnowledgeBase, planner: Planner) -> Dict[str, Any]:
    targets = sorted(
        thing for thing in {kb.game_id(t) for t in kb._by_uri.values()} if thing
        and ("item" in kb.kinds(thing) or "block" in kb.kinds(thing))
    )

    feasible = 0
    reasons: collections.Counter = collections.Counter()
    unreachable: List[str] = []
    classes: Dict[str, List[str]] = collections.defaultdict(list)
    bottlenecks: collections.Counter = collections.Counter()
    times: List[float] = []

    started = time.perf_counter()
    for target in targets:
        at = time.perf_counter()
        plan = planner.plan(target, 1, {})
        elapsed = (time.perf_counter() - at) * 1000
        # Долгие цели перемеряются, и берётся наименьшее время: разовый замер на занятой
        # машине показывает планировщик операционной системы, а не решателя.
        if elapsed > SLOW_MS:
            for _ in range(REPEATS):
                at = time.perf_counter()
                planner.plan(target, 1, {})
                elapsed = min(elapsed, (time.perf_counter() - at) * 1000)
        times.append(elapsed)
        if plan.feasible:
            feasible += 1
            continue
        unreachable.append(target)
        missing = [step for step in plan.root.walk() if step.kind == MISSING]
        reason = (missing[0].reason if missing else None) or "?"
        reasons[reason] += 1
        blocked_on = sorted({step.item for step in missing})
        found = classify(target, blocked_on, reason)
        classes[found].append(target)
        if found == DEPENDS:
            bottlenecks.update(blocked_on)
    total = time.perf_counter() - started

    times.sort()
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "targets": len(targets),
        "feasible": feasible,
        "coverage": round(feasible / len(targets), 4) if targets else 0.0,
        "seconds_total": round(total, 1),
        "ms_median": round(times[len(times) // 2], 2) if times else 0.0,
        "ms_p95": round(times[int(len(times) * 0.95)], 1) if times else 0.0,
        "ms_max": round(times[-1], 1) if times else 0.0,
        "reasons": dict(reasons.most_common()),
        "classes": {name: len(found) for name, found in
                    sorted(classes.items(), key=lambda pair: -len(pair[1]))},
        "class_samples": {name: found[:8] for name, found in classes.items()},
        "bottlenecks": dict(bottlenecks.most_common(10)),
        "unreachable_sample": unreachable[:40],
    }


def format_report(stats: Dict[str, Any]) -> str:
    lines = [
        "# Покрытие и скорость планировщика",
        "",
        f"Дата прогона: {stats['timestamp']}",
        "",
        "| Показатель | Значение |",
        "|---|---|",
        f"| Целей проверено | {stats['targets']} |",
        f"| План построен | {stats['feasible']} ({stats['coverage']:.1%}) |",
        f"| Время всего прогона | {stats['seconds_total']} с |",
        f"| Время на цель, медиана | {stats['ms_median']} мс |",
        f"| Время на цель, 95-й перцентиль | {stats['ms_p95']} мс |",
        f"| Время на цель, максимум | {stats['ms_max']} мс |",
        "",
        "## Почему план не строится",
        "",
        "| Причина | Целей |",
        "|---|---|",
    ]
    for reason, count in stats["reasons"].items():
        lines.append(f"| {reason} | {count} |")
    lines += ["", "## Недостижимые цели по классам", "", "| Класс | Целей | Примеры |", "|---|---|---|"]
    for name, count in stats.get("classes", {}).items():
        samples = ", ".join(item.split(":", 1)[-1] for item in stats["class_samples"][name][:5])
        lines.append(f"| {name} | {count} | {samples} |")
    if stats.get("bottlenecks"):
        lines += ["", "Предметы, на которые упираются чужие планы: " + ", ".join(
            f"{item.split(':', 1)[-1]} — {count}" for item, count in stats["bottlenecks"].items()) + "."]
    lines += [
        "",
        "Технические формы блоков и предметы творческого режима целями игрока не бывают.",
        "Остальное — способы получения, которые игра задаёт кодом, а не файлами данных:",
        "их закрывает рукописный слой `kb/gathering.json`, запись за записью.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", action="store_true", help="сохранить отчёт в eval/reports/")
    args = parser.parse_args()

    kb = KnowledgeBase.load()
    stats = measure(kb, Planner(kb))
    report = format_report(stats)
    print(report)

    if args.report:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d")
        (REPORTS_DIR / f"{stamp}-planner-coverage.md").write_text(report, encoding="utf-8")
        (REPORTS_DIR / f"{stamp}-planner-coverage.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Отчёт сохранён в {REPORTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
