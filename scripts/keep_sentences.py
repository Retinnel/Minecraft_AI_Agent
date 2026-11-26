"""Не даёт рисунку или таблице разорвать предложение на стыке страниц записки.

LaTeX ставит плавающий объект в начало страницы, даже если абзац перед ним ещё не
закончился: предложение начинается внизу одной страницы, а заканчивается под рисунком на
следующей. Скрипт собирает записку, находит такие стыки по тексту PDF и запрещает перенос
на новую страницу внутри найденного абзаца (`\\interlinepenalty=10000`): абзац целиком
уходит под рисунок. После каждой правки раскладка сдвигается, поэтому проверка повторяется,
пока разрывов не останется.

Запуск из корня репозитория (нужен MiKTeX в PATH):
    .venv/Scripts/python.exe scripts/keep_sentences.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "report" / "cource_report.pdf"
SECTIONS = sorted((ROOT / "report" / "sections").glob("*.tex"))
MARK = "{\\interlinepenalty=10000 "
MAX_ROUNDS = 12
WORD = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)


def words(text: str) -> list:
    return [word.lower() for word in WORD.findall(text.replace("ё", "е"))]


def splits() -> list:
    """Стыки страниц, где страница начинается объектом, а предыдущая кончается не точкой.

    Возвращает последние слова незаконченного абзаца — по ним он ищется в исходниках."""
    pages = [(page.extract_text() or "") for page in PdfReader(str(PDF)).pages]
    found = []
    for index in range(1, len(pages)):
        lines = [line.strip() for line in pages[index - 1].splitlines() if line.strip()]
        while lines and re.fullmatch(r"\d+", lines[-1]):
            lines.pop()
        tail = " ".join(lines[-3:])
        if not lines or re.search(r"[.:;!?»)]$", lines[-1]) or not re.search(r"[а-яё]", lines[-1]):
            continue
        head = [line.strip() for line in pages[index].splitlines() if line.strip()][:1]
        caption = re.search(r"(Рисунок|Таблица) \d+\.\d+", pages[index])
        if not caption or not head:
            continue
        # Объект вверху: страница открывается подписью таблицы или содержимым рисунка
        # (подпись рисунка стоит под ним, в первой четверти текста страницы).
        at_top = head[0].startswith("Таблица") or caption.start() / max(1, len(pages[index])) < 0.25
        continues = head[0][:1].islower() and not head[0].startswith(("mod", "0"))
        if at_top and not continues:
            cut = words(tail)
            found.append((index + 1, cut[-7:-1] if len(cut) > 7 else cut[:-1]))
    return found


def protect(tail: list) -> bool:
    """Находит абзац, содержащий эти слова подряд, и запрещает разрывать его страницей."""
    for path in SECTIONS:
        text = path.read_text(encoding="utf-8")
        blocks = text.split("\n\n")
        for number, block in enumerate(blocks):
            if block.lstrip().startswith(("\\", "%", MARK)) or "\\begin" in block or "\\item" in block:
                continue
            sequence = words(block)
            size = len(tail)
            if size and any(sequence[i:i + size] == tail for i in range(len(sequence) - size + 1)):
                blocks[number] = MARK + block.strip("\n") + "\\par}"
                path.write_text("\n\n".join(blocks), encoding="utf-8", newline="\n")
                print(f"  абзац в {path.name}: …{' '.join(tail)}")
                return True
    return False


def build() -> None:
    subprocess.run(["sh", "report/build.sh"], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for round_number in range(1, MAX_ROUNDS + 1):
        build()
        found = splits()
        print(f"проход {round_number}: разрывов {len(found)}", [page for page, _ in found])
        if not found:
            return 0
        if not any(protect(tail) for _, tail in found):
            print("  абзацы в исходниках не найдены — дальше вручную")
            return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
