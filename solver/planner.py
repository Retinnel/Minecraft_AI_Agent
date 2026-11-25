"""Планировщик достижения цели.

Отвечает на вопрос «как получить N штук предмета X, если у игрока сейчас вот это».
Работает обратным выводом от цели: сначала пытается взять из инвентаря, затем из
известных хранилищ игрока, затем изготовить, затем добыть. Если для добычи нужен инструмент, которого нет, план рекурсивно достраивает
и его.

Планировщик обращается только к словарю ядра онтологии и не знает, что предметная область —
именно Minecraft.
"""

from __future__ import annotations

from collections import Counter
from functools import cached_property
from dataclasses import dataclass, field, replace
from math import ceil
from typing import Dict, List, Optional, Sequence, Tuple

from kb.graph import KnowledgeBase, Recipe, Source

# Как получен предмет на данном шаге.
HAVE = "have"          # уже в инвентаре
TAKE = "take"          # лежит в хранилище игрока — взять
CRAFT = "craft"        # изготовить по рецепту
SMELT = "smelt"        # переплавить
STONECUT = "stonecut"  # вырезать камнерезом
SMITH = "smith"        # выковать
MINE = "mine"          # добыть из блока
HUNT = "hunt"          # выбить из существа
MISSING = "missing"    # получить не удалось

METHOD_STEPS = {"craft": CRAFT, "smelt": SMELT, "stonecut": STONECUT, "smith": SMITH}

# Станция рецепта как предмет, который для неё нужен. Ручной крафт («inventory») станции
# не требует. Станция в план не входит шагом, но её цена учитывается при выборе рецепта:
# иначе между печью и доменной печью побеждал бы алфавит, а доменная печь сама требует
# печи. Кампания превращает недостающую станцию в отдельный этап.
STATION_ITEMS = {
    "crafting_table": "minecraft:crafting_table",
    "furnace": "minecraft:furnace",
    "blast_furnace": "minecraft:blast_furnace",
    "smoker": "minecraft:smoker",
    "campfire": "minecraft:campfire",
    "stonecutter": "minecraft:stonecutter",
    "smithing_table": "minecraft:smithing_table",
}

# Ограничение глубины вывода. Самая длинная осмысленная цепочка в игре — от голых рук до
# незеритового снаряжения — укладывается примерно в десяток уровней; запас взят вдвое.
MAX_DEPTH = 20

# Сколько вариантов рецепта или источника рассматривать. Варианты упорядочены по
# оценке стоимости, поэтому лучшие идут первыми, а ограничение держит перебор конечным.
MAX_BRANCHES = 4

# Предел числа раскрытий за один запрос. Ограничения глубины и ветвления недостаточно:
# в предметной области есть узлы с очень плотным графом рецептов — красители получаются
# смешиванием друг друга, и перебор там растёт экспоненциально. Бюджет даёт жёсткую
# верхнюю границу времени ответа, что важнее полноты: ассистент обязан отвечать быстро,
# а на исчерпании бюджета возвращается лучший найденный план.
MAX_EXPANSIONS = 4000

# Заглушка для оценки стоимости: возвращается, когда сработала защита от цикла или
# исчерпана глубина. Значение заведомо больше любой реальной оценки.
COST_GUARD = 50

# Сколько проходов делает приближение стоимостей. Цепочки изготовления в предметной
# области короткие, значения сходятся за единицы проходов; предел страхует от
# патологических случаев.
COST_ITERATIONS = 16

