"""Древо развития: граф зависимостей между ключевыми предметами и состояние игрока на нём.

Древо не нарисовано руками. Рукописный слой `kb/showcase.json` называет только витрину —
какие предметы стоит показать. Всё остальное выводит решатель:

    узлы       витрина плюс всё, через что проходит план каждого её предмета от пустого
               инвентаря: материалы, инструменты добычи, оборудование рецептов;
    рёбра      «входит в состав», «нужен как инструмент добычи», «нужен как оборудование»;
    ступень    сколько уровней кирки нужно пройти, чтобы предмет стал доступен, — из
               требований к инструменту у источников добычи;
    состояние  по текущему инвентарю: есть, получается прямо сейчас, открыт ступенью,
               закрыт до следующей кирки.

Состояние пересчитывается планировщиком при каждом изменении инвентаря, поэтому древо
«загорается» по мере игры без каких-либо отметок о пройденном.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from kb.graph import KnowledgeBase
from solver import groups
from solver.planner import HAVE, HUNT, MINE, MISSING, STATION_ITEMS, Plan, Planner, Step

SHOWCASE_PATH = Path(__file__).resolve().parent.parent / "kb" / "showcase.json"

INGREDIENT = "ingredient"   # входит в состав
TOOL = "tool"               # нужен, чтобы добыть
STATION = "station"         # нужен как оборудование рецепта

HAVE_IT = "have"            # есть в инвентаре
READY = "ready"             # получается сейчас: из инвентаря без добычи либо одной добычей
OPEN = "open"               # ступень позволяет, но сначала нужно что-то добыть
LOCKED = "locked"           # нужна кирка следующей ступени
UNKNOWN = "unknown"         # план не строится

PICKAXE = "pickaxe"


@dataclass
class Node:
    id: str
    label: str
    showcase: bool = False              # назван в витрине, а не попал как промежуточный
    how: str = ""                       # craft | smelt | mine | hunt | smith | ...
    source: Optional[str] = None        # откуда добывается, если добывается
    remote: Optional[str] = None        # nether | end | deep | ... для источника
    tier: int = 0                       # сколько ступеней кирки нужно пройти
    depth: int = 0                      # длина самой длинной цепочки до узла
    group: Optional[str] = None         # группа взаимозаменяемых предметов, за которую стоит узел


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str


@dataclass
class NodeState:
    status: str
    count: int = 0                      # сколько в инвентаре
    steps: int = 0                      # действий до предмета от текущего инвентаря


@dataclass
class Structure:
    nodes: Dict[str, Node] = field(default_factory=dict)
    edges: List[Edge] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)   # витрина, для которой план не строится


class TechTree:
    def __init__(self, kb: KnowledgeBase, planner: Optional[Planner] = None,
                 showcase: Optional[Sequence[str]] = None,
                 base: Optional[Dict[str, int]] = None) -> None:
        self.kb = kb
        # С чем игрок появляется в мире. В Minecraft — ни с чем; в мире, где первая кирка
        # выдаётся при входе, без этого строение древа упёрлось бы в неё.
        self.base: Dict[str, int] = dict(base or {})
        self.planner = planner or Planner(kb)
        if showcase is None:
            showcase = json.loads(SHOWCASE_PATH.read_text(encoding="utf-8")).get("items") or []
        self.showcase: List[str] = [item for item in showcase if kb.exists(item)]
        self._structure: Optional[Structure] = None

    # --- строение ---

    def structure(self) -> Structure:
        """Узлы и рёбра; от инвентаря не зависят и считаются один раз."""
        if self._structure is None:
            self._structure = self._build()
        return self._structure

    def _build(self) -> Structure:
        result = Structure()
        edges: Dict[Tuple[str, str], str] = {}
        queue = list(self.showcase)
        seen_goals = set()
        while queue:
            goal = queue.pop(0)
            if goal in seen_goals:
                continue
            seen_goals.add(goal)
            plan = self.planner.plan(goal, 1, {k: v for k, v in self.base.items() if k != goal})
            if not plan.feasible:
                if goal in self.showcase:
                    result.skipped.append(goal)
                continue
            generic = groups.in_plan(plan)
            for step in plan.root.walk():
                if step.kind == MISSING:
                    continue
                node = result.nodes.get(step.item)
                if node is None:
                    node = result.nodes[step.item] = Node(
                        id=step.item, label=self.kb.label(step.item) or step.item)
                group = generic.get(id(step))
                if group and not node.group and step.item not in self.showcase:
                    # Узел стоит за любой предмет группы: рецептам выше годится каждый.
                    name = groups.label(group) or node.label
                    node.group, node.label = group, name[:1].upper() + name[1:]
                if step.kind == HAVE or node.how:
                    # Способ получения берётся из первого вхождения: дальше предмет уже
                    # «есть» как остаток предыдущих шагов.
                    continue
                node.how = step.kind
                if step.kind in (MINE, HUNT):
                    node.source = step.source
                    node.remote = self.kb.accessibility(step.source) if step.source else None
                kind = TOOL if step.kind in (MINE, HUNT) else INGREDIENT
                for child in step.children:
                    if child.kind != MISSING and child.item != step.item:
                        edges.setdefault((child.item, step.item), kind)
                station = (self.kb.station_item(step.station)
                           or STATION_ITEMS.get(step.station or ""))
                if station and station != step.item:
                    edges.setdefault((station, step.item), STATION)
                    # Оборудование в план шагом не входит, но в древе это узел со своим путём.
                    queue.append(station)
        for item in self.showcase:
            if item in result.nodes:
                result.nodes[item].showcase = True
        result.edges = [Edge(source, target, kind) for (source, target), kind in edges.items()
                        if source in result.nodes and target in result.nodes]
        self._levels(result)
        return result

    def _levels(self, structure: Structure) -> None:
        """Ступень и глубина узлов — по рёбрам, от предметов без зависимостей."""
        incoming: Dict[str, List[Edge]] = {}
        for edge in structure.edges:
            incoming.setdefault(edge.target, []).append(edge)
        tier: Dict[str, int] = {}
        depth: Dict[str, int] = {}

        def visit(item: str, trail: frozenset) -> Tuple[int, int]:
            if item in tier:
                return tier[item], depth[item]
            if item in trail:          # цикл рецептов: слиток ↔ блок
                return 0, 0
            best_tier, best_depth = 0, 0
            for edge in incoming.get(item, ()):
                parent_tier, parent_depth = visit(edge.source, trail | {item})
                gate = parent_tier
                if edge.kind == TOOL:
                    profile = self.kb.tool_profile(edge.source)
                    if profile is not None and profile.kind == PICKAXE:
                        # Кирка уровня N открывает следующую ступень.
                        gate = max(gate, profile.tier_rank + 1)
                best_tier = max(best_tier, gate)
                best_depth = max(best_depth, parent_depth + 1)
            tier[item], depth[item] = best_tier, best_depth
            return best_tier, best_depth

        for item, node in structure.nodes.items():
            node.tier, node.depth = visit(item, frozenset())

    # --- состояние ---

    def state(self, inventory: Optional[Dict[str, int]] = None) -> Dict[str, NodeState]:
        """Состояние каждого узла при данном инвентаре."""
        stock = {item: int(count) for item, count in (inventory or {}).items()
                 if int(count or 0) > 0}
        rank = self.rank(stock)
        states: Dict[str, NodeState] = {}
        for item, node in self.structure().nodes.items():
            count = stock.get(item, 0)
            if node.group:
                count = sum(stock.get(member, 0) for member in groups.members(node.group, self.kb))
            if count > 0:
                states[item] = NodeState(HAVE_IT, count=count)
                continue
            plan = self.planner.plan(item, 1, stock)
            if not plan.feasible:
                states[item] = NodeState(UNKNOWN)
                continue
            steps = len(plan.actions())
            if self._ready(plan, stock):
                status = READY
            elif node.tier <= rank + 1:
                status = OPEN
            else:
                status = LOCKED
            states[item] = NodeState(status, steps=steps)
        return states

    def rank(self, stock: Dict[str, int]) -> int:
        """Уровень лучшей кирки в инвентаре; -1, если кирки нет."""
        owned = [tool.tier_rank for tool in self.kb.tools_of_kind(PICKAXE)
                 if stock.get(tool.id, 0) > 0]
        return max(owned) if owned else -1

    def _ready(self, plan: Plan, stock: Dict[str, int]) -> bool:
        """Получается ли предмет прямо сейчас.

        Либо всё изготавливается из инвентаря без добычи и оборудование на месте, либо это
        одна добыча, для которой инструмент уже в руках.
        """
        actions = plan.actions()
        if not actions:
            return False
        if len(actions) == 1 and actions[0].kind == MINE:
            return True
        if any(step.kind in (MINE, HUNT) for step in actions):
            return False
        for station in {step.station for step in actions if step.station}:
            station_item = self.kb.station_item(station) or STATION_ITEMS.get(station)
            if not station_item or stock.get(station_item, 0) > 0:
                continue
            extra = self.planner.plan(station_item, 1, stock)
            if not extra.feasible or any(s.kind in (MINE, HUNT) for s in extra.actions()):
                return False
        return True

    def plan_for(self, item: str, inventory: Optional[Dict[str, int]] = None) -> Plan:
        stock = {key: int(count) for key, count in (inventory or {}).items()
                 if int(count or 0) > 0}
        return self.planner.plan(item, 1, stock)
