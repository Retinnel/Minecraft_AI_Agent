"""Замер распознавания экрана на настоящих кадрах: точность, задержка, токены, цена.

Кадры берутся из папки — например, из data/vision_frames/<дата>/, куда их складывает
наблюдатель со включённым «сохранять кадры». Разметка — необязательный JSON рядом:

    {"120501-123.jpg": {"scene": "inventory", "focus": "Graphics card",
                        "items": ["Graphics card", "Bitcoin"], "options": 0}}

Сравнение без учёта регистра: сцена и предмет под курсором — точное совпадение, видимые
предметы — доля найденных, диалог — совпало ли число вариантов ответа. Без разметки
печатается, что увидела модель, и меряются задержка и расход.

    .venv/Scripts/python.exe -m eval.vision_probe data/vision_frames/2026-11-24
    .venv/Scripts/python.exe -m eval.vision_probe кадры --truth разметка.json --width 640 --width 1280 --report

Каждый кадр — один запрос к deepseek-flash, около 0,0002 $.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from PIL import Image  # noqa: E402

from vision.scene import SceneFacts, VisionModel, VisionUnavailable  # noqa: E402

REPORTS = ROOT / "eval" / "reports"
IMAGES = (".jpg", ".jpeg", ".png", ".webp")

# Тариф DeepSeek-V4.1-Flash в часы пик, $ за миллион токенов (обзоры, ноябрь 2026); ночью
# вдвое дешевле. Только для оценки порядка цены.
PRICE_INPUT = 0.30
PRICE_CACHED = 0.006
PRICE_OUTPUT = 1.20


def _same(a: Optional[str], b: Optional[str]) -> bool:
    """Совпало ли распознанное с разметкой.

    В разметке допустимы варианты через «|»: «Печь|Furnace». Подписанный предмет модель
    называет так, как он написан в игре, неподписанный — официальным английским названием,
    и оба ответа верны.
    """
    seen = (a or "").strip().lower()
    return any(seen == variant.strip().lower() for variant in (b or "").split("|"))


def score(facts: SceneFacts, truth: Dict[str, Any]) -> Dict[str, Optional[float]]:
    result: Dict[str, Optional[float]] = {"scene": None, "focus": None, "items": None, "options": None}
    if "scene" in truth:
        result["scene"] = float(_same(facts.scene, truth["scene"]))
    if "focus" in truth:
        result["focus"] = float(_same(facts.focus, truth["focus"]))
    if truth.get("items"):
        result["items"] = sum(any(_same(seen, item) for seen in facts.items)
                              for item in truth["items"]) / len(truth["items"])
    if "options" in truth:
        found = len(facts.dialogue.options) if facts.dialogue else 0
        result["options"] = float(found == truth["options"])
    return result


def cost(usage: Dict[str, Any]) -> float:
    hit = usage.get("prompt_cache_hit_tokens") or 0
    miss = (usage.get("prompt_tokens") or 0) - hit
    return (miss * PRICE_INPUT + hit * PRICE_CACHED
            + (usage.get("completion_tokens") or 0) * PRICE_OUTPUT) / 1_000_000


def run(folder: Path, truth: Dict[str, Any], widths: List[int]) -> str:
    files = sorted(path for path in folder.iterdir() if path.suffix.lower() in IMAGES)
    if not files:
        raise SystemExit(f"В {folder} нет изображений")
    lines = [f"# Распознавание экрана: {folder.name}", "",
             f"Дата: {datetime.now().isoformat(timespec='seconds')}. Кадров: {len(files)}. "
             f"Ширина кадра для модели: {', '.join(map(str, widths))}.", ""]
    for width in widths:
        model = VisionModel(max_width=width)
        rows, scores = [], {"scene": [], "focus": [], "items": [], "options": []}
        latencies, prompt_tokens, costs, failures = [], [], [], 0
        for path in files:
            try:
                description = model.describe(Image.open(path))
            except VisionUnavailable as error:
                failures += 1
                rows.append(f"| {path.name} | сбой: {error} | | |")
                continue
            facts = description.facts
            latencies.append(description.elapsed_ms)
            prompt_tokens.append(description.usage.get("prompt_tokens") or 0)
            costs.append(cost(description.usage))
            marks = score(facts, truth.get(path.name, {}))
            for key, value in marks.items():
                if value is not None:
                    scores[key].append(value)
            seen = facts.focus or (facts.dialogue.line if facts.dialogue else None) or "—"
            rows.append(f"| {path.name} | {facts.scene or '—'} | {seen} | "
                        f"{round(description.elapsed_ms)} |")
            print(f"[{width}] {path.name}: {facts.scene}, {seen}, {round(description.elapsed_ms)} мс")
        lines += [f"## Ширина {width}", "",
                  f"- задержка: медиана {round(statistics.median(latencies)) if latencies else '—'} мс, "
                  f"максимум {round(max(latencies)) if latencies else '—'} мс;",
                  f"- входных токенов на кадр: {round(statistics.mean(prompt_tokens)) if prompt_tokens else '—'}; "
                  f"цена кадра около {statistics.mean(costs):.5f} $" if costs else "- цена: —",
                  f"- сбоев связи: {failures};"]
        for key, title in (("scene", "сцена"), ("focus", "предмет под курсором"),
                           ("items", "видимые предметы, доля найденных"),
                           ("options", "число вариантов диалога")):
            if scores[key]:
                lines.append(f"- {title}: {statistics.mean(scores[key]):.0%} на {len(scores[key])} кадрах;")
        lines += ["", "| Кадр | Сцена | Под курсором или реплика | мс |", "|---|---|---|---|", *rows, ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="папка с кадрами")
    parser.add_argument("--truth", type=Path, help="JSON с разметкой кадров")
    parser.add_argument("--width", type=int, action="append", help="ширина кадра для модели; можно несколько")
    parser.add_argument("--report", action="store_true", help="сохранить в eval/reports/")
    args = parser.parse_args()
    truth = json.loads(args.truth.read_text(encoding="utf-8")) if args.truth else {}
    report = run(args.folder, truth, args.width or [1280])
    print()
    print(report)
    if args.report:
        target = REPORTS / f"{datetime.now():%Y-%m-%d}-vision-probe.md"
        target.write_text(report, encoding="utf-8")
        print(f"Сохранено: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