# Веса, по которым сравниваются планы. Это проектное решение, а не физика игры: они задают,
# что считать удобным для игрока. Подобраны так, чтобы план шёл привычным путём развития.
WEIGHT_STEP = 1          # любое действие само по себе
WEIGHT_TOOL_TIER = 2     # за каждый уровень инструмента, нужного для добычи
WEIGHT_HUNT = 20         # охота: существо нужно найти и одолеть, это не то же, что копать
WEIGHT_UNRELIABLE = 30   # выпадение может не случиться, планировать на нём нельзя
# Источник не в обычном Верхнем мире (рукописный слой kb/accessibility.json). Без этого
# чернит из Незера и бамбук из джунглей равноценны булыжнику и доскам, и выбор решает алфавит.
# Незер и Край стоят дорого не из-за расстояния: в Незер ведёт портал из обсидиана, а
# обсидиан добывается алмазной киркой. Пока вес был сопоставим с обычной добычей, золото
# планировалось из незерской руды деревянной киркой — формально дешевле, по сути недостижимо
# раньше алмазной ступени.
WEIGHT_REMOTE = {"deep": 1, "rare_biome": 3, "unnatural": 5, "nether": 13, "end": 16}
UNNATURAL = "unnatural"
# Взять из своего хранилища: дойти до сундука. Дешевле любого изготовления и добычи, но не
# бесплатно, как инвентарь: при равных прочих лучше то, что уже с собой.
WEIGHT_TAKE = 1
# Источник, который игрок уже видел (память мира): искать его не нужно. Скидка мала и
# служит только для выбора между равноценными источниками — железной рудой, которую игрок
# видел, и глубинносланцевой, которую не видел.
WEIGHT_KNOWN = 1


@dataclass(frozen=True)
class Step:
    """Узел плана."""

    kind: str
    item: str
    count: int
    satisfied: bool
    # Чем именно закрывается шаг: станция для изготовления, источник для добычи.
    station: Optional[str] = None
    source: Optional[str] = None
    tool_kind: Optional[str] = None
    tool_tier: Optional[str] = None
    reason: Optional[str] = None
    # Ненадёжный источник: попытка может не дать ничего.
    rare: bool = False
    # Во что обходится сам этот шаг, без учёта вложенных. Складывается при сравнении планов.
    weight: int = 0
    # Группа взаимозаменяемых предметов, из которой выбран предмет шага: тег рецепта либо
    # перечень вариантов через запятую. Рецепту годится любой из них — см. solver/groups.py.
    group: Optional[str] = None
    # Где это взять или добыть по памяти мира: хранилище для «взять», увиденный источник
    # для «добыть». Строку собирает память из координат мода.
    where: Optional[str] = None
    # Рецепт, по которому изготавливается предмет (Recipe.id): по нему мод в режиме
    # «Сделай сам» раскладывает крафт через книгу рецептов.
    recipe: Optional[str] = None
    children: Tuple["Step", ...] = ()

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    @cached_property
    def totals(self) -> Tuple[int, int]:
        """Число тупиков и трудоёмкость поддерева.

        Варианты сравниваются на каждом уровне вывода, и обход поддерева при каждом
        сравнении давал квадратичную работу. Узел неизменяем, поэтому сумма считается один
        раз и складывается из сумм вложенных узлов.
        """
        dead_ends = 1 if self.kind == MISSING else 0
        effort = self.weight
        for child in self.children:
            child_dead_ends, child_effort = child.totals
            dead_ends += child_dead_ends
            effort += child_effort
        return dead_ends, effort


@dataclass(frozen=True)
class Option:
    """Один рассмотренный способ получить предмет."""

    kind: str              # craft | smelt | mine | hunt | ...
    detail: str            # станция изготовления либо источник добычи
    # Из чего изготавливается. Без этого три рецепта железного слитка выглядят одинаково
    # («переплавить в печи») и различие между ними не видно.
    via: Optional[str]
    feasible: bool
    effort: int            # трудоёмкость поддерева целиком
    dead_ends: int         # сколько шагов внутри не удалось вывести
    chosen: bool = False


@dataclass(frozen=True)
class Decision:
    """Выбор между способами получить один предмет.

    Записывается, чтобы на вопрос «почему план именно такой» отвечал сам решатель, а не
    докладчик. Отвергнутые варианты — не отладочный след: они показывают, что выбор был,
    и по какому признаку он сделан.
    """

    item: str
    depth: int
    options: Tuple[Option, ...]

    @property
    def chosen(self) -> Optional[Option]:
        return next((option for option in self.options if option.chosen), None)

    @property
    def rejected(self) -> Tuple[Option, ...]:
        return tuple(option for option in self.options if not option.chosen)


