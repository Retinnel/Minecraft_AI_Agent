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

AVAILABLE_CONFIGS = ("symbolic", "llm_only", "hybrid")


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
      planned_instead — надо было выполнить действие, система построила план

    Путаница отказа и уточнения:
      over_reject     — надо было переспросить, система отказала
      under_reject    — надо было отказать, система переспросила

      other           — прочее расхождение
    """
    # Сбой канала связи с моделью решением системы не является и в статистику решений
    # не входит: иначе измеряется качество сети, а не качество разбора.
    if prediction.outcome == "error":
        return "error"

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
            # План вместо действия к опасным исходам не относится: plan_goal не собирает
            # команду и ничего не меняет в мире. Игрок получает не предмет, а объяснение,
            # как его добыть, — это неудобство того же рода, что отказ, а не молчаливое
            # действие не с тем предметом.
            if prediction.intent == "plan_goal":
                return "planned_instead"
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

    failed = sum(1 for row in rows if row["verdict"] == "error")
    total = len(rows) - failed
    passed = sum(1 for row in rows if row["verdict"] == "pass")
    unsafe = sum(1 for row in rows if row["verdict"] in UNSAFE)
    latencies = [row["latency_ms"] for row in rows if row["verdict"] != "error"]

    verdict_counts: Dict[str, int] = {}
    for row in rows:
        verdict_counts[row["verdict"]] = verdict_counts.get(row["verdict"], 0) + 1

    by_group: Dict[str, Dict[str, int]] = {}
    for row in rows:
        if row["verdict"] == "error":
            continue
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
        "failed": failed,
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
    if summary.get("failed"):
        lines.append(f"| Исключено из-за сбоя связи с моделью | {summary['failed']} |")
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
        if any(row["policy"] for row in failures):
            lines.append("")
            lines.append("⚙ — разметка зависит от неподтверждённой политики поведения, а не от "
                         "объективно правильного ответа.")
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


def aggregate(runs: List[Tuple[List[Dict[str, Any]], Dict[str, Any]]]) -> Dict[str, Any]:
    """Сводка по нескольким прогонам одного набора.

    Один прогон конфигурации с языковой моделью измерением не является: даже при нулевой
    температуре провайдер отвечает на один вход по-разному, и два прогона с интервалом в
    несколько минут расходились на полтора пункта. Поэтому в сводку идут медиана и
    разброс, а реплики, исход которых менялся от прогона к прогону, перечисляются отдельно.
    """
    summaries = [summary for _, summary in runs]
    first = summaries[0]

    def spread(values: List[float]) -> Dict[str, float]:
        return {"median": statistics.median(values), "min": min(values), "max": max(values)}

    passed = [summary["passed"] for summary in summaries]
    unsafe = [summary["unsafe"] for summary in summaries]
    totals = [summary["total"] for summary in summaries]

    by_group: Dict[str, Dict[str, Any]] = {}
    for group in first["by_group"]:
        values = [summary["by_group"].get(group, {}).get("pass", 0) for summary in summaries]
        by_group[group] = {"total": first["by_group"][group]["total"],
                           "pass": statistics.median(values),
                           "pass_range": [min(values), max(values)]}

    verdicts: Dict[str, float] = {}
    for verdict in {key for summary in summaries for key in summary["verdicts"]}:
        verdicts[verdict] = statistics.median(summary["verdicts"].get(verdict, 0) for summary in summaries)

    # Реплики, исход которых менялся: они и создают разброс.
    outcomes: Dict[str, List[str]] = {}
    for rows, _ in runs:
        for row in rows:
            outcomes.setdefault(row["id"], []).append(row["verdict"])
    unstable = {case_id: verdict_list for case_id, verdict_list in outcomes.items()
                if len(set(verdict_list)) > 1}

    latencies = [row["latency_ms"] for rows, _ in runs for row in rows if row["verdict"] != "error"]
    total = statistics.median(totals)
    median_passed = statistics.median(passed)
    median_unsafe = statistics.median(unsafe)
    return {
        **{key: first[key] for key in ("config", "title", "description")},
        "timestamp": summaries[-1]["timestamp"],
        "repeat": len(runs),
        "total": total,
        "failed": sum(summary.get("failed", 0) for summary in summaries),
        "passed": median_passed,
        "passed_range": [min(passed), max(passed)],
        "accuracy": round(median_passed / total, 4) if total else 0.0,
        "unsafe": median_unsafe,
        "unsafe_range": [min(unsafe), max(unsafe)],
        "unsafe_rate": round(median_unsafe / total, 4) if total else 0.0,
        "verdicts": dict(sorted(verdicts.items(), key=lambda item: -item[1])),
        "by_group": by_group,
        "latency_ms": {
            "median": round(statistics.median(latencies), 3) if latencies else 0.0,
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "unstable": unstable,
        "runs": summaries,
    }


def _n(value: float) -> str:
    """Медиана двух прогонов бывает дробной; целые печатаются без «.0»."""
    return f"{value:g}"


def format_repeated(summary: Dict[str, Any]) -> str:
    lines = [f"# Повторные прогоны золотого набора — {summary['title']}", "",
             summary["description"], "",
             f"Прогонов: {summary['repeat']}. Последний: {summary['timestamp']}.", "",
             "## Сводка", "",
             "| Показатель | Медиана | Разброс |", "|---|---|---|"]
    total = summary["total"]
    lo, hi = summary["passed_range"]
    lines.append(f"| Совпало с разметкой | {_n(summary['passed'])} из {_n(total)} "
                 f"({summary['accuracy']:.1%}) | {lo}–{hi} |")
    lo, hi = summary["unsafe_range"]
    lines.append(f"| Недопустимых действий | {_n(summary['unsafe'])} ({summary['unsafe_rate']:.1%}) "
                 f"| {lo}–{hi} |")
    for group, bucket in summary["by_group"].items():
        lo, hi = bucket["pass_range"]
        lines.append(f"| Пройдено, {group} | {_n(bucket['pass'])} из {bucket['total']} | {lo}–{hi} |")
    if summary.get("failed"):
        lines.append(f"| Сбоев связи с моделью за все прогоны | {summary['failed']} | |")
    lines += ["", "## Нестабильные реплики", ""]
    if summary["unstable"]:
        lines += ["Исход этих реплик менялся от прогона к прогону:", "",
                  "| id | Исходы по прогонам |", "|---|---|"]
        lines += [f"| {case_id} | {', '.join(verdicts)} |"
                  for case_id, verdicts in sorted(summary["unstable"].items())]
    else:
        lines.append("Исход всех реплик совпал во всех прогонах.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Прогон золотого набора реплик игрока.")
    parser.add_argument("--config", default="symbolic", choices=AVAILABLE_CONFIGS,
                        help="какую конфигурацию системы проверять")
    parser.add_argument("--golden-set", type=Path, default=GOLDEN_SET_PATH)
    parser.add_argument("--report", action="store_true",
                        help="сохранить отчёт в eval/reports/")
    parser.add_argument("--only-failures", action="store_true",
                        help="в консоли показать только расхождения")
    parser.add_argument("--model",
                        help="ключ модели из llm_config.json на время прогона; "
                             "файл настроек при этом не меняется")
    parser.add_argument("--repeat", type=int, default=1,
                        help="сколько раз прогнать набор; в сводку идут медиана и разброс")
    args = parser.parse_args()

    cases = load_golden_set(args.golden_set)
    # Конфигурации с языковой моделью прогоняются при зафиксированных параметрах,
    # иначе два прогона одного набора несопоставимы.
    profile = None
    if args.config != "symbolic":
        from eval.model_profile import apply
        profile = apply(args.model)
        print("Модель: " + ", ".join(f"{key} {value}" for key, value in profile.items()))
        print()
    config = load_config(args.config)
    runs = []
    for index in range(max(1, args.repeat)):
        if args.repeat > 1:
            print(f"Прогон {index + 1} из {args.repeat}...")
        runs.append(run(config, cases))

    rows, summary = runs[-1]
    if len(runs) > 1:
        summary = aggregate(runs)
        report = format_repeated(summary)
    else:
        report = format_report(rows, summary)
    print(report)

    if args.report:
        REPORTS_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d")
        (REPORTS_DIR / f"{stamp}-{config.key}.md").write_text(report, encoding="utf-8")
        (REPORTS_DIR / f"{stamp}-{config.key}.json").write_text(
            json.dumps({"summary": summary, "model": profile, "rows": rows,
                        "all_rows": [run_rows for run_rows, _ in runs] if len(runs) > 1 else None},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Отчёт сохранён в {REPORTS_DIR}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
