"""Исполнение плана решателя в игре: «Сделай сам».

План по-прежнему строит планировщик; этот модуль превращает его в очередь типизированных
задач для мода и ведёт цикл «задача → отчёт → новый план от фактического инвентаря».
Мод исполняет только задачи из белого списка видов (`TASK_KINDS`): добыть, изготовить,
переплавить, поставить станцию, взять из своего сундука. Команд игры здесь нет вовсе —
ни текста, ни slash-команд.

Следующая задача — первый лист нового плана в порядке зависимостей (обход «дети раньше
родителя»): сначала инструмент, потом добыча, потом изготовление. Станция, которой в плане
нет отдельным шагом (верстак, печь), ставится перед первым крафтом на ней, а если её нет
и в инвентаре — сначала изготавливается. Топливо для печи считается здесь же.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import ceil
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from kb.graph import KnowledgeBase
from solver import groups
from solver.planner import (CRAFT, HAVE, HUNT, MINE, MISSING, SMELT, SMITH, STONECUT, TAKE,
                            Plan, Planner, Step)

# Виды задач мода. Неизвестный вид мод отклоняет, сервер другого не выдаёт.
TASK_MINE = "mine"
TASK_CRAFT = "craft"
TASK_SMELT = "smelt"
TASK_PLACE = "place_station"
TASK_TAKE = "take_from_container"
TASK_KINDS = (TASK_MINE, TASK_CRAFT, TASK_SMELT, TASK_PLACE, TASK_TAKE)

# Станции v1: крафт в руках, верстак, печь. Доменная печь, коптильня, камнерез и кузнечный
# стол автопилоту недоступны.
STATIONS = {"inventory": None, "crafting_table": "minecraft:crafting_table",
            "furnace": "minecraft:furnace"}

# Что автопилот не делает (B.3): охота, камнерез, кузнечный стол.
UNSUPPORTED_STEPS = {HUNT: "нужна охота на существ", STONECUT: "нужен камнерез",
                     SMITH: "нужен кузнечный стол"}

# Куда автопилот не ходит: Незер, Край и блоки, которых в мире нет сами по себе.
FORBIDDEN_REMOTE = {"nether": "путь идёт через Незер — автопилот туда не ходит",
                    "end": "путь идёт через Край — автопилот туда не ходит",
                    "unnatural": "нужен блок, который в мире сам не встречается"}

# Руды, которым нужна железная кирка или лучше (алмазы, золото, редстоун, изумруды), лежат
# глубоко, рядом с лавой и пещерами: в v1 автопилот за ними не спускается.
DANGEROUS_TIERS = {"iron": "железной киркой", "diamond": "алмазной киркой"}

# Топливо печи: сколько предметов переплавляет одна штука.
FUEL = (("minecraft:coal", 8.0), ("minecraft:charcoal", 8.0), ("minecraft:coal_block", 80.0))
FUEL_GROUPS = (("minecraft:logs_that_burn", 1.5), ("minecraft:planks", 1.5))
FUEL_LOGS = "minecraft:logs_that_burn"

DEFAULT_TIMEOUTS = {TASK_MINE: 120, TASK_CRAFT: 15, TASK_SMELT: 90, TASK_PLACE: 15, TASK_TAKE: 60}

# Глубина вложенных подцелей: станция для станции (печь требует булыжника, тот — кирки).
MAX_SUBGOAL_DEPTH = 3


@dataclass
class Task:
    """Задача мода — только данные, из которых мод сам выбирает действие."""

    task_id: str
    kind: str
    item: str
    count: int
    blocks: List[str] = field(default_factory=list)   # mine: какие блоки годятся
    recipe_id: Optional[str] = None                    # craft / smelt: рецепт для книги рецептов
    runs: int = 1                                      # craft: сколько раз выполнить рецепт
    station: Optional[str] = None                      # inventory | crafting_table | furnace
    input: Optional[str] = None                        # smelt: что класть в печь
    fuel: Optional[str] = None                         # smelt: чем топить
    fuel_count: int = 0
    position: Optional[Dict[str, Any]] = None          # из памяти мира: руда или сундук
    where: Optional[str] = None                        # то же словами — для отчёта игроку
    timeout_s: int = 60
    goal_step: str = ""                                # для отчёта: что делается и зачем

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Waiting:
    """Добыть самому автопилот не может (нет Baritone): просит игрока принести."""

    item: str
    count: int
    blocks: List[str]
    where: Optional[str] = None


@dataclass
class Context:
    """Что известно о мире в момент выбора задачи."""

    inventory: Dict[str, int]
    stations: Sequence[str] = ()                      # станции в пределах досягаемости
    storage: Dict[str, int] = field(default_factory=dict)
    where: Dict[str, str] = field(default_factory=dict)
    known: Dict[str, str] = field(default_factory=dict)
    excluded: Sequence[str] = ()
    can_mine: bool = True
    # Память мира: где ближайший сундук с предметом и где увиденный блок.
    container_of: Optional[Callable[[str], Optional[Tuple[Dict[str, Any], str]]]] = None
    block_at: Optional[Callable[[Sequence[str]], Optional[Tuple[Dict[str, Any], str]]]] = None


class ExecutionPlanner:
    def __init__(self, kb: KnowledgeBase, planner: Optional[Planner] = None, *,
                 max_effort: int = 30, timeouts: Optional[Dict[str, int]] = None,
                 allowed_kinds: Sequence[str] = TASK_KINDS) -> None:
        self.kb = kb
        self.planner = planner or Planner(kb)
        self.max_effort = max_effort
        self.timeouts = {**DEFAULT_TIMEOUTS, **(timeouts or {})}
        self.allowed_kinds = tuple(kind for kind in allowed_kinds if kind in TASK_KINDS)
        self._counter = 0

    # --- план ---

    def plan(self, goal: str, count: int, context: Context) -> Plan:
        return self.planner.plan(goal, count, context.inventory, storage=context.storage,
                                 where=context.where, known=context.known,
                                 excluded_sources=context.excluded)

    def check(self, plan: Plan, *, effort_limit: bool = True) -> List[str]:
        """Почему этот план автопилоту не по силам; пустой список — можно выполнять.

        Трудоёмкость проверяется только при запуске: по ходу выполнения план короче.
        """
        problems: List[str] = []
        if not plan.feasible:
            missing = ", ".join(self.kb.label(item) or item for item in plan.missing[:3])
            problems.append(f"план не сходится: не получить {missing}")
        for step in plan.root.walk():
            if step.kind in UNSUPPORTED_STEPS:
                label = self.kb.label(step.item) or step.item
                problems.append(f"для «{label}» {UNSUPPORTED_STEPS[step.kind]} — "
                                f"это автопилоту недоступно")
            if step.kind in (CRAFT, SMELT) and (step.station or "inventory") not in STATIONS:
                problems.append(f"нужна станция «{step.station}» — её автопилот не использует")
            if step.kind == MINE and step.source:
                remote = self._remote(step.source)
                if remote in FORBIDDEN_REMOTE:
                    problems.append(FORBIDDEN_REMOTE[remote])
                tier = self._source(step.source, step.item).tool_tier
                if tier in DANGEROUS_TIERS:
                    label = self.kb.label(step.item) or step.item
                    problems.append(f"«{label}» добывается {DANGEROUS_TIERS[tier]} на глубине — "
                                    f"это опасно")
            kind = self._task_kind(step)
            if kind and kind not in self.allowed_kinds:
                problems.append(f"задачи вида «{kind}» выключены в настройках")
        if effort_limit and plan.effort > self.max_effort:
            problems.append(f"это долго: трудоёмкость {plan.effort} при пределе {self.max_effort}")
        return list(dict.fromkeys(problems))

    # --- следующая задача ---

    def next_task(self, goal: str, count: int, context: Context) -> Tuple[Plan, Any]:
        """План от текущего инвентаря и то, что делать дальше.

        Возвращает (план, Task | Waiting | None). None — цель уже в инвентаре.
        """
        plan = self.plan(goal, count, context)
        if context.inventory.get(goal, 0) >= count:
            return plan, None
        return plan, self._next(plan, context, depth=0)

    def _next(self, plan: Plan, context: Context, depth: int):
        if not plan.feasible:
            return None
        step = self._first_leaf(plan.root)
        if step is None:
            return None
        if step.kind == MINE:
            return self._mine(step, context, plan)
        if step.kind == TAKE:
            return self._take(step, context)
        if step.kind in (CRAFT, SMELT):
            station = step.station or "inventory"
            station_item = STATIONS.get(station)
            if station_item and station not in context.stations:
                if context.inventory.get(station_item, 0) > 0:
                    return self._task(TASK_PLACE, station_item, 1, station=station,
                                      goal_step=f"поставить «{self._label(station_item)}»")
                return self._subgoal(station_item, context, depth)
            if step.kind == SMELT:
                return self._smelt(step, context, depth)
            return self._craft(step)
        return None

    def _first_leaf(self, step: Step) -> Optional[Step]:
        """Первый шаг, все вложенные шаги которого уже закрыты, — обход «дети раньше»."""
        for child in step.children:
            found = self._first_leaf(child)
            if found is not None:
                return found
        if step.kind in (HAVE, MISSING):
            return None
        if step.kind == TAKE and step.children:
            return None
        if all(child.kind == HAVE for child in step.children):
            return step
        return None

    def _subgoal(self, item: str, context: Context, depth: int):
        if depth >= MAX_SUBGOAL_DEPTH:
            return None
        plan = self.plan(item, 1, context)
        if plan.root.kind == HAVE:
            return None
        return self._next(plan, context, depth + 1)

    # --- виды задач ---

    def _mine(self, step: Step, context: Context, plan: Optional[Plan] = None):
        blocks = self._blocks(step, context.excluded)
        if not blocks:
            # Все годные блоки исключены после неудач — добывать нечего, запуск остановится.
            return None
        count = step.count
        if plan is not None:
            # Всё, что плану нужно из того же источника и что уже можно добыть, — за один раз.
            count = sum(other.count for other in plan.root.walk()
                        if other.kind == MINE and other.item == step.item
                        and all(child.kind == HAVE for child in other.children)) or step.count
        position, where = None, step.where
        if context.block_at is not None:
            found = context.block_at(blocks)
            if found is not None:
                position, where = found
        if not context.can_mine:
            return Waiting(item=step.item, count=count, blocks=blocks, where=where)
        return self._task(TASK_MINE, step.item, count, blocks=blocks, position=position,
                          where=where, goal_step=f"добыть «{self._label(step.item)}» ×{count}")

    def _take(self, step: Step, context: Context):
        position, where = None, step.where
        if context.container_of is not None:
            found = context.container_of(step.item)
            if found is not None:
                position, where = found
        return self._task(TASK_TAKE, step.item, step.count, position=position, where=where,
                          goal_step=f"взять «{self._label(step.item)}» ×{step.count} из сундука")

    @staticmethod
    def _to_make(step: Step) -> int:
        """Сколько изготовить: часть шага может уже лежать в инвентаре (шаг «есть» внутри)."""
        have = sum(child.count for child in step.children
                   if child.kind == HAVE and child.item == step.item)
        return max(1, step.count - have)

    def _craft(self, step: Step) -> Task:
        recipe = next((candidate for candidate in self.kb.recipes_for(step.item)
                       if candidate.id == step.recipe), None)
        produces = recipe.produces_count if recipe else 1
        count = self._to_make(step)
        return self._task(TASK_CRAFT, step.item, count, recipe_id=_namespaced(step.recipe),
                          runs=max(1, ceil(count / max(1, produces))),
                          station=step.station or "inventory",
                          goal_step=f"изготовить «{self._label(step.item)}» ×{count}")

    def _smelt(self, step: Step, context: Context, depth: int):
        ingredient = next((child.item for child in step.children
                           if child.kind == HAVE and child.item != step.item), None)
        count = self._to_make(step)
        fuel, fuel_count = self._fuel(count, context.inventory, ingredient)
        if fuel is None:
            logs = self.kb.tag_members(FUEL_LOGS)
            need = ceil(count / 1.5)
            blocks = self._natural_blocks(logs, context.excluded)
            if not context.can_mine:
                return Waiting(item="minecraft:oak_log", count=need, blocks=blocks)
            return self._task(TASK_MINE, "minecraft:oak_log", need, blocks=blocks,
                              goal_step=f"добыть брёвна на топливо ×{need}")
        return self._task(TASK_SMELT, step.item, count, recipe_id=_namespaced(step.recipe),
                          station="furnace", input=ingredient, fuel=fuel, fuel_count=fuel_count,
                          goal_step=f"переплавить «{self._label(step.item)}» ×{count}")

    def _fuel(self, amount: int, inventory: Dict[str, int],
              ingredient: Optional[str]) -> Tuple[Optional[str], int]:
        """Чем топить: сначала уголь, потом брёвна и доски, которые не идут в переплавку."""
        for item, value in FUEL:
            need = ceil(amount / value)
            if inventory.get(item, 0) >= need and item != ingredient:
                return item, need
        for tag, value in FUEL_GROUPS:
            need = ceil(amount / value)
            for item in sorted(self.kb.tag_members(tag)):
                if item != ingredient and inventory.get(item, 0) >= need:
                    return item, need
        return None, 0

    def _task(self, kind: str, item: str, count: int, **fields: Any) -> Task:
        self._counter += 1
        return Task(task_id=f"t{self._counter}", kind=kind, item=item, count=max(1, int(count)),
                    timeout_s=int(self.timeouts.get(kind, 60)), **fields)

    # --- блоки для добычи ---

    def _blocks(self, step: Step, excluded: Sequence[str]) -> List[str]:
        """Какие блоки годятся: источник шага и равноценные ему, для группы — всей группы."""
        group = step.group
        if group:
            # Брёвна берёзы для берёзовых досок — частный случай: годится любое дерево рядом,
            # после пересчёта план сам выберет доски из того, что добыто.
            short = group.split(":", 1)[-1]
            wider = groups._layer()["within"].get(short)
            group = wider or group
        items = groups.members(group, self.kb) if group else [step.item]
        candidates: List[str] = []
        for item in items or [step.item]:
            for source in self.kb.sources_of(item):
                if source.kind == "creature" or source.rare:
                    continue
                if step.source and source.tool_kind != self._source(step.source, item).tool_kind:
                    continue
                candidates.append(source.id)
        if step.source:
            candidates = [step.source] + [block for block in candidates if block != step.source]
        blocks = self._natural_blocks(candidates, excluded, already_blocks=True)
        # Глубинные разновидности (глубинный сланец) — только если других нет: незачем копать вниз.
        shallow = [block for block in blocks if self._remote(block) != "deep"]
        return shallow or blocks

    def _natural_blocks(self, items: Sequence[str], excluded: Sequence[str], *,
                        already_blocks: bool = False) -> List[str]:
        result: List[str] = []
        for item in items:
            sources = [item] if already_blocks else [
                source.id for source in self.kb.sources_of(item) if source.kind != "creature"]
            for block in sources:
                # Обтёсанные брёвна в мире сами не растут, а «заражённый» камень — гнездо чешуйниц.
                if block in excluded or "stripped" in block or "infested" in block:
                    continue
                if self._remote(block) in FORBIDDEN_REMOTE:
                    continue
                if block not in result:
                    result.append(block)
        return result

    def _source(self, block: str, item: str):
        for source in self.kb.sources_of(item):
            if source.id == block:
                return source
        for source in self.kb.sources_of(block):
            if source.id == block:
                return source
        return _NO_SOURCE

    def _remote(self, block: str) -> Optional[str]:
        for source in self.kb.sources_of(block):
            if source.id == block:
                return source.remote
        return None

    def _task_kind(self, step: Step) -> Optional[str]:
        return {MINE: TASK_MINE, CRAFT: TASK_CRAFT, SMELT: TASK_SMELT, TAKE: TASK_TAKE}.get(step.kind)

    def _label(self, item: str) -> str:
        return self.kb.label(item) or item


class _NoSource:
    tool_kind = None


_NO_SOURCE = _NoSource()


def _namespaced(recipe_id: Optional[str]) -> Optional[str]:
    if not recipe_id:
        return None
    return recipe_id if ":" in recipe_id else f"minecraft:{recipe_id}"
