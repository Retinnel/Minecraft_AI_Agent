"""Сводная таблица по прогонам разных конфигураций.

Собирается из сохранённых отчётов, а не пересчитывается заново: конфигурации с языковой
моделью прогоняются долго, и сравнивать нужно именно те прогоны, которые были проведены,
с их зафиксированными параметрами модели.

    .venv/Scripts/python.exe -m eval.compare
    .venv/Scripts/python.exe -m eval.compare --save
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
REPORTS_DIR = ROOT / "reports"

# Порядок колонок: от простого к сложному, чтобы таблица читалась как рассуждение.
ORDER = ("llm_only", "symbolic", "hybrid")

GROUPS = (("typical", "типичные"), ("boundary", "граничные"), ("negative", "отрицательные"))


def latest(config: str) -> Optional[Dict[str, Any]]:
    """Самый свежий прогон конфигурации."""
    files = sorted(REPORTS_DIR.glob(f"*-{config}.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _number(value: float) -> str:
    return f"{value:g}"


def _share(value: float, total: float, spread=None) -> str:
    """Значение с долей; для повторных прогонов — медиана и разброс."""
    if not total:
        return "—"
    text = f"{_number(value)} ({value / total:.1%})"
    if spread and spread[0] != spread[1]:
        text += f", разброс {_number(spread[0])}–{_number(spread[1])}"
    return text


def format_table(runs: Dict[str, Dict[str, Any]]) -> str:
    keys = [key for key in ORDER if key in runs]
    titles = [runs[key]["summary"]["title"] for key in keys]

    lines: List[str] = []
    lines.append("# Сравнение конфигураций на золотом наборе")
    lines.append("")
    lines.append(f"Составлено: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")

    sizes = {runs[key]["summary"]["total"]
             + runs[key]["summary"].get("failed", 0) / runs[key]["summary"].get("repeat", 1)
             for key in keys}
    if len(sizes) > 1:
        lines.append("> ВНИМАНИЕ: прогоны выполнены на разных по объёму наборах "
                     f"({', '.join(str(size) for size in sorted(sizes))} реплик) "
                     "и строго несопоставимы. Нужен повторный прогон отставших конфигураций.")
        lines.append("")

    for key in keys:
        summary = runs[key]["summary"]
        model = runs[key].get("model")
        where = f", модель {model['модель']} ({model['провайдер']}), температура {model['температура']}" if model else ""
        lines.append(f"- {summary['title']}: прогон от {summary['timestamp']}{where}.")
    lines.append("")

    header = "| Показатель | " + " | ".join(titles) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(keys) + 1))

    def row(name: str, values: List[str]) -> None:
        lines.append(f"| {name} | " + " | ".join(values) + " |")

    row("Всего примеров", [str(runs[key]["summary"]["total"]) for key in keys])
    row("Прогонов", [str(runs[key]["summary"].get("repeat", 1)) for key in keys])
    row("Совпало с разметкой",
        [_share(runs[key]["summary"]["passed"], runs[key]["summary"]["total"],
                runs[key]["summary"].get("passed_range")) for key in keys])
    row("Недопустимых действий",
        [_share(runs[key]["summary"]["unsafe"], runs[key]["summary"]["total"],
                runs[key]["summary"].get("unsafe_range")) for key in keys])
    for group, label in GROUPS:
        values = []
        for key in keys:
            bucket = runs[key]["summary"]["by_group"].get(group)
            values.append(_share(bucket["pass"], bucket["total"], bucket.get("pass_range"))
                          if bucket else "—")
        row(f"Пройдено, {label}", values)
    row("Время отклика, медиана, мс",
        [f"{runs[key]['summary']['latency_ms']['median']:.0f}" for key in keys])
    row("Время отклика, максимум, мс",
        [f"{runs[key]['summary']['latency_ms']['max']:.0f}" for key in keys])

    lines.append("")
    lines.append("## Чем различаются ошибки")
    lines.append("")
    lines.append("Доля совпадений говорит о качестве, но не о цене ошибки. Отказ игрок видит и")
    lines.append("может переспросить; недопустимое действие происходит молча и выглядит как успех.")
    lines.append("")
    verdicts = sorted({verdict for key in keys for verdict in runs[key]["summary"]["verdicts"]})
    lines.append("| Исход | " + " | ".join(titles) + " |")
    lines.append("|" + "---|" * (len(keys) + 1))
    for verdict in verdicts:
        row(verdict, [_number(runs[key]["summary"]["verdicts"].get(verdict, 0)) for key in keys])

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--save", action="store_true", help="сохранить в eval/reports/")
    args = parser.parse_args()

    runs = {key: run for key in ORDER if (run := latest(key)) is not None}
    missing = [key for key in ORDER if key not in runs]
    if missing:
        print(f"Нет прогонов: {', '.join(missing)}. Запусти eval.runner --config <ключ> --report\n")
    if not runs:
        return 1

    table = format_table(runs)
    print(table)

    if args.save:
        target = REPORTS_DIR / f"{datetime.now():%Y-%m-%d}-comparison.md"
        target.write_text(table, encoding="utf-8")
        print(f"Сохранено: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