@dataclass
class Plan:
    goal: str
    count: int
    root: Step
    # Только те предметы, где способов получения было больше одного.
    decisions: Tuple[Decision, ...] = ()
    # Что было у игрока до плана. Шаг «есть» бывает и запасом игрока, и остатком
    # предыдущего шага; называются они по-разному (solver/groups.py).
    owned: frozenset = frozenset()

    @property
    def feasible(self) -> bool:
        return self.root.satisfied

    @property
    def missing(self) -> List[str]:
        """Предметы, на которых вывод упёрся."""
        return sorted({step.item for step in self.root.walk() if step.kind == MISSING})

    @property
    def steps(self) -> List[Step]:
        """Все шаги, кроме уже имеющегося в инвентаре, в порядке обхода."""
        return [step for step in self.root.walk() if step.kind != HAVE]

    def taken(self) -> List[Step]:
        """Что взять из хранилищ игрока."""
        return [step for step in self.root.walk() if step.kind == TAKE]

    def actions(self) -> List[Step]:
        """Только то, что игроку предстоит сделать: изготовить, добыть, выбить."""
        return [step for step in self.root.walk() if step.kind in
                (CRAFT, SMELT, STONECUT, SMITH, MINE, HUNT)]

    @property
    def effort(self) -> int:
        """Суммарная трудоёмкость плана по тем же весам, по которым выбирался вариант."""
        return self.root.totals[1]


def _stored(item: str) -> Tuple[str, str]:
    """Ключ запаса хранилищ в общем счётчике.

    Запасы хранилищ лежат в том же счётчике, что и инвентарь, под отдельными ключами: так
    пробные ветки вывода копируют и откатывают их вместе, ничего не зная о хранилищах.
    """
    return (TAKE, item)


def _fork(stock: Counter) -> Counter:
    """Копия запасов для пробной ветки вывода.

    Штатная Counter.copy проходит через проверку типа и подсчёт; пробных веток на один
    план тысячи, и на копиях терялась заметная доля времени вывода.
    """
    trial: Counter = Counter()
    dict.update(trial, stock)
    return trial


