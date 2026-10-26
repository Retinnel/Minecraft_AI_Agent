"""Кампания: дальняя цель, разложенная на этапы с наградами.

Отвечает на вопрос «как провести игрока к цели по шагам, чтобы каждый шаг был посильным
и доказанно выполнимым». Этапы не придумываются: они вырезаются из дерева плана как вехи —
узлы, открывающие новую возможность (инструмент, станция, сама цель). Выполнимость каждого
этапа доказывается от прогнозируемого инвентаря — того, каким он будет после предыдущих
этапов, — так что доказана вся траектория, а не только конечная цель.

Награды выбираются правилом, а не вкусом: награда не может обесценить путь, то есть не
является целью кампании, целью этапа или предметом, который понадобится дальше.

Языковая модель здесь не участвует. Ей остаётся выбрать цель среди доказанно достижимых,
выбрать награду среди допустимых и пересказать этапы от лица персонажа — всё это за
пределами модуля. Что бы она ни сказала, цепочка этапов от этого не меняется.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from math import ceil
from typing import Deque, Dict, List, Optional, Sequence, Set, Tuple

from kb.graph import KnowledgeBase
from solver.planner import (CRAFT, HAVE, HUNT, MINE, SMELT, SMITH, STATION_ITEMS, STONECUT,
                            Plan, Planner, Step)
from solver.quest import COLLECT, MAX_EFFORT, QuestChecker
from solver.validator import CREATIVE, MAX_ITEM_COUNT, Validator

# Чем этап полезен игроку — почему он стал вехой.
UNLOCKS_TOOL = "tool"            # инструмент: открывает добычу следующего уровня
UNLOCKS_STATION = "station"      # станция: открывает способ изготовления
UNLOCKS_MATERIAL = "material"    # материал: выделен из слишком тяжёлого этапа
UNLOCKS_GOAL = "goal"            # сама цель кампании

ACTION_KINDS = (CRAFT, SMELT, STONECUT, SMITH, MINE, HUNT)

# Сколько раз этап можно дробить. Самая длинная цепочка в игре — до незерита — укладывается
# в несколько дроблений; предел страхует от вырождения в пошаговый пересказ плана.
MAX_SPLITS = 8

# Пул наград: расходники, уместные в выживании и не входящие в прогрессию. Количество —
# на единицу базы, умножается на трудоёмкость этапа. Это проектное решение о балансе,
# такое же, как веса планировщика: оно задаёт, что игроку приятно получить за труд.
REWARD_POOL: Tuple[Tuple[str, int], ...] = (
    ("minecraft:bread", 2),
    ("minecraft:cooked_beef", 2),
    ("minecraft:torch", 4),
    ("minecraft:arrow", 4),
    ("minecraft:apple", 2),
    ("minecraft:baked_potato", 2),
    ("minecraft:cooked_chicken", 2),
)
REWARD_MAX = 16
# Сколько пунктов трудоёмкости удваивают базовое количество награды.
REWARD_EFFORT_STEP = 6

# Кандидаты цели: сколько этапов должно быть в кампании, чтобы она отличалась от обычного
# задания, и сколько — чтобы не превращаться в проект на вечер.
MIN_STAGES = 1
MAX_STAGES = 5
TOOL_KINDS = ("pickaxe", "axe", "shovel", "hoe")
# Станции, которые предлагаются как цель, если их ещё нет.
GOAL_STATIONS = ("minecraft:crafting_table", "minecraft:furnace")


@dataclass(frozen=True)
class RewardOption:
    item: str
    count: int


@dataclass
class Stage:
    """Один этап кампании: задание на сбор с доказанной выполнимостью."""

    index: int                        # с единицы, для реплик «этап 2 из 5»
    target: str
    count: int
    unlocks: str                      # tool | station | material | goal
    # Что именно открывает этап: предмет, ради которого он нужен.
    for_item: Optional[str]
    plan: Plan
    effort: int
    # Откуда цель добывается, если добывается: для «где искать».
    source: Optional[str] = None
    reward: Optional[str] = None
    reward_count: int = 0
    # Все допустимые награды: из них можно выбрать другую, но нельзя выйти за их пределы.
    reward_options: Tuple[RewardOption, ...] = ()
    explanation: List[str] = field(default_factory=list)


@dataclass
class Campaign:
    goal: str
    count: int
    stages: Tuple[Stage, ...] = ()
    feasible: bool = False
    reason: Optional[str] = None
    explanation: List[str] = field(default_factory=list)

    @property
    def effort(self) -> int:
        return sum(stage.effort for stage in self.stages)

    @property
    def targets(self) -> List[str]:
        return [stage.target for stage in self.stages]


@dataclass(frozen=True)
class GoalCandidate:
    """Цель, для которой кампания из текущего состояния доказанно строится."""

    item: str
    kind: str              # tool | station
    stages: int
    effort: int


class CampaignBuilder:
    def __init__(self, kb: KnowledgeBase, planner: Optional[Planner] = None,
                 validator: Optional[Validator] = None,
                 reward_pool: Sequence[Tuple[str, int]] = REWARD_POOL) -> None:
        self.kb = kb
        self.planner = planner or Planner(kb)
        self.validator = validator or Validator(kb)
        self.checker = QuestChecker(kb, self.planner, self.validator)
        self.reward_pool = tuple(reward_pool)

    # --- публичный интерфейс ---

    def build(self, goal: str, count: int = 1,
              inventory: Optional[Dict[str, int]] = None,
              mode: str = CREATIVE) -> Campaign:
        trace: List[str] = []
        label = self.kb.label(goal) or goal
        stock = Counter(inventory or {})

        plan = self.planner.plan(goal, count, stock)
        if not plan.feasible:
            blocked = ", ".join((self.kb.label(item) or item) for item in plan.missing[:3])
            trace.append(f"план цели не выводится, упёрся в {blocked}")
            return Campaign(goal, count, reason=f"«{label}» сейчас не достать: нужно {blocked}.",
                            explanation=trace)

        milestones = self._milestones(plan, stock)
        trace.append("вехи: " + ", ".join(
            f"{self.kb.label(item) or item} ({unlocks})" for item, _, unlocks, _ in milestones))

        stages, failure = self._prove(milestones, stock, mode, trace)
        if failure is not None:
            return Campaign(goal, count, stages=tuple(stages), reason=failure, explanation=trace)

        self._assign_rewards(stages, goal, stock, mode)
        trace.append(f"этапов {len(stages)}, суммарная трудоёмкость "
                     f"{sum(stage.effort for stage in stages)}")
        return Campaign(goal, count, stages=tuple(stages), feasible=True, explanation=trace)

    def propose_goals(self, inventory: Optional[Dict[str, int]] = None,
                      mode: str = CREATIVE) -> List[GoalCandidate]:
        """Цели, до которых из текущего состояния доказанно ведёт кампания.

        По каждому виду инструмента — ступень выше имеющейся; станции — если их нет.
        Среди инструментов одной ступени (деревянная и золотая кирки) остаётся та, что
        дешевле: предлагать новичку золотую кирку — значит вести его не туда.
        """
        stock = Counter(inventory or {})
        candidates: List[GoalCandidate] = []

        for kind in TOOL_KINDS:
            tools = self.kb.tools_of_kind(kind)
            owned = [tool.tier_rank for tool in tools if stock.get(tool.id, 0) > 0]
            next_rank = (max(owned) + 1) if owned else 0
            best: Optional[GoalCandidate] = None
            for tool in tools:
                if tool.tier_rank != next_rank:
                    continue
                candidate = self._candidate(tool.id, UNLOCKS_TOOL, stock, mode)
                if candidate and (best is None or (candidate.stages, candidate.effort)
                                  < (best.stages, best.effort)):
                    best = candidate
            if best is not None:
                candidates.append(best)

        for station in GOAL_STATIONS:
            if stock.get(station, 0) == 0:
                candidate = self._candidate(station, UNLOCKS_STATION, stock, mode)
                if candidate:
                    candidates.append(candidate)

        return sorted(candidates, key=lambda c: (c.stages, c.effort, c.item))

    # --- вехи ---

    def _milestones(self, plan: Plan, stock: Counter
                    ) -> List[Tuple[str, int, str, Optional[str]]]:
        """Вырезает вехи из дерева плана в порядке зависимостей.

        Обход снизу вверх: веха попадает в список после всех вех своего поддерева. Станция
        нужна тому шагу, который на ней выполняется, поэтому ставится перед ним. Один и тот
        же инструмент, понадобившийся в двух ветках, даёт одну веху.
        """
        found: List[Tuple[str, int, str, Optional[str]]] = []
        seen: Set[str] = set()

        def visit(step: Step, parent: Optional[Step]) -> None:
            for child in step.children:
                visit(child, step)
            if step.kind not in ACTION_KINDS:
                return

            station_item = STATION_ITEMS.get(step.station or "")
            if (station_item and station_item not in seen and stock.get(station_item, 0) == 0
                    and self.kb.exists(station_item)):
                seen.add(station_item)
                found.append((station_item, 1, UNLOCKS_STATION, step.item))

            if step is plan.root:
                return
            # Инструмент в дереве появляется только как требование добычи: родитель — шаг
            # добычи, и именно его предмет инструмент открывает.
            if (parent is not None and parent.kind in (MINE, HUNT) and step.item != parent.item
                    and step.item not in seen and self.kb.tool_profile(step.item) is not None):
                seen.add(step.item)
                found.append((step.item, step.count, UNLOCKS_TOOL, parent.item))

        visit(plan.root, None)
        found.append((plan.goal, plan.count, UNLOCKS_GOAL, None))
        return found

    # --- доказательство ---

    def _prove(self, milestones: Sequence[Tuple[str, int, str, Optional[str]]],
               stock: Counter, mode: str, trace: List[str]
               ) -> Tuple[List[Stage], Optional[str]]:
        """Проверяет вехи по очереди от прогнозируемого инвентаря.

        Этап, не уложившийся в трудоёмкость одиночного задания, дробится: самое тяжёлое
        поддерево материала становится отдельной вехой перед ним, и проверка повторяется.
        """
        pending: Deque[Tuple[str, int, str, Optional[str]]] = deque(milestones)
        projected = stock.copy()
        stages: List[Stage] = []
        splits = 0
        queued: Set[str] = {item for item, _, _, _ in milestones}

        while pending:
            item, count, unlocks, for_item = pending[0]
            verdict = self.checker.check(COLLECT, item, count, None, inventory=projected,
                                         mode=mode)
            label = self.kb.label(item) or item

            if not verdict.accepted and verdict.effort > MAX_EFFORT and splits < MAX_SPLITS:
                heavy = self._heaviest_material(verdict.plan, queued)
                if heavy is not None:
                    splits += 1
                    queued.add(heavy.item)
                    pending.appendleft((heavy.item, heavy.count, UNLOCKS_MATERIAL, item))
                    trace.append(f"этап «{label}» слишком тяжёл ({verdict.effort}), "
                                 f"выделен материал «{self.kb.label(heavy.item) or heavy.item}»")
                    continue

            pending.popleft()
            if not verdict.accepted:
                trace.append(f"этап «{label}» отклонён: {verdict.reason}")
                return stages, f"Этап «{label}» не проходит проверку: {verdict.reason}"

            plan = verdict.plan
            stages.append(Stage(
                index=len(stages) + 1,
                target=item,
                count=count,
                unlocks=unlocks,
                for_item=for_item,
                plan=plan,
                effort=verdict.effort,
                source=self._source_of(plan, item),
                explanation=list(verdict.explanation),
            ))
            trace.append(f"этап {len(stages)}: «{label}» ×{count}, трудоёмкость {verdict.effort}")
            self._advance(projected, plan, item, count)

        return stages, None

    @staticmethod
    def _advance(projected: Counter, plan: Plan, item: str, count: int) -> None:
        """Прогноз инвентаря после этапа: цель добавлена, взятое из инвентаря вычтено.

        Излишки рецептов не засчитываются — прогноз занижен намеренно: недооценка даёт
        лишнюю работу на следующем этапе, но не ложное доказательство.
        """
        for consumed, taken in CampaignBuilder._consumed(plan).items():
            projected[consumed] = max(0, projected.get(consumed, 0) - taken)
        projected[item] += count

    @staticmethod
    def _consumed(plan: Plan) -> Counter:
        """Что план взял из инвентаря. Инструмент при добыче не расходуется и не считается."""
        taken: Counter = Counter()

        def visit(step: Step, parent: Optional[Step]) -> None:
            if step.kind == HAVE and step.count > 0:
                is_tool = (parent is not None and parent.kind in (MINE, HUNT)
                           and step.item != parent.item)
                if not is_tool:
                    taken[step.item] += step.count
            for child in step.children:
                visit(child, step)

        visit(plan.root, None)
        return taken

    @staticmethod
    def _heaviest_material(plan: Optional[Plan], queued: Set[str]) -> Optional[Step]:
        """Самое тяжёлое поддерево среди составляющих цели, ещё не ставшее вехой."""
        if plan is None:
            return None
        options = [child for child in plan.root.children
                   if child.kind in ACTION_KINDS and child.item != plan.root.item
                   and child.item not in queued]
        if not options:
            return None
        heaviest = max(options, key=lambda step: (sum(node.weight for node in step.walk()),
                                                  step.item))
        return heaviest if sum(node.weight for node in heaviest.walk()) > 0 else None

    @staticmethod
    def _source_of(plan: Plan, item: str) -> Optional[str]:
        for step in plan.root.walk():
            if step.item == item and step.kind in (MINE, HUNT):
                return step.source
        return None

    # --- награды ---

    def _assign_rewards(self, stages: List[Stage], goal: str, stock: Counter, mode: str) -> None:
        """Допустимые награды каждого этапа по правилу «не обесценивать путь».

        Награда не может быть целью кампании или этапа и не может встречаться в планах
        последующих этапов. Инструмент в награду — не выше того уровня, что у игрока будет
        после этапа. Одна из допустимых назначается по умолчанию; остальные — выбор рассказчика.
        """
        targets = {goal} | {stage.target for stage in stages}
        later_items: List[Set[str]] = []
        following: Set[str] = set()
        for stage in reversed(stages):
            later_items.append(set(following))
            following |= {step.item for step in stage.plan.root.walk()}
        later_items.reverse()

        projected = stock.copy()
        for stage, forbidden_later in zip(stages, later_items):
            self._advance(projected, stage.plan, stage.target, stage.count)
            top_tier = max((tool.tier_rank for tool in self._owned_tools(projected)), default=-1)

            options: List[RewardOption] = []
            for item, base in self.reward_pool:
                if item in targets or item in forbidden_later or not self.kb.exists(item):
                    continue
                profile = self.kb.tool_profile(item)
                if profile is not None and profile.tier_rank > top_tier:
                    continue
                count = min(REWARD_MAX, MAX_ITEM_COUNT,
                            base * max(1, ceil(stage.effort / REWARD_EFFORT_STEP)))
                verdict = self.validator.validate("give_item", item, count, mode=CREATIVE)
                if verdict.allowed:
                    options.append(RewardOption(item, verdict.count))

            stage.reward_options = tuple(options)
            if options:
                # По умолчанию награды чередуются по номеру этапа: шаблонный ответ без
                # модели не должен выдавать хлеб за каждый этап подряд.
                chosen = options[(stage.index - 1) % len(options)]
                stage.reward, stage.reward_count = chosen.item, chosen.count
                stage.explanation.append(
                    f"награда «{self.kb.label(stage.reward) or stage.reward}» ×{stage.reward_count}; "
                    f"допустимых вариантов {len(options)}")
            else:
                stage.explanation.append("допустимой награды не нашлось, этап без награды")

    def _owned_tools(self, stock: Counter):
        for item, count in stock.items():
            if count > 0:
                profile = self.kb.tool_profile(item)
                if profile is not None:
                    yield profile

    # --- кандидаты ---

    def _candidate(self, item: str, kind: str, stock: Counter, mode: str) -> Optional[GoalCandidate]:
        if not self.kb.exists(item) or stock.get(item, 0) > 0:
            return None
        campaign = self.build(item, 1, stock, mode)
        if not campaign.feasible or not MIN_STAGES <= len(campaign.stages) <= MAX_STAGES:
            return None
        return GoalCandidate(item=item, kind=kind, stages=len(campaign.stages),
                             effort=campaign.effort)
