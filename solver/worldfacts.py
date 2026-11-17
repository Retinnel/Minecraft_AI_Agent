"""Вопросы о мире: обратные запросы к онтологии.

Планировщик отвечает на вопрос «как получить X». Здесь — остальные вопросы, которые игрок
задаёт об устройстве игры и на которые у онтологии есть прямой ответ:

    uses        что делается из предмета — рецепты, где он входит в состав;
    sources     откуда предмет берётся — из каких блоков и существ, каким инструментом и где;
    drops       что выпадает из блока или существа;
    available   что собирается прямо сейчас из инвентаря одним действием.

Ответ целиком берётся из базы знаний: языковая модель определяет только вид вопроса и
пересказывает найденное, а назвать предмет, которого в ответе нет, ей не даёт сверка
названий в формулировщике.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from kb.graph import KnowledgeBase, Recipe, Source
from solver.planner import STATION_ITEMS, UNNATURAL, Planner
from solver.render import REMOTE_RU, TIER_RU, TOOL_KIND_RU

USES = "ask_uses"
SOURCES = "ask_sources"
DROPS = "ask_drops"
AVAILABLE = "ask_available"
WORLD_ACTIONS = (USES, SOURCES, DROPS, AVAILABLE)

# Сколько названий перечислять: ответ читается в строке чата.
MAX_LISTED = 6


@dataclass
class WorldAnswer:
    question: str                       # uses | sources | drops | available
    subject: Optional[str] = None       # о чём спрошено
    items: List[str] = field(default_factory=list)       # что названо в ответе
    rare: List[str] = field(default_factory=list)        # ненадёжные выпадения, отдельной строкой
    total: int = 0                      # сколько всего найдено, если названо не всё
    craftable: bool = False             # sources: у предмета есть рецепт
    notes: Dict[str, str] = field(default_factory=dict)  # пояснение к названному: инструмент, место
    explanation: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.items and not self.rare and not self.craftable


class WorldFacts:
    def __init__(self, kb: KnowledgeBase, planner: Optional[Planner] = None) -> None:
        self.kb = kb
        self.planner = planner or Planner(kb)
        self._uses: Optional[Dict[str, List[Recipe]]] = None
        self._drops: Optional[Dict[str, List[Tuple[str, bool]]]] = None

    # --- что делается из предмета ---

    def uses(self, item: str) -> WorldAnswer:
        if self._uses is None:
            index: Dict[str, List[Recipe]] = {}
            for recipes in self.kb.index_recipes().values():
                for recipe in recipes:
                    for alternative in {alt for ing in recipe.inputs for alt in ing.alternatives}:
                        index.setdefault(alternative, []).append(recipe)
            self._uses = index
        products = sorted({recipe.produces for recipe in self._uses.get(item, [])
                           if recipe.produces != item}, key=self._prominence)
        answer = WorldAnswer(USES, subject=item, items=products[:MAX_LISTED], total=len(products))
        answer.explanation.append(
            f"«{self._label(item)}» входит в состав рецептов {len(products)} предметов")
        return answer

    def _prominence(self, item: str) -> Tuple[int, float, str]:
        """Что назвать первым: инструменты и оборудование, затем то, что проще получить."""
        if self.kb.tool_profile(item) is not None or item in STATION_ITEMS.values():
            group = 0
        elif item.endswith(("_helmet", "_chestplate", "_leggings", "_boots", "_sword",
                            "shield", "bucket")):
            group = 1
        else:
            group = 2
        return group, self.planner._item_cost(item), item

    # --- откуда предмет берётся ---

    def sources(self, item: str) -> WorldAnswer:
        found = [source for source in self.kb.sources_of(item) if source.remote != UNNATURAL]
        reliable = [source for source in found if not source.rare]
        answer = WorldAnswer(SOURCES, subject=item,
                             items=[source.id for source in reliable[:MAX_LISTED]],
                             rare=[source.id for source in found if source.rare][:MAX_LISTED],
                             total=len(reliable),
                             craftable=bool(self._real_recipes(item)))
        for source in found:
            answer.notes[source.id] = self._source_note(source)
        answer.explanation.append(
            f"у «{self._label(item)}» источников добычи: {len(reliable)}, ненадёжных: "
            f"{len(found) - len(reliable)}, рецептов: {len(self.kb.recipes_for(item))}")
        return answer

    def _real_recipes(self, item: str) -> List[Recipe]:
        """Рецепты, кроме «распаковки»: слиток из блока слитков способом получения не считается.

        Блок и девять его слитков превращаются друг в друга, и по данным игры это такой же
        рецепт, как любой другой. Игроку он ничего не говорит о том, откуда слиток взять.
        """
        real = []
        for recipe in self.kb.recipes_for(item):
            packed = len(recipe.inputs) == 1 and any(
                item in ingredient.alternatives
                for alternative in recipe.inputs[0].alternatives
                for back in self.kb.recipes_for(alternative)
                for ingredient in back.inputs)
            if not packed:
                real.append(recipe)
        return real

    def _source_note(self, source: Source) -> str:
        parts = []
        if source.tool_required and source.tool_kind:
            tool = TOOL_KIND_RU.get(source.tool_kind, source.tool_kind)
            tier = TIER_RU.get(source.tool_tier or "")
            parts.append(f"{tier} {tool} и выше" if tier else tool)
        where = REMOTE_RU.get(source.remote or "")
        if where:
            parts.append(where)
        return ", ".join(parts)

    # --- что выпадает из блока или существа ---

    def drops(self, thing: str) -> WorldAnswer:
        if self._drops is None:
            index: Dict[str, List[Tuple[str, bool]]] = {}
            for item, sources in self.kb.index_sources().items():
                for source in sources:
                    index.setdefault(source.id, []).append((item, source.rare))
            self._drops = index
        found = sorted(self._drops.get(thing, []), key=lambda pair: (pair[1], pair[0]))
        reliable = [item for item, rare in found if not rare and item != thing]
        answer = WorldAnswer(DROPS, subject=thing, items=reliable[:MAX_LISTED],
                             rare=[item for item, rare in found if rare][:MAX_LISTED],
                             total=len(reliable))
        answer.explanation.append(
            f"из «{self._label(thing)}» выпадает предметов: {len(reliable)}, "
            f"ненадёжно: {len(found) - len(reliable) - (1 if (thing, False) in found else 0)}")
        return answer

    # --- что собирается прямо сейчас ---

    def available(self, inventory: Optional[Dict[str, int]] = None) -> WorldAnswer:
        stock = {item: int(count) for item, count in (inventory or {}).items()
                 if int(count or 0) > 0}
        makeable: List[str] = []
        for item, recipes in self.kb.index_recipes().items():
            if stock.get(item, 0) > 0:
                continue
            if any(self._in_reach(recipe, stock) for recipe in recipes):
                makeable.append(item)
        # Сначала инструменты и оборудование, затем самое «дорогое»: оно дальше по развитию.
        makeable.sort(key=lambda item: (self._prominence(item)[0], -self.planner._item_cost(item),
                                        item))
        answer = WorldAnswer(AVAILABLE, items=makeable[:MAX_LISTED], total=len(makeable))
        answer.explanation.append(
            f"одним действием из инвентаря изготавливается предметов: {len(makeable)}")
        return answer

    @staticmethod
    def _in_reach(recipe: Recipe, stock: Dict[str, int]) -> bool:
        """Все составляющие рецепта в инвентаре, и оборудование для него есть."""
        if not recipe.inputs:
            return False
        station = STATION_ITEMS.get(recipe.station)
        if station and stock.get(station, 0) <= 0:
            return False
        need: Dict[str, int] = {}
        for ingredient in recipe.inputs:
            # Позиции рецепта делят один запас: два слитка на палку и три на кирку — это пять.
            chosen = next((alt for alt in ingredient.alternatives
                           if stock.get(alt, 0) - need.get(alt, 0) >= ingredient.quantity), None)
            if chosen is None:
                return False
            need[chosen] = need.get(chosen, 0) + ingredient.quantity
        return True

    def _label(self, thing: str) -> str:
        return self.kb.label(thing) or thing


def _inline(label: str) -> str:
    return label[:1].lower() + label[1:] if label else label


def render_answer(answer: WorldAnswer, kb: KnowledgeBase) -> str:
    """Ответ словами — из фактов базы знаний, без модели."""
    def name(thing: str) -> str:
        return _inline(kb.label(thing) or thing)

    def listed(things: Sequence[str], notes: bool = False) -> str:
        parts = []
        for thing in things:
            note = answer.notes.get(thing) if notes else None
            parts.append(f"{name(thing)} ({note})" if note else name(thing))
        return ", ".join(parts)

    subject = name(answer.subject) if answer.subject else ""
    more = answer.total - len(answer.items)
    tail = f" и ещё {more}" if more > 0 else ""

    if answer.question == USES:
        if not answer.items:
            return f"{_cap(subject)} ни в один рецепт не входит."
        return (f"{_cap(subject)} входит в рецепты предметов: {answer.total}. "
                f"Например: {listed(answer.items)}.")

    # «Ненадёжное» выпадение в базе знаний — то, что может не выпасть вовсе: порох с
    # крипера, кожа с коровы. Это не редкость, поэтому сказано «не каждый раз».
    if answer.question == SOURCES:
        parts = []
        if answer.items:
            parts.append(f"{_cap(subject)} — источники: {listed(answer.items, notes=True)}{tail}.")
        if answer.rare:
            parts.append((f"Не каждый раз: {listed(answer.rare, notes=True)}." if parts else
                          f"{_cap(subject)} — источники (выпадает не каждый раз): "
                          f"{listed(answer.rare, notes=True)}."))
        if answer.craftable:
            parts.append("Кроме того, изготавливается — спроси «как сделать», покажу путь."
                         if parts else
                         f"{_cap(subject)} в мире не добывается, а изготавливается — "
                         f"спроси «как сделать», покажу путь.")
        if not parts:
            return (f"{_cap(subject)}: в базе знаний нет ни источника добычи, ни рецепта — "
                    f"получается способом, которого нет в данных игры.")
        return " ".join(parts)

    if answer.question == DROPS:
        parts = []
        if answer.items:
            parts.append(f"{_cap(subject)} — выпадает: {listed(answer.items)}{tail}.")
        if answer.rare:
            parts.append(f"Не каждый раз: {listed(answer.rare)}." if parts else
                         f"{_cap(subject)} — выпадает не каждый раз: {listed(answer.rare)}.")
        return " ".join(parts) or f"{_cap(subject)} — ничего не выпадает."

    if not answer.items:
        return "Из того, что лежит в инвентаре, одним действием ничего не собрать."
    return f"Прямо сейчас из инвентаря собирается: {listed(answer.items)}{tail}."


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def essentials(answer: WorldAnswer, kb: KnowledgeBase) -> List[str]:
    """Названия, которые пересказ обязан сохранить: всё перечисленное в ответе."""
    return [kb.label(thing) or thing for thing in answer.items + answer.rare]
