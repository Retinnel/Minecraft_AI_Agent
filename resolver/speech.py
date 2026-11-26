"""Правка распознанной речи по словарю онтологии — до разбора реплики.

Распознавание речи ошибается характерно: склеивает слова («Пустьделаеткаменнуюкирку»),
ставит в конец чужую точку («начинай。»). Модель такие
реплики понимает через раз, и исход начинает зависеть от случая. Здесь это чинится
символьно и предсказуемо:

- склеенное слово режется на части, только если каждая часть — известное слово: слово
  команды или слово из названий базы знаний (сверка по основам, как в резолвере);
- знаки препинания других письменностей убираются.

Слова, которых правка не узнала, остаются как были: текст не «улучшается», а только
возвращается к словам, которые система и так знает. Что было услышано до правки,
сохраняется и показывается.
"""

from __future__ import annotations

import re
from typing import Collection, List, Optional

from resolver.text import stem

# Слова, из которых игрок строит просьбу: глаголы, частицы, обращения. Не предметы —
# предметы берутся из онтологии игры.
COMMAND_WORDS = frozenset("""
дай дайте выдай принеси неси сделай сделать сделает делает делай скрафти скрафтить создай
построй добудь добыть найди призови вызови убери забери запомни напомни забудь покажи
расскажи скажи начинай начни продолжай стоп стой хватит пусть давай хочу нужен нужна нужно
нужны надо как что где чем кто из для мне нам ему ещё еще тоже также одну один два три
помощник помощника помощнику ассистент сам сама мой моя режим выживания творческий
каменную железную алмазную золотую деревянную
""".split())

# Слово длиннее этого и неизвестное системе — подозрение на склейку.
MIN_GLUED = 10
# Часть склейки короче не считается словом названия: иначе «и», «на», «ка» режут всё подряд.
MIN_PIECE = 3
# Похожие слова не подменяются намеренно: «сделал» превращалось в «сделай», а это уже
# не правка распознавания, а правка смысла.

FOREIGN_PUNCTUATION = re.compile(r"[。、，！？；：「」『』（）]")
WORD = re.compile(r"[a-zа-яё]+", re.IGNORECASE)


def _known(word: str, vocabulary: Collection[str]) -> bool:
    return word in COMMAND_WORDS or (len(word) >= MIN_PIECE and stem(word) in vocabulary)


def split_glued(word: str, vocabulary: Collection[str]) -> Optional[List[str]]:
    """Разрезает слово на известные слова; None, если целиком так не раскладывается.

    Из разбиений берётся то, где частей меньше: «каменную» — одно слово, а не «камен»
    и «ную». Нужно не меньше двух частей — иначе это не склейка.
    """
    size = len(word)
    best: List[Optional[List[str]]] = [None] * (size + 1)
    best[0] = []
    for end in range(1, size + 1):
        for start in range(end):
            head = best[start]
            piece = word[start:end]
            if head is None or not _known(piece, vocabulary):
                continue
            if best[end] is None or len(head) + 1 < len(best[end]):
                best[end] = head + [piece]
    found = best[size]
    return found if found and len(found) >= 2 else None


def repair(text: str, vocabulary: Collection[str]) -> str:
    """Распознанная реплика, возвращённая к словам, которые система знает."""
    cleaned = FOREIGN_PUNCTUATION.sub(" ", text or "")

    def fix(match: "re.Match[str]") -> str:
        original = match.group(0)
        word = original.lower().replace("ё", "е")
        if _known(word, vocabulary) or _known(original.lower(), vocabulary):
            return original
        if len(word) >= MIN_GLUED:
            pieces = split_glued(word, vocabulary)
            if pieces:
                return " ".join(pieces)
        return original

    return " ".join(WORD.sub(fix, cleaned).split())
