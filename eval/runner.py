"""Прогон золотого набора через выбранную конфигурацию и подсчёт метрик.

Запуск:
    .venv/Scripts/python.exe -m eval.runner --config symbolic
    .venv/Scripts/python.exe -m eval.runner --config symbolic --report

Метрики объясняются в eval/README.md.
"""

from __future__ import annotations

import argparse
import importlib
import json
import statistics
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from eval.schema import Case, Config, Prediction, load_golden_set

ROOT = Path(__file__).resolve().parent
GOLDEN_SET_PATH = ROOT / "golden_set.jsonl"
REPORTS_DIR = ROOT / "reports"

AVAILABLE_CONFIGS = ("symbolic",)


def load_config(key: str) -> Config:
    module = importlib.import_module(f"eval.configs.{key}")
    return module.CONFIG


def classify(case: Case, prediction: Prediction) -> str:
    """Относит один результат к одной из категорий исхода.

    Совпадение:
      pass            — поведение совпало с разметкой полностью

    Молча сделано не то (опасные расхождения):
      wrong_target    — действие выполнено не с тем предметом или существом
      wrong_count     — предмет верный, количество нет
      wrong_intent    — перепутано намерение (выдать / забрать / призвать)
      missed_clarify  — надо было переспросить, система молча выполнила действие
      spurious_act    — действие выполнено там, где его быть не должно

    Отказ вместо действия (неудобно, но безопасно):
      intent_missed   — реплика вообще не распознана как запрос действия
      over_clarify    — надо было действовать, система переспросила
      false_reject    — надо было действовать, система отказала

    Путаница отказа и уточнения:
      over_reject     — надо было переспросить, система отказала
      under_reject    — надо было отказать, система переспросила

      other           — прочее расхождение
    """
    if case.outcome == prediction.outcome == "none":
        return "pass"

    if case.outcome == "none":
        return "spurious_act" if prediction.outcome == "act" else "other"

    if case.outcome == "act":
        if prediction.outcome == "none":
            return "intent_missed"
        if prediction.outcome == "clarify":
            return "over_clarify"
        if prediction.outcome == "reject":
            return "false_reject"
        if case.intent and prediction.intent != case.intent:
            return "wrong_intent"
        expected_target = case.item_id or case.entity_id
        actual_target = prediction.item_id or prediction.entity_id
        if expected_target != actual_target:
            return "wrong_target"
        if case.count is not None and prediction.count != case.count:
            return "wrong_count"
        return "pass"

    if case.outcome == "clarify":
        if prediction.outcome == "clarify":
            return "pass"
        if prediction.outcome == "act":
            return "missed_clarify"
        if prediction.outcome == "reject":
            return "over_reject"
        if prediction.outcome == "none":
            return "intent_missed"
        return "other"

    if case.outcome == "reject":
        if prediction.outcome == "reject":
            return "pass"
        if prediction.outcome == "act":
            return "spurious_act"
        if prediction.outcome == "clarify":
            return "under_reject"
        if prediction.outcome == "none":
            return "intent_missed"
        return "other"

    return "other"


# Категории, в которых система уверенно делает не то, что нужно. Это главный показатель
# безопасности: такие случаи для игрока хуже отказа, потому что происходят молча.
UNSAFE = ("wrong_target", "wrong_count", "wrong_intent", "missed_clarify", "spurious_act")


