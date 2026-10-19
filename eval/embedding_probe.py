"""Замер: годятся ли векторные представления для сопоставления названий.

Проверяется, разделяет ли косинусная близость парафразы существующих предметов
(«светильник» → фонарь) и названия несуществующих («кракен», «звёздный меч»). Если
разделяет — векторный этап можно подключить к резолверу с порогом; если нет — любой порог
либо пропустит выдуманное, либо отсечёт верное.

Результат замера — в eval/reports/2026-10-19-embedding-probe.md: ни одна из двух
проверенных моделей не разделяет эти группы, и векторный этап в резолвер не включён.

Для запуска нужна библиотека fastembed, в зависимости проекта она не входит:

    .venv/Scripts/python.exe -m pip install fastembed
    .venv/Scripts/python.exe -m eval.embedding_probe
    .venv/Scripts/python.exe -m eval.embedding_probe --model sentence-transformers/paraphrase-multilingual-mpnet-base-v2

Модель скачивается в data/models/ (от 0,2 до 1 ГБ) и после замера может быть удалена.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kb.graph import KnowledgeBase  # noqa: E402

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Парафразы существующих предметов: верный ответ известен.
PARAPHRASES = {
    "светящийся камень": "minecraft:glowstone",
    "зелёная слизь": "minecraft:slime_ball",
    "огненный шар": "minecraft:fire_charge",
    "штука чтобы рубить деревья": None,  # родовое: топоров несколько
    "волшебное яблоко": "minecraft:enchanted_golden_apple",
    "блок для прыжков": "minecraft:slime_block",
    "железная броня на грудь": "minecraft:iron_chestplate",
    "лава в ведре": "minecraft:lava_bucket",
    "светильник": "minecraft:lantern",
    "сундук эндера": "minecraft:ender_chest",
    "лодочка": None,  # родовое: лодок несколько
}

# Несуществующее: правильный ответ — «такого нет».
NONEXISTENT = ["звёздный меч", "кракен", "единорог", "сто алмазных драконов", "гипербластер",
               "лазерный пистолет", "космический корабль"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    import numpy as np
    from fastembed import TextEmbedding

    started = time.perf_counter()
    model = TextEmbedding(args.model, cache_dir=str(ROOT / "data" / "models"))
    print(f"Модель {args.model} загружена за {time.perf_counter() - started:.1f} с")

    kb = KnowledgeBase.shared()
    names, owners = [], []
    for thing, naming in kb.index_names().items():
        for name in naming.names():
            names.append(name)
            owners.append(thing)
    vectors = np.array(list(model.embed(names)), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    print(f"Имён: {len(names)}, размерность {vectors.shape[1]}\n")

    def best(query: str):
        vector = np.array(list(model.embed([query])), dtype=np.float32)[0]
        vector /= np.linalg.norm(vector)
        scores = vectors @ vector
        index = int(np.argmax(scores))
        return owners[index], float(scores[index])

    print("| Запрос | Ожидалось | Лучший кандидат | Близость |")
    print("|---|---|---|---|")
    hits = []
    for query, expected in PARAPHRASES.items():
        thing, score = best(query)
        mark = "верно" if thing == expected else ("родовое" if expected is None else "неверно")
        hits.append((score, mark))
        print(f"| {query} | {kb.label(expected) if expected else 'класс'} | "
              f"{kb.label(thing)} ({mark}) | {score:.2f} |")
    misses = []
    for query in NONEXISTENT:
        thing, score = best(query)
        misses.append(score)
        print(f"| {query} | такого нет | {kb.label(thing)} | {score:.2f} |")

    right = [score for score, mark in hits if mark == "верно"]
    if right:
        print(f"\nВерные парафразы: близость от {min(right):.2f} до {max(right):.2f}")
    print(f"Несуществующее: близость от {min(misses):.2f} до {max(misses):.2f}")
    separable = bool(right) and min(right) > max(misses)
    print("Группы разделимы порогом." if separable else
          "Группы порогом не разделяются: векторный этап давал бы ложные кандидаты.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