class Planner:
    def __init__(self, kb: KnowledgeBase, max_depth: int = MAX_DEPTH,
                 max_expansions: int = MAX_EXPANSIONS) -> None:
        self.kb = kb
        self.max_depth = max_depth
        self.max_expansions = max_expansions
        self._cost_cache: Dict[str, int] = {}
        self._budget = 0
        self._decisions: List[Decision] = []
        self._where: Dict[str, str] = {}
        self._known: Dict[str, str] = {}
        self._excluded: frozenset = frozenset()

    # --- публичный интерфейс ---

    def plan(self, goal: str, count: int = 1,
             inventory: Optional[Dict[str, int]] = None,
             storage: Optional[Dict[str, int]] = None,
             where: Optional[Dict[str, str]] = None,
             known: Optional[Dict[str, str]] = None,
             excluded_sources: Optional[Sequence[str]] = None) -> Plan:
        """План от инвентаря.

        storage — запасы известных хранилищ игрока (память мира), предмет → количество;
        where — где лежит предмет, строкой для шага «взять»; known — источники добычи,
        которые игрок видел, блок → где. Без них план тот же, что и раньше.
        excluded_sources — блоки, из которых добыть не вышло (автопилот исключает источник
        после повторных провалов); план ищет обход.
        """
        stock = Counter(inventory or {})
        for item, have in (storage or {}).items():
            if have and have > 0:
                stock[_stored(item)] = int(have)
        self._where = dict(where or {})
        self._known = dict(known or {})
        self._excluded = frozenset(excluded_sources or ())
        self._budget = self.max_expansions
        self._decisions = []
        root = self._resolve(goal, count, stock, frozenset(), 0)
        # В ответе остаются только настоящие развилки: там, где способ был один,
        # объяснять нечего.
        decisions = tuple(decision for decision in self._decisions if len(decision.options) > 1)
        owned = frozenset(item for item, have in (inventory or {}).items() if have and have > 0)
        return Plan(goal=goal, count=count, root=root, decisions=decisions, owned=owned)

    @property
    def budget_exhausted(self) -> bool:
        """Последний запрос упёрся в предел перебора, и план может быть неполным."""
        return self._budget <= 0

    # --- вывод ---

    def _resolve(self, item: str, count: int, stock: Counter,
                 stack: frozenset, depth: int) -> Step:
        if count <= 0:
            return Step(kind=HAVE, item=item, count=0, satisfied=True)

        # Что-то из нужного уже лежит в инвентаре — берём и уменьшаем остаток.
        taken = min(stock.get(item, 0), count)
        if taken:
            stock[item] -= taken
        # Остальное, что найдётся в хранилищах игрока, — взять оттуда.
        stored = min(stock.get(_stored(item), 0), count - taken)
        if stored:
            stock[_stored(item)] -= stored
        remaining = count - taken - stored

        if remaining == 0:
            if not stored:
                return Step(kind=HAVE, item=item, count=taken, satisfied=True)
            take = self._take(item, stored)
            if not taken:
                return take
            return Step(kind=TAKE, item=item, count=count, satisfied=True, weight=0,
                        children=(Step(kind=HAVE, item=item, count=taken, satisfied=True), take))
        if stored:
            taken = (taken, stored)

        if depth >= self.max_depth:
            return self._missing(item, remaining, "превышена глубина вывода", taken)

        self._budget -= 1
        if self._budget <= 0:
            return self._missing(item, remaining, "исчерпан бюджет поиска", taken)

        # Предмет уже раскрывается выше по стеку: дальше идти нельзя, иначе цикл.
        # Такое бывает у обратимых рецептов вроде «блок алмазов -> 9 алмазов».
        if item in stack:
            return self._missing(item, remaining, "циклическая зависимость", taken)

        deeper = stack | {item}

        # Изготовление и добыча рассматриваются как равноправные пути, каждый на своей копии
        # запасов. Иначе неудачная попытка изготовить закрыла бы дорогу к добыче: у алмаза
        # есть рецепт из алмазного блока, который упирается в цикл, но сам алмаз прекрасно
        # добывается из руды.
        # Отметка в журнале развилок: всё записанное дальше относится к этому предмету
        # и к тому, что раскрывается под ним.
        mark = len(self._decisions)

        candidates: List[Tuple[Step, Counter]] = []
        for attempt in (self._try_recipes, self._try_sources):
            trial = _fork(stock)
            step = attempt(item, remaining, trial, deeper, depth)
            if step is not None:
                candidates.append((step, trial))

        if candidates:
            # Оба пути рассматриваются до конца, и берётся лучший, а не первый сработавший.
            # Иначе алмаз «выплавлялся бы из алмазной руды» — формально это рецепт, но он
            # требует сначала добыть саму руду, тогда как прямая добыча даёт алмаз сразу.
            step, trial = max(candidates, key=lambda pair: self._score(pair[0]))
            stock.clear()
            dict.update(stock, trial)
            self._merge_decisions(item, depth, step, mark)
        else:
            step = self._missing(item, remaining, "нет ни рецепта, ни источника добычи", 0)

        return self._merge_taken(item, count, step, taken)

    def _take(self, item: str, count: int) -> Step:
        return Step(kind=TAKE, item=item, count=count, satisfied=True, weight=WEIGHT_TAKE,
                    where=self._where.get(item))

    def _prefix(self, item: str, taken) -> Tuple[Step, ...]:
        """Шаги «есть» и «взять» для части, закрытой инвентарём и хранилищами."""
        have, stored = taken if isinstance(taken, tuple) else (taken, 0)
        steps: List[Step] = []
        if have:
            steps.append(Step(kind=HAVE, item=item, count=have, satisfied=True))
        if stored:
            steps.append(self._take(item, stored))
        return tuple(steps)

    def _merge_taken(self, item: str, count: int, step: Step, taken) -> Step:
        """Часть закрыта инвентарём (и хранилищами), часть выводом — показываем все шаги."""
        prefix = self._prefix(item, taken)
        if not prefix:
            return step
        return Step(kind=step.kind, item=item, count=count, satisfied=step.satisfied,
                    station=step.station, source=step.source, tool_kind=step.tool_kind,
                    tool_tier=step.tool_tier, reason=step.reason, where=step.where,
                    recipe=step.recipe, children=(*prefix, *step.children))

    def _try_recipes(self, item: str, count: int, stock: Counter,
                     stack: frozenset, depth: int) -> Optional[Step]:
        recipes = self.kb.recipes_for(item)
        if not recipes:
            return None

        best: Optional[Tuple[Step, Counter]] = None
        tried: List[Tuple[Step, str, Optional[str]]] = []
        for recipe in sorted(recipes,
                             key=lambda r: self._recipe_cost(r, stack, stock))[:MAX_BRANCHES]:
            # Каждый вариант считается на своей копии запасов: неудачная попытка не должна
            # съедать инвентарь и портить следующие.
            trial = _fork(stock)
            step = self._expand_recipe(recipe, item, count, trial, stack, depth)
            main = recipe.inputs[0].alternatives[0] if recipe.inputs and recipe.inputs[0].alternatives else None
            tried.append((step, recipe.station, main))
            if best is None or self._score(step) > self._score(best[0]):
                best = (step, trial)
        if best is None:
            return None
        self._record(item, depth, tried, best[0])
        stock.clear()
        dict.update(stock, best[1])
        return best[0]

    def _expand_recipe(self, recipe: Recipe, item: str, count: int, stock: Counter,
                       stack: frozenset, depth: int) -> Step:
        runs = ceil(count / recipe.produces_count)
        children: List[Step] = []
        satisfied = True

        for ingredient in recipe.inputs:
            choice = self._choose_alternative(ingredient.alternatives, stock, stack)
            if choice is None:
                children.append(self._missing(
                    ingredient.tag or "?", ingredient.quantity * runs,
                    "ни один из вариантов недоступен", 0))
                satisfied = False
                continue
            child = self._resolve(choice, ingredient.quantity * runs, stock, stack, depth + 1)
            if len(ingredient.alternatives) > 1:
                # Выбран самый доступный вариант, но рецепту подошёл бы любой из группы.
                child = replace(child, group=ingredient.tag
                                or ",".join(sorted(ingredient.alternatives)))
            children.append(child)
            satisfied = satisfied and child.satisfied

        # Рецепт может дать больше, чем нужно: излишек остаётся игроку и пригодится дальше.
        surplus = runs * recipe.produces_count - count
        if satisfied and surplus > 0:
            stock[item] += surplus

        return Step(kind=METHOD_STEPS.get(recipe.method, CRAFT), item=item, count=count,
                    satisfied=satisfied, station=recipe.station, weight=1,
                    recipe=recipe.id, children=tuple(children))

    def _try_sources(self, item: str, count: int, stock: Counter,
                     stack: frozenset, depth: int) -> Optional[Step]:
        sources = self._usable_sources(item)
        if not sources:
            return None

        best: Optional[Tuple[Step, Counter]] = None
        tried: List[Tuple[Step, str, Optional[str]]] = []
        for source in sources[:MAX_BRANCHES]:
            trial = _fork(stock)
            step = self._expand_source(source, item, count, trial, stack, depth)
            tried.append((step, source.id, None))
            if best is None or self._score(step) > self._score(best[0]):
                best = (step, trial)
        if best is None:
            return None
        self._record(item, depth, tried, best[0])
        stock.clear()
        dict.update(stock, best[1])
        return best[0]

    def _usable_sources(self, item: str) -> List[Source]:
        """Источники добычи, на которых можно строить план.

        Блок, который в мире сам не встречается (эндер-сундук, книжная полка, проводка),
        в данных игры связан с выпадающим из него предметом так же, как руда. Добывать его
        неоткуда, поэтому такой источник берётся только тогда, когда другого пути у
        предмета нет вовсе — ни настоящего источника, ни рецепта.
        """
        sources = [source for source in self.kb.sources_of(item)
                   if source.id not in self._excluded]
        natural = [source for source in sources if source.remote != UNNATURAL]
        if natural or self.kb.recipes_for(item):
            return natural
        return list(sources)

    def _expand_source(self, source: Source, item: str, count: int, stock: Counter,
                       stack: frozenset, depth: int) -> Step:
        kind = HUNT if source.kind == "creature" else MINE
        children: List[Step] = []
        satisfied = True

        if source.tool_required and source.tool_kind:
            tool_step = self._ensure_tool(source, stock, stack, depth)
            if tool_step is not None:
                children.append(tool_step)
                satisfied = tool_step.satisfied

        weight = self._source_weight(source)
        where = self._known.get(source.id)
        if where is not None:
            weight = max(WEIGHT_STEP, weight - WEIGHT_KNOWN)
        return Step(kind=kind, item=item, count=count, satisfied=satisfied,
                    source=source.id, tool_kind=source.tool_kind, tool_tier=source.tool_tier,
                    rare=source.rare, weight=weight, where=where,
                    children=tuple(children))

    @staticmethod
    def _source_weight(source: Source) -> int:
        """Во что обходится добыча из источника.

        Единая шкала с оценкой в `_item_cost`: добыча инструментом дороже добычи руками,
        охота дороже добычи, ненадёжное выпадение дороже всего.
        """
        weight = WEIGHT_STEP
        if source.tool_required:
            weight += WEIGHT_TOOL_TIER * (source.tool_tier_rank + 1)
        if source.kind == "creature":
            weight += WEIGHT_HUNT
        if source.rare or source.silk_touch:
            weight += WEIGHT_UNRELIABLE
        if source.remote:
            weight += WEIGHT_REMOTE.get(source.remote, 0)
        return weight

    def _ensure_tool(self, source: Source, stock: Counter,
                     stack: frozenset, depth: int) -> Optional[Step]:
        """Подходящий инструмент уже есть — шага не нужно; иначе планируем самый простой."""
        candidates = self.kb.tools_of_kind(source.tool_kind)
        suitable = [tool for tool in candidates
                    if self.kb.tier_satisfies(tool.tier, source.tool_tier)]
        if not suitable:
            return self._missing(source.tool_kind or "?", 1, "подходящего инструмента не существует", 0)

        # Инструмент не расходуется при добыче, поэтому имеющийся просто засчитывается.
        for tool in suitable:
            if stock.get(tool.id, 0) > 0:
                return Step(kind=HAVE, item=tool.id, count=1, satisfied=True)
        # Подходящий инструмент лежит в хранилище — взять его, а не делать новый.
        for tool in suitable:
            if stock.get(_stored(tool.id), 0) > 0:
                stock[_stored(tool.id)] -= 1
                stock[tool.id] += 1
                return self._take(tool.id, 1)

        # Берём минимальный достаточный уровень, а при равном уровне — самый дешёвый.
        #
        # Порядок важен именно такой. Если сортировать только по стоимости, для добычи алмаза
        # может быть выбрана алмазная кирка: её оценка совпадает с железной, а чтобы её
        # сделать, нужны алмазы — вывод замыкается в цикл и план разваливается. Если же
        # сортировать только по уровню, для добычи чернита выбирается золотая кирка: по
        # уровню она слабее каменной, хотя золото добыть куда сложнее.
        simplest = min(suitable,
                       key=lambda tool: (tool.tier_rank, self._item_cost(tool.id, stack), tool.id))
        step = self._resolve(simplest.id, 1, stock, stack, depth + 1)
        if step.satisfied:
            # Изготовленный инструмент остаётся у игрока: следующая добыча в том же плане
            # его засчитывает. Без этого длинный план делал одну и ту же кирку дважды.
            stock[simplest.id] += 1
        return step

    # --- выбор вариантов ---

    def _choose_alternative(self, alternatives: Sequence[str], stock: Counter,
                            stack: frozenset) -> Optional[str]:
        """Из взаимозаменяемых вариантов берём тот, что дешевле обойдётся игроку.

        Сначала то, что уже лежит в инвентаре, затем то, что проще получить. Полный перебор
        всех вариантов здесь не нужен и вреден: у тега «доски» одиннадцать значений, и перебор
        размножил бы дерево вывода без пользы для игрока.
        """
        if not alternatives:
            return None
        available = [item for item in alternatives
                     if stock.get(item, 0) > 0 or stock.get(_stored(item), 0) > 0]
        # На шаг вперёд: вариант, который изготавливается прямо из инвентаря, лучше того,
        # за которым нужно идти в мир. Иначе при дубовых брёвнах в инвентаре доски
        # планировались бы из берёзы — она дешевле «вообще», но её у игрока нет.
        makeable = [] if available else [item for item in alternatives
                                         if item not in stack and self._makeable(item, stock)]
        pool = (available or makeable
                or [item for item in alternatives if item not in stack] or list(alternatives))
        return min(pool, key=lambda item: (self._item_cost(item, stack), item))

    def _makeable(self, item: str, stock: Counter) -> bool:
        """Есть ли рецепт предмета, все составляющие которого уже лежат в инвентаре."""
        for recipe in self.kb.recipes_for(item):
            if recipe.inputs and all(
                    any(stock.get(alternative, 0) >= ingredient.quantity
                        for alternative in ingredient.alternatives)
                    for ingredient in recipe.inputs):
                return True
        return False

    def _recipe_cost(self, recipe: Recipe, stack: frozenset,
                     stock: Optional[Counter] = None) -> Tuple[float, int, str]:
        """Оценка рецепта: во что обойдётся единица результата.

        Деление на выход существенно. Палки делаются и из двух досок сразу четыре штуки, и из
        двух бамбуков по одной; по числу шагов бамбук выглядит дешевле, но в пересчёте на
        палку он вчетверо дороже.

        Станция, которой у игрока нет, прибавляет свою цену один раз: она не расходуется.
        """
        total = 0
        for ingredient in recipe.inputs:
            if not ingredient.alternatives:
                total += 100
                continue
            total += ingredient.quantity * min(
                self._item_cost(item, stack) for item in ingredient.alternatives)
        station_cost = 0
        station_item = STATION_ITEMS.get(recipe.station)
        if station_item and not (stock and stock.get(station_item, 0) > 0):
            station_cost = self._item_cost(station_item, stack)
        return (total / max(recipe.produces_count, 1) + station_cost,
                len(recipe.inputs), recipe.id)

    def _item_cost(self, item: str, stack: frozenset = frozenset()) -> float:
        """Оценка трудоёмкости предмета: во что он обойдётся от подручных ресурсов.

        Оценка дробная: округление до целых уравнивало доски из берёзы и из акации, и выбор
        между ними снова доставался алфавиту.
        """
        if not self._cost_cache:
            self._compute_costs()
        return self._cost_cache.get(item, COST_GUARD)

    def _compute_costs(self) -> None:
        """Считает оценки для всех предметов сразу, итеративным приближением.

        Рекурсивный расчёт здесь не работает. Предметная область содержит взаимно зависимые
        узлы — красители получаются смешиванием друг друга, — поэтому рекурсия либо
        пересчитывает одно и то же экспоненциально, либо кэширует заглушки защиты от цикла
        и навсегда завышает стоимость.

        Итеративное приближение свободно от обеих бед: начинаем с того, что добывается в
        мире, и на каждом проходе уточняем стоимость всего, что из этого изготавливается,
        пока значения не перестанут меняться. Это обычная релаксация, и на цикл ей всё равно.
        """
        recipes_by_item = self.kb.index_recipes()
        sources_by_item = self.kb.index_sources()

        # Начальное приближение: то, что просто лежит в мире.
        cost: Dict[str, float] = {}
        for item in sources_by_item:
            sources = self._usable_sources(item)
            # Источник может быть не опознан, если у него нет записи в базе знаний:
            # часть лут-таблиц описывает сущности, которых нет среди именованных.
            if sources:
                cost[item] = min(self._source_weight(source) for source in sources)

        for _ in range(COST_ITERATIONS):
            changed = False
            for item, recipes in recipes_by_item.items():
                for recipe in recipes:
                    total = WEIGHT_STEP
                    for ingredient in recipe.inputs:
                        if not ingredient.alternatives:
                            total += COST_GUARD
                            continue
                        cheapest = min(
                            (cost.get(alternative, COST_GUARD)
                             for alternative in ingredient.alternatives),
                            default=COST_GUARD)
                        total += ingredient.quantity * cheapest
                    # Нижняя граница страхует от вырождения оценки в ноль на циклах.
                    value = max(total / max(recipe.produces_count, 1), 0.25)
                    if value < cost.get(item, COST_GUARD):
                        cost[item] = value
                        changed = True
            if not changed:
                break

        self._cost_cache = cost

    # --- журнал развилок ---

    def _record(self, item: str, depth: int, tried: Sequence[Tuple[Step, str, Optional[str]]],
                winner: Step) -> None:
        """Записывает рассмотренные варианты одного способа получения."""
        options = tuple(
            Option(kind=step.kind, detail=detail, via=via, feasible=step.satisfied,
                   effort=step.totals[1],
                   dead_ends=step.totals[0],
                   chosen=step is winner)
            for step, detail, via in tried
        )
        self._decisions.append(Decision(item=item, depth=depth, options=options))

    def _merge_decisions(self, item: str, depth: int, winner: Step, mark: int) -> None:
        """Сводит изготовление и добычу одного предмета в одну развилку.

        Внутри каждого способа лучший вариант уже отмечен, но выбор между способами делается
        позже и здесь: у проигравшего способа отметка снимается. Иначе выходило бы, что
        выбраны оба.
        """
        indexes = [index for index in range(mark, len(self._decisions))
                   if self._decisions[index].item == item
                   and self._decisions[index].depth == depth]
        if not indexes:
            return

        options: List[Option] = []
        for index in indexes:
            for option in self._decisions[index].options:
                # Способ, проигравший сравнение, не может считаться выбранным.
                won = (option.kind in (MINE, HUNT)) == (winner.kind in (MINE, HUNT))
                options.append(option if (option.chosen and won)
                               else Option(kind=option.kind, detail=option.detail,
                                           via=option.via, feasible=option.feasible,
                                           effort=option.effort, dead_ends=option.dead_ends,
                                           chosen=False))

        for index in reversed(indexes):
            del self._decisions[index]
        self._decisions.append(Decision(item=item, depth=depth, options=tuple(options)))

    # --- вспомогательное ---

    def _missing(self, item: str, count: int, reason: str, taken) -> Step:
        prefix = self._prefix(item, taken)
        closed = sum(step.count for step in prefix)
        return Step(kind=MISSING, item=item, count=count + closed, satisfied=False,
                    reason=reason, children=prefix)

    @staticmethod
    def _score(step: Step) -> Tuple[int, int, int]:
        """Насколько хорош вариант плана.

        Сначала выполнимость, затем отсутствие тупиков, затем трудоёмкость. Считать именно
        трудоёмкость, а не число шагов, существенно: палки выпадают из мёртвого куста за один
        шаг, но с шансом получить ноль, и три надёжных шага через доски игроку полезнее.
        """
        dead_ends, effort = step.totals
        return (int(step.satisfied), -dead_ends, -effort)
