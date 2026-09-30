"""Лексическое сопоставление запроса с сущностями онтологии.

Индекс строится по всем именам из базы знаний: официальному русскому названию, разговорным
синонимам и английскому названию. Имена разной природы взвешены по-разному — совпадение с
официальным названием весомее совпадения с синонимом.

Это существенно для принятой политики. Слово «меч» записано синонимом алмазного меча ещё с
прошлой версии справочника, и при равном весе имён запрос «дай меч» молча выдавал бы алмазный.
При разном весе синоним не перевешивает шесть частичных совпадений с официальными названиями
мечей, и запрос признаётся неоднозначным. Никаких правил про конкретные предметы в системе нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from kb.graph import KnowledgeBase
from resolver.text import name_stems, query_stems, tokenize

# Вес имени: насколько совпадение с ним заслуживает доверия.
WEIGHT_LABEL = 1.00    # официальное русское название
WEIGHT_ALIAS = 0.80    # разговорный синоним
WEIGHT_ENGLISH = 0.70  # английское название

# Нечёткое сравнение основ закрывает то, чего не берёт стеммер: беглые гласные,
# опечатки, чередования. Совпадение засчитывается не полностью — оно менее надёжно.
FUZZY_THRESHOLD = 0.80
FUZZY_CREDIT = 0.75

# Доля запроса, которую имя обязано объяснить, чтобы вообще считаться кандидатом.
# Этим «дай меч» отличается от «дай звёздный меч»: в первом случае слово одно и любой меч
# объясняет запрос целиком — выбор за игроком; во втором слово «звёздный» не объясняет
# никто, и это не неоднозначность, а несуществующий предмет.
MIN_QUERY_COVERAGE = 0.75


@dataclass(frozen=True)
class Candidate:
    """Сущность, подошедшая под запрос, с оценкой уверенности."""

    thing: str
    label: str
    kinds: Sequence[str]
    score: float
    matched: str  # имя, по которому сопоставилось — для объяснения игроку
    coverage_query: float = 1.0  # какую долю запроса объясняет это имя


@dataclass(frozen=True)
class _Name:
    thing: str
    text: str
    stems: Tuple[str, ...]
    weight: float


class LexicalIndex:
    """Обратный индекс «основа слова → имена», построенный один раз на процесс."""

    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb
        self._names: List[_Name] = []
        self._by_stem: Dict[str, Set[int]] = {}
        self._labels: Dict[str, str] = {}
        self._kinds: Dict[str, Sequence[str]] = {}
        self._build()

    def _build(self) -> None:
        generic = self._generic_terms()
        for thing, naming in self.kb.index_names().items():
            self._labels[thing] = naming.label_ru or naming.label_en or thing
            self._kinds[thing] = naming.kinds
            weighted: List[Tuple[str, float]] = []
            if naming.label_ru:
                weighted.append((naming.label_ru, WEIGHT_LABEL))
            weighted.extend(
                (alias, WEIGHT_ALIAS)
                for alias in naming.aliases
                if (thing, tuple(sorted(tokenize(alias)))) not in generic
            )
            if naming.label_en:
                weighted.append((naming.label_en, WEIGHT_ENGLISH))

            seen: Set[Tuple[str, ...]] = set()
            for text, weight in weighted:
                stems = name_stems(text)
                if not stems or stems in seen:
                    continue
                seen.add(stems)
                index = len(self._names)
                self._names.append(_Name(thing=thing, text=text, stems=stems, weight=weight))
                for token in stems:
                    self._by_stem.setdefault(token, set()).add(index)

    def _generic_terms(self) -> Set[Tuple[str, Tuple[str, ...]]]:
        """Синонимы, оказавшиеся названием целого класса, а не одной сущности.

        «Меч» записан синонимом алмазного меча ещё в рукописном каталоге прошлого семестра —
        и это то же самое умолчание, от которого мы отказались: запрос «дай меч» допускает
        шесть прочтений, и выбирать за игрока система не должна. Признак родового термина
        объективен: слова синонима целиком входят в официальное название другой сущности.
        Такие синонимы в индекс не попадают, и запрос сопоставляется только с названиями —
        со всеми сразу, что и даёт неоднозначность вместо молчаливой подстановки.
        """
        by_token: Dict[str, Set[str]] = {}
        for thing, naming in self.kb.index_names().items():
            for token in tokenize(naming.label_ru or ""):
                by_token.setdefault(token, set()).add(thing)

        generic: Set[Tuple[str, Tuple[str, ...]]] = set()
        for thing, naming in self.kb.index_names().items():
            for alias in naming.aliases:
                tokens = tokenize(alias)
                if not tokens:
                    continue
                owners: Optional[Set[str]] = None
                for token in tokens:
                    holders = by_token.get(token, set())
                    owners = set(holders) if owners is None else owners & holders
                    if not owners:
                        break
                if owners and owners - {thing}:
                    generic.add((thing, tuple(sorted(tokens))))
        return generic

    @property
    def vocabulary(self) -> Iterable[str]:
        return self._by_stem.keys()

    def _expand(self, stem: str) -> List[Tuple[str, float]]:
        """Основа запроса и близкие к ней основы словаря с понижающим коэффициентом.

        Сравнение идёт со словарём основ, а не с полутора тысячами сущностей: одинаковые
        слова в названиях встречаются постоянно, и считать их сходство заново для каждой
        сущности незачем.
        """
        if stem in self._by_stem:
            return [(stem, 1.0)]

        matches: List[Tuple[str, float]] = []
        for candidate in self._by_stem:
            # Основы, различающиеся длиной более чем на треть, до порога сходства не дойдут.
            if abs(len(candidate) - len(stem)) * 3 > max(len(candidate), len(stem)):
                continue
            ratio = SequenceMatcher(None, stem, candidate).ratio()
            if ratio >= FUZZY_THRESHOLD:
                matches.append((candidate, FUZZY_CREDIT * ratio))
        matches.sort(key=lambda pair: -pair[1])
        return matches

    def search(
        self,
        query: str,
        *,
        kinds: Optional[Sequence[str]] = None,
        limit: int = 5,
    ) -> List[Candidate]:
        """Кандидаты по убыванию оценки. Пустой список означает «ничего похожего»."""
        stems = query_stems(query)
        if not stems:
            return []

        # Для каждой основы запроса — с каким весом она засчитывается каждому имени.
        credits: Dict[int, Dict[str, float]] = {}
        for stem in stems:
            for matched_stem, credit in self._expand(stem):
                for index in self._by_stem.get(matched_stem, ()):
                    per_name = credits.setdefault(index, {})
                    if credit > per_name.get(stem, 0.0):
                        per_name[stem] = credit

        best: Dict[str, Candidate] = {}
        for index, per_stem in credits.items():
            name = self._names[index]
            if kinds and not set(self._kinds.get(name.thing, ())) & set(kinds):
                continue

            overlap = sum(per_stem.values())
            coverage_name = overlap / len(name.stems)
            coverage_query = overlap / len(stems)
            # Для отсева считаем слова, а не доли: слово либо объяснено именем, либо нет.
            # Нечёткость снижает уверенность в совпадении, но не превращает слово
            # в наполовину объяснённое.
            if len(per_stem) / len(stems) < MIN_QUERY_COVERAGE:
                continue
            # Оценка падает и от того, что имя объяснено не полностью, и от того, что не
            # полностью объяснён запрос. Произведение, а не сумма: слабость по любой из
            # сторон должна отбрасывать кандидата, а не усредняться со второй.
            score = name.weight * coverage_name * coverage_query

            current = best.get(name.thing)
            if current is None or score > current.score:
                best[name.thing] = Candidate(
                    thing=name.thing,
                    label=self._labels.get(name.thing, name.thing),
                    kinds=self._kinds.get(name.thing, ()),
                    score=round(score, 4),
                    matched=name.text,
                    coverage_query=round(coverage_query, 4),
                )

        candidates = sorted(best.values(), key=lambda item: (-item.score, item.thing))
        return candidates[: max(1, limit)]
