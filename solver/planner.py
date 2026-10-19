"""Планировщик достижения цели.

Отвечает на вопрос «как получить N штук предмета X, если у игрока сейчас вот это».
Работает обратным выводом от цели: сначала пытается взять из инвентаря, затем изготовить,
затем добыть. Если для добычи нужен инструмент, которого нет, план рекурсивно достраивает
и его.

Планировщик обращается только к словарю ядра онтологии и не знает, что предметная область —
именно Minecraft.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from math import ceil
from typing import Dict, List, Optional, Sequence, Tuple

from kb.graph import KnowledgeBase, Recipe, Source

# Как получен предмет на данном шаге.
HAVE = "have"          # уже в инвентаре
CRAFT = "craft"        # изготовить по рецепту
SMELT = "smelt"        # переплавить
STONECUT = "stonecut"  # вырезать камнерезом
SMITH = "smith"        # выковать
MINE = "mine"          # добыть из блока
HUNT = "hunt"          # выбить из существа
MISSING = "missing"    # получить не удалось

METHOD_STEPS = {"craft": CRAFT, "smelt": SMELT, "stonecut": STONECUT, "smith": SMITH}

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
    children: Tuple["Step", ...] = ()

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


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

    def actions(self) -> List[Step]:
        """Только то, что игроку предстоит сделать: изготовить, добыть, выбить."""
        return [step for step in self.root.walk() if step.kind in
                (CRAFT, SMELT, STONECUT, SMITH, MINE, HUNT)]

    @property
    def effort(self) -> int:
        """Суммарная трудоёмкость плана по тем же весам, по которым выбирался вариант."""
        return sum(step.weight for step in self.root.walk())


class Planner:
    def __init__(self, kb: KnowledgeBase, max_depth: int = MAX_DEPTH,
                 max_expansions: int = MAX_EXPANSIONS) -> None:
        self.kb = kb
        self.max_depth = max_depth
        self.max_expansions = max_expansions
        self._cost_cache: Dict[str, int] = {}
        self._budget = 0
        self._decisions: List[Decision] = []

    # --- публичный интерфейс ---

    def plan(self, goal: str, count: int = 1,
             inventory: Optional[Dict[str, int]] = None) -> Plan:
        stock = Counter(inventory or {})
        self._budget = self.max_expansions
        self._decisions = []
        root = self._resolve(goal, count, stock, frozenset(), 0)
        # В ответе остаются только настоящие развилки: там, где способ был один,
        # объяснять нечего.
        decisions = tuple(decision for decision in self._decisions if len(decision.options) > 1)
        return Plan(goal=goal, count=count, root=root, decisions=decisions)

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
        remaining = count - taken

        if remaining == 0:
            return Step(kind=HAVE, item=item, count=taken, satisfied=True)

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
            trial = stock.copy()
            step = attempt(item, remaining, trial, deeper, depth)
            if step is not None:
                candidates.append((step, trial))

        if candidates:
            # Оба пути рассматриваются до конца, и берётся лучший, а не первый сработавший.
            # Иначе алмаз «выплавлялся бы из алмазной руды» — формально это рецепт, но он
            # требует сначала добыть саму руду, тогда как прямая добыча даёт алмаз сразу.
            step, trial = max(candidates, key=lambda pair: self._score(pair[0]))
            stock.clear()
            stock.update(trial)
            self._merge_decisions(item, depth, step, mark)
        else:
            step = self._missing(item, remaining, "нет ни рецепта, ни источника добычи", 0)

        return self._merge_taken(item, count, step, taken)

    @staticmethod
    def _merge_taken(item: str, count: int, step: Step, taken: int) -> Step:
        """Часть закрыта инвентарём, часть выводом — показываем оба шага."""
        if not taken:
            return step
        return Step(kind=step.kind, item=item, count=count, satisfied=step.satisfied,
                    station=step.station, source=step.source, tool_kind=step.tool_kind,
                    tool_tier=step.tool_tier, reason=step.reason,
                    children=(Step(kind=HAVE, item=item, count=taken, satisfied=True),
                              *step.children))

    def _try_recipes(self, item: str, count: int, stock: Counter,
                     stack: frozenset, depth: int) -> Optional[Step]:
        recipes = self.kb.recipes_for(item)
        if not recipes:
            return None

        best: Optional[Tuple[Step, Counter]] = None
        tried: List[Tuple[Step, str, Optional[str]]] = []
        for recipe in sorted(recipes, key=lambda r: self._recipe_cost(r, stack))[:MAX_BRANCHES]:
            # Каждый вариант считается на своей копии запасов: неудачная попытка не должна
            # съедать инвентарь и портить следующие.
            trial = stock.copy()
            step = self._expand_recipe(recipe, item, count, trial, stack, depth)
            main = recipe.inputs[0].alternatives[0] if recipe.inputs and recipe.inputs[0].alternatives else None
            tried.append((step, recipe.station, main))
            if best is None or self._score(step) > self._score(best[0]):
                best = (step, trial)
        if best is None:
            return None
        self._record(item, depth, tried, best[0])
        stock.clear()
        stock.update(best[1])
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
            children.append(child)
            satisfied = satisfied and child.satisfied

        # Рецепт может дать больше, чем нужно: излишек остаётся игроку и пригодится дальше.
        surplus = runs * recipe.produces_count - count
        if satisfied and surplus > 0:
            stock[item] += surplus

        return Step(kind=METHOD_STEPS.get(recipe.method, CRAFT), item=item, count=count,
                    satisfied=satisfied, station=recipe.station, weight=1,
                    children=tuple(children))

    def _try_sources(self, item: str, count: int, stock: Counter,
                     stack: frozenset, depth: int) -> Optional[Step]:
        sources = self.kb.sources_of(item)
        if not sources:
            return None

        best: Optional[Tuple[Step, Counter]] = None
        tried: List[Tuple[Step, str, Optional[str]]] = []
        for source in sources[:MAX_BRANCHES]:
            trial = stock.copy()
            step = self._expand_source(source, item, count, trial, stack, depth)
            tried.append((step, source.id, None))
            if best is None or self._score(step) > self._score(best[0]):
                best = (step, trial)
        if best is None:
            return None
        self._record(item, depth, tried, best[0])
        stock.clear()
        stock.update(best[1])
        return best[0]

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

        return Step(kind=kind, item=item, count=count, satisfied=satisfied,
                    source=source.id, tool_kind=source.tool_kind, tool_tier=source.tool_tier,
                    rare=source.rare, weight=self._source_weight(source),
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
        if source.rare:
            weight += WEIGHT_UNRELIABLE
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

        # Берём минимальный достаточный уровень, а при равном уровне — самый дешёвый.
        #
        # Порядок важен именно такой. Если сортировать только по стоимости, для добычи алмаза
        # может быть выбрана алмазная кирка: её оценка совпадает с железной, а чтобы её
        # сделать, нужны алмазы — вывод замыкается в цикл и план разваливается. Если же
        # сортировать только по уровню, для добычи чернита выбирается золотая кирка: по
        # уровню она слабее каменной, хотя золото добыть куда сложнее.
        simplest = min(suitable,
                       key=lambda tool: (tool.tier_rank, self._item_cost(tool.id, stack), tool.id))
        return self._resolve(simplest.id, 1, stock, stack, depth + 1)

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
        available = [item for item in alternatives if stock.get(item, 0) > 0]
        pool = available or [item for item in alternatives if item not in stack] or list(alternatives)
        return min(pool, key=lambda item: (self._item_cost(item, stack), item))

    def _recipe_cost(self, recipe: Recipe, stack: frozenset) -> Tuple[float, int, str]:
        """Оценка рецепта: во что обойдётся единица результата.

        Деление на выход существенно. Палки делаются и из двух досок сразу четыре штуки, и из
        двух бамбуков по одной; по числу шагов бамбук выглядит дешевле, но в пересчёте на
        палку он вчетверо дороже.
        """
        total = 0
        for ingredient in recipe.inputs:
            if not ingredient.alternatives:
                total += 100
                continue
            total += ingredient.quantity * min(
                self._item_cost(item, stack) for item in ingredient.alternatives)
        return (total / max(recipe.produces_count, 1), len(recipe.inputs), recipe.id)

    def _item_cost(self, item: str, stack: frozenset = frozenset()) -> int:
        """Оценка трудоёмкости предмета: во что он обойдётся от подручных ресурсов."""
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
        cost: Dict[str, int] = {}
        for item, sources in sources_by_item.items():
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
                    value = max(int(total / max(recipe.produces_count, 1)), 1)
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
                   effort=sum(node.weight for node in step.walk()),
                   dead_ends=sum(1 for node in step.walk() if node.kind == MISSING),
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

    @staticmethod
    def _missing(item: str, count: int, reason: str, taken: int) -> Step:
        children = (Step(kind=HAVE, item=item, count=taken, satisfied=True),) if taken else ()
        return Step(kind=MISSING, item=item, count=count + taken, satisfied=False,
                    reason=reason, children=children)

    @staticmethod
    def _score(step: Step) -> Tuple[int, int, int]:
        """Насколько хорош вариант плана.

        Сначала выполнимость, затем отсутствие тупиков, затем трудоёмкость. Считать именно
        трудоёмкость, а не число шагов, существенно: палки выпадают из мёртвого куста за один
        шаг, но с шансом получить ноль, и три надёжных шага через доски игроку полезнее.
        """
        nodes = list(step.walk())
        dead_ends = sum(1 for node in nodes if node.kind == MISSING)
        effort = sum(node.weight for node in nodes)
        return (int(step.satisfied), -dead_ends, -effort)
