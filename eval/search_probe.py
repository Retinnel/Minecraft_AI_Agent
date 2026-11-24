"""Замер веб-поиска на живых вопросах: маршрут, запрос, источники, задержка, ответ.

Прогоняет вопросы через весь конвейер с настоящей моделью и настоящим поиском. Проверяется
не только качество ответа, но и маршрутизация: вопросы о рецептах и добыче должны уходить в
базу знаний, а не в сеть, — иначе поиск подменил бы проверенные знания непроверенными.

    .venv/Scripts/python.exe -m eval.search_probe
    .venv/Scripts/python.exe -m eval.search_probe --report

Каждый вопрос, ушедший в поиск, стоит один кредит Tavily (бесплатно 1000 в месяц).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from orchestrator import Pipeline  # noqa: E402
from orchestrator.extractor import LanguageModelIntent  # noqa: E402
from orchestrator.responder import Responder  # noqa: E402

REPORTS = ROOT / "eval" / "reports"

# (игра, реплика, ожидаемое действие). Ожидание — маршрут, а не текст ответа. «refuse» —
# просьба, которую в этой игре выполнить нельзя: годится и отказ, и разговор, лишь бы без
# команды; что ответ при этом объясняет, почему, видно в разделе ответов.
QUESTIONS = [
    ("Minecraft", "как победить визера", "search_web"),
    ("Minecraft", "где найти крепость края", "search_web"),
    ("Minecraft", "что делает зачарование починка", "search_web"),
    ("Minecraft", "как сделать алмазную кирку", "plan_goal"),
    ("Minecraft", "откуда берётся порох", "ask_sources"),
    ("Elden Ring", "как победить Маргита", "search_web"),
    ("The Witcher 3", "что ответить Кровавому барону, чтобы спасти Анну", "search_web"),
    ("Stardew Valley", "для чего нужен аккумулятор", "search_web"),
    ("Elden Ring", "дай мне меч", "refuse"),
    ("Minecraft", "как приручить розового единорога в аду", "search_web"),
]


def run(model_key: str | None, engines: list[str] | None = None) -> str:
    if model_key:
        from eval.model_profile import apply
        apply(model_key)
    search = None
    if engines:
        # Замер конкретного движка: без запасных, чтобы не подменили результат.
        from search.engines import default_engines
        from search.service import WebSearch
        search = WebSearch(default_engines(engines))
        if not search.engines:
            raise SystemExit(f"Нет доступных движков из {engines}: нужен ключ или библиотека")
    pipeline = Pipeline(LanguageModelIntent(), search=search)
    responder = Responder()

    rows = []
    for game, utterance, expected in QUESTIONS:
        started = time.perf_counter()
        turn = pipeline.handle(utterance, game=game)
        routed = time.perf_counter()
        reply = responder.reply(turn, utterance)
        finished = time.perf_counter()
        search = turn.stages.get("search") or {}
        if expected == "refuse":
            ok = turn.command is None and turn.outcome in ("none", "reject")
        else:
            ok = turn.action == expected
        rows.append({
            "game": game, "utterance": utterance, "expected": expected,
            "route": turn.action or "none", "outcome": turn.outcome,
            "ok": ok,
            "query": search.get("query", "—"), "engine": search.get("engine") or "—",
            "sources": ", ".join(search.get("sources") or []) or "—",
            "scores": ", ".join(f"{score:.2f}" for score in search.get("scores") or []) or "—",
            "rejected": search.get("rejected", 0),
            "search_ms": search.get("elapsed_ms", 0), "total_ms": round((finished - started) * 1000),
            "route_ms": round((routed - started) * 1000), "reply": reply.text, "by": reply.source,
        })
        print(f"[{'+' if rows[-1]['ok'] else '-'}] {game}: {utterance} → {turn.action} "
              f"({turn.outcome}), {rows[-1]['total_ms']} мс, {rows[-1]['sources']}")

    passed = sum(row["ok"] for row in rows)
    searched = [row for row in rows if row["route"] == "search_web" and row["outcome"] == "act"]
    lines = [
        "# Веб-поиск на живых вопросах", "",
        f"Дата: {datetime.now().isoformat(timespec='seconds')}. "
        f"Маршрут совпал с ожидаемым: {passed} из {len(rows)}. "
        f"Ответов из сети: {len(searched)}.", "",
        "| Игра | Реплика | Ждали | Маршрут | Источники | Всего, мс |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| {row['game']} | {row['utterance']} | {row['expected']} | "
                     f"{row['route']} ({row['outcome']}) | {row['sources']} | {row['total_ms']} |")
    lines += ["", "## Запросы и ответы", ""]
    for row in rows:
        lines += [f"**{row['game']}: {row['utterance']}**", "",
                  f"- поисковый запрос: `{row['query']}`, движок: {row['engine']}, "
                  f"поиск {row['search_ms']} мс; релевантность принятых страниц на общей "
                  f"шкале: {row['scores']}; отброшено по порогу: {row['rejected']}",
                  f"- ответ ({row['by']}): {row['reply']}", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="ключ модели из llm_config.json")
    parser.add_argument("--report", action="store_true", help="сохранить в eval/reports/")
    parser.add_argument("--engine", action="append",
                        help="поисковый движок: tavily, deepseek, duckduckgo; можно несколько — "
                             "по порядку; без ключа — как на сервере")
    args = parser.parse_args()
    # Журнал печатает стрелки и кавычки; консоль Windows в cp1251 на них падала.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    report = run(args.model, args.engine)
    print()
    print(report)
    if args.report:
        tag = "-" + "-".join(args.engine) if args.engine else ""
        target = REPORTS / f"{datetime.now():%Y-%m-%d}-search-probe{tag}.md"
        target.write_text(report, encoding="utf-8")
        print(f"Сохранено: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