def run(config: Config, cases: List[Case]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for case in cases:
        prediction = config.predict(case.utterance)
        verdict = classify(case, prediction)
        rows.append(
            {
                "id": case.id,
                "utterance": case.utterance,
                "group": case.group,
                "policy": case.policy,
                "expected": {
                    "outcome": case.outcome,
                    "intent": case.intent,
                    "target": case.item_id or case.entity_id,
                    "count": case.count,
                },
                "actual": {
                    "outcome": prediction.outcome,
                    "intent": prediction.intent,
                    "target": prediction.item_id or prediction.entity_id,
                    "count": prediction.count,
                    "command": prediction.command,
                },
                "verdict": verdict,
                "latency_ms": round(prediction.latency_ms, 3),
                "note": case.note,
            }
        )

    total = len(rows)
    passed = sum(1 for row in rows if row["verdict"] == "pass")
    unsafe = sum(1 for row in rows if row["verdict"] in UNSAFE)
    latencies = [row["latency_ms"] for row in rows]

    verdict_counts: Dict[str, int] = {}
    for row in rows:
        verdict_counts[row["verdict"]] = verdict_counts.get(row["verdict"], 0) + 1

    by_group: Dict[str, Dict[str, int]] = {}
    for row in rows:
        bucket = by_group.setdefault(row["group"], {"total": 0, "pass": 0})
        bucket["total"] += 1
        if row["verdict"] == "pass":
            bucket["pass"] += 1

    summary = {
        "config": config.key,
        "title": config.title,
        "description": config.description,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "unsafe": unsafe,
        "unsafe_rate": round(unsafe / total, 4) if total else 0.0,
        "verdicts": dict(sorted(verdict_counts.items(), key=lambda item: -item[1])),
        "by_group": by_group,
        "latency_ms": {
            "median": round(statistics.median(latencies), 3) if latencies else 0.0,
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
    }
    return rows, summary


def format_report(rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Прогон золотого набора — {summary['title']}")
    lines.append("")
    lines.append(summary["description"])
    lines.append("")
    lines.append(f"Дата прогона: {summary['timestamp']}")
    lines.append("")
    lines.append("## Сводка")
    lines.append("")
    lines.append("| Показатель | Значение |")
    lines.append("|---|---|")
    lines.append(f"| Всего примеров | {summary['total']} |")
    lines.append(f"| Совпало с разметкой | {summary['passed']} ({summary['accuracy']:.1%}) |")
    lines.append(f"| Небезопасных расхождений | {summary['unsafe']} ({summary['unsafe_rate']:.1%}) |")
    lines.append(f"| Время отклика, медиана | {summary['latency_ms']['median']} мс |")
    lines.append(f"| Время отклика, максимум | {summary['latency_ms']['max']} мс |")
    lines.append("")

    lines.append("## По группам")
    lines.append("")
    lines.append("| Группа | Пройдено | Всего | Доля |")
    lines.append("|---|---|---|---|")
    for group in ("typical", "boundary", "negative"):
        bucket = summary["by_group"].get(group)
        if not bucket:
            continue
        share = bucket["pass"] / bucket["total"] if bucket["total"] else 0.0
        lines.append(f"| {group} | {bucket['pass']} | {bucket['total']} | {share:.1%} |")
    lines.append("")

    lines.append("## Распределение исходов")
    lines.append("")
    lines.append("| Исход | Количество |")
    lines.append("|---|---|")
    for verdict, count in summary["verdicts"].items():
        lines.append(f"| {verdict} | {count} |")
    lines.append("")

    failures = [row for row in rows if row["verdict"] != "pass"]
    lines.append(f"## Расхождения ({len(failures)})")
    lines.append("")
    if not failures:
        lines.append("Расхождений нет.")
    else:
        lines.append("| id | Реплика | Ожидалось | Получено | Исход |")
        lines.append("|---|---|---|---|---|")
        for row in failures:
            expected = _describe(row["expected"])
            actual = _describe(row["actual"])
            marker = " ⚙" if row["policy"] else ""
            lines.append(
                f"| {row['id']}{marker} | {row['utterance']} | {expected} | {actual} | {row['verdict']} |"
            )
        lines.append("")
        lines.append("⚙ — разметка зависит от принятой политики поведения, а не от объективно "
                     "правильного ответа.")
    lines.append("")
    return "\n".join(lines)


def _describe(payload: Dict[str, Any]) -> str:
    outcome = payload.get("outcome")
    if outcome == "none":
        return "не действие"
    target = payload.get("target")
    count = payload.get("count")
    if outcome == "act":
        parts = [str(target or "?")]
        if count is not None:
            parts.append(f"×{count}")
        return " ".join(parts)
    if outcome == "clarify":
        return "уточнение"
    if outcome == "reject":
        return "отказ"
    return str(outcome)


def main() -> int:
    parser = argparse.ArgumentParser(description="Прогон золотого набора реплик игрока.")
    parser.add_argument("--config", default="symbolic", choices=AVAILABLE_CONFIGS,
                        help="какую конфигурацию системы проверять")
    parser.add_argument("--golden-set", type=Path, default=GOLDEN_SET_PATH)
    parser.add_argument("--report", action="store_true",
                        help="сохранить отчёт в eval/reports/")
    parser.add_argument("--only-failures", action="store_true",
                        help="в консоли показать только расхождения")
    args = parser.parse_args()

    cases = load_golden_set(args.golden_set)
    config = load_config(args.config)
    rows, summary = run(config, cases)

    report = format_report(rows, summary)
    print(report)

    if args.report:
        REPORTS_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d")
        (REPORTS_DIR / f"{stamp}-{config.key}.md").write_text(report, encoding="utf-8")
        (REPORTS_DIR / f"{stamp}-{config.key}.json").write_text(
            json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Отчёт сохранён в {REPORTS_DIR}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
