"""Разбор смерти: от чего игрок погиб, что от этого защищает и что из этого было достижимо.

Причина смерти приходит из игры строкой-идентификатором источника урона. Рукописный слой
`kb/threats.json` сводит причины к классам угроз и сопоставляет классам меры защиты. Дальше
работает решатель: для каждой меры он устанавливает одно из трёх —

    надето       мера уже была в деле (броня на игроке, щит в руке);
    не надето    мера лежала в инвентаре, но не использовалась;
    достижимо    меры не было, но она изготавливалась из инвентаря без единой добычи.

Третий случай — вывод, а не справка: строится план от инвентаря на момент смерти, и мера
засчитывается, только если в плане нет шагов добычи и охоты, а нужное оборудование есть либо
так же собирается из инвентаря. Поэтому ассистент говорит не «носи броню», а «из восьми
слитков, что лежали у тебя, выходил нагрудник».

Языковая модель здесь не участвует: ей остаётся пересказать разбор.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kb.graph import KnowledgeBase
from solver.planner import HUNT, MINE, STATION_ITEMS, Plan, Planner

THREATS_PATH = Path(__file__).resolve().parent.parent / "kb" / "threats.json"

WORN = "worn"
UNUSED = "unused"
CRAFTABLE = "craftable"

ARMOR = "armor"
ARMOR_SLOTS = ("chestplate", "leggings", "helmet", "boots")
OTHER = "other"

# Сколько достижимых мер называть: разбор читается в строке чата, не в таблице.
MAX_MISSED = 3


@dataclass(frozen=True)
class Measure:
    """Одна мера защиты и то, как с ней обстояло дело в момент смерти."""

    item: str
    group: str                 # armor | shield | weapon | water | food
    status: str                # worn | unused | craftable
    points: int = 0            # очки брони; для прочих мер ноль
    steps: int = 0             # действий изготовления, если мера была достижима
    plan: Optional[Plan] = None


@dataclass
class PostMortem:
    cause: str
    threat: str
    threat_label: str
    worn: List[Measure] = field(default_factory=list)
    unused: List[Measure] = field(default_factory=list)
    craftable: List[Measure] = field(default_factory=list)
    # Что стоит добыть к следующему разу, если из инвентаря ничего не выходило.
    next_goal: Optional[str] = None
    next_plan: Optional[Plan] = None
    explanation: List[str] = field(default_factory=list)

    @property
    def preventable(self) -> bool:
        """Была ли защита под рукой: лежала в инвентаре или собиралась из него."""
        return bool(self.unused or self.craftable)

    @property
    def protectable(self) -> bool:
        """Защищает ли от этой угрозы снаряжение вообще."""
        return bool(self.worn or self.unused or self.craftable or self.next_goal)


class DeathAnalyzer:
    def __init__(self, kb: KnowledgeBase, planner: Optional[Planner] = None,
                 layer: Optional[Dict[str, Any]] = None) -> None:
        self.kb = kb
        self.planner = planner or Planner(kb)
        self.layer = layer if layer is not None else json.loads(
            THREATS_PATH.read_text(encoding="utf-8"))
        self.armor_points: Dict[str, int] = dict(self.layer.get("armor_points") or {})

    # --- публичный интерфейс ---

    def analyze(self, cause: str, inventory: Optional[Dict[str, int]] = None,
                armor: Sequence[str] = (), held: Sequence[str] = ()) -> PostMortem:
        """Разбор по причине смерти и состоянию игрока на её момент.

        inventory — количества предметов, включая надетое; armor — надетая броня;
        held — то, что было в руках (вторая рука и панель быстрого доступа).
        """
        stock = Counter({item: int(count) for item, count in (inventory or {}).items()
                         if int(count or 0) > 0})
        threat = self._threat(cause)
        spec = (self.layer.get("threats") or {}).get(threat) or {}
        result = PostMortem(cause=cause, threat=threat,
                            threat_label=spec.get("label") or "неизвестная причина")
        result.explanation.append(f"причина «{cause}» отнесена к угрозе «{result.threat_label}»")

        groups = list(spec.get("measures") or [])
        if not groups:
            result.explanation.append("от этой угрозы снаряжение не защищает")
            return result

        candidates: List[str] = []
        for group in groups:
            if group == ARMOR:
                candidates.extend(self._armor(result, stock, armor))
            else:
                candidates.extend(self._simple(result, group, stock, held))

        result.craftable.sort(key=lambda m: (-m.points, m.steps, m.item))
        del result.craftable[MAX_MISSED:]

        if not result.preventable and not result.worn:
            self._next_goal(result, candidates, stock)
        return result

    # --- угроза ---

    def _threat(self, cause: str) -> str:
        causes = self.layer.get("causes") or {}
        key = str(cause or "").strip()
        return causes.get(key) or causes.get(key.split(".")[0]) or OTHER

    # --- броня ---

    def _armor(self, result: PostMortem, stock: Counter, worn_ids: Sequence[str]) -> List[str]:
        """По каждому слоту: что надето, что лежало лучше надетого, что собиралось."""
        worn_by_slot = {self._slot(item): item for item in worn_ids if self._slot(item)}
        candidates: List[str] = []
        for slot in ARMOR_SLOTS:
            worn = worn_by_slot.get(slot)
            worn_points = self.armor_points.get(worn or "", 0)
            if worn:
                result.worn.append(Measure(worn, ARMOR, WORN, points=worn_points))

            better = sorted((item for item, points in self.armor_points.items()
                             if self._slot(item) == slot and points > worn_points),
                            key=lambda item: (-self.armor_points[item], item))
            candidates.extend(better)

            lying = next((item for item in better if stock.get(item, 0) > 0), None)
            if lying:
                result.unused.append(Measure(lying, ARMOR, UNUSED, points=self.armor_points[lying]))
                result.explanation.append(
                    f"«{self._label(lying)}» лежал в инвентаре, но надет не был")
                continue
            for item in better:
                reach = self._reachable(item, stock)
                if reach is not None:
                    steps, plan = reach
                    result.craftable.append(Measure(item, ARMOR, CRAFTABLE,
                                                    points=self.armor_points[item],
                                                    steps=steps, plan=plan))
                    result.explanation.append(
                        f"«{self._label(item)}» собирался из инвентаря за {steps} действий")
                    break
        if not worn_by_slot:
            result.explanation.append("брони на игроке не было")
        return candidates

    @staticmethod
    def _slot(item: Optional[str]) -> Optional[str]:
        name = str(item or "").rsplit(":", 1)[-1]
        return next((slot for slot in ARMOR_SLOTS if name.endswith("_" + slot)), None)

    # --- прочие меры ---

    def _simple(self, result: PostMortem, group: str, stock: Counter,
                held: Sequence[str]) -> List[str]:
        """Щит, оружие, вода, еда: мера одна, вариантов её несколько, по убыванию пользы."""
        spec = (self.layer.get("measures") or {}).get(group) or {}
        items: List[str] = list(spec.get("items") or [])

        in_hand = next((item for item in items if item in held), None)
        if in_hand:
            result.worn.append(Measure(in_hand, group, WORN))
            return items
        lying = next((item for item in items if stock.get(item, 0) > 0), None)
        if lying:
            result.unused.append(Measure(lying, group, UNUSED))
            result.explanation.append(f"«{self._label(lying)}» был в инвентаре, но не в деле")
            return items
        for item in items:
            reach = self._reachable(item, stock)
            if reach is not None:
                steps, plan = reach
                result.craftable.append(Measure(item, group, CRAFTABLE, steps=steps, plan=plan))
                result.explanation.append(
                    f"«{self._label(item)}» собирался из инвентаря за {steps} действий")
                break
        return items

    # --- вывод ---

    def _reachable(self, item: str, stock: Counter) -> Optional[Tuple[int, Plan]]:
        """Изготавливается ли предмет из инвентаря без добычи; число действий и план."""
        if not self.kb.exists(item):
            return None
        plan = self.planner.plan(item, 1, dict(stock))
        steps = self._crafting_steps(plan)
        if steps is None or steps == 0:
            return None
        # Оборудование рецепта — тоже требование: без верстака нагрудник не собрать.
        for station in {step.station for step in plan.actions() if step.station}:
            station_item = STATION_ITEMS.get(station)
            if not station_item or stock.get(station_item, 0) > 0:
                continue
            extra = self._crafting_steps(self.planner.plan(station_item, 1, dict(stock)))
            if extra is None:
                return None
            steps += extra
        return steps, plan

    @staticmethod
    def _crafting_steps(plan: Plan) -> Optional[int]:
        """Число действий, если план выполним одним изготовлением; иначе None."""
        if not plan.feasible:
            return None
        actions = plan.actions()
        if any(step.kind in (MINE, HUNT) for step in actions):
            return None
        return len(actions)

    def _next_goal(self, result: PostMortem, candidates: Sequence[str], stock: Counter) -> None:
        """Из инвентаря ничего не выходило — называем самую доступную меру на будущее."""
        best: Optional[Tuple[int, str, Plan]] = None
        for item in dict.fromkeys(candidates):
            if not self.kb.exists(item):
                continue
            plan = self.planner.plan(item, 1, dict(stock))
            if plan.feasible and (best is None or plan.effort < best[0]):
                best = (plan.effort, item, plan)
        if best is not None:
            result.next_goal, result.next_plan = best[1], best[2]
            result.explanation.append(
                f"из инвентаря защиту собрать было не из чего; ближайшая — «{self._label(best[1])}»")

    def _label(self, item: str) -> str:
        return self.kb.label(item) or item


def _inline(label: str) -> str:
    return label[:1].lower() + label[1:] if label else label


def render_postmortem(result: PostMortem, kb: KnowledgeBase) -> str:
    """Разбор словами — из тех же фактов, без модели."""
    def name(item: str) -> str:
        return _inline(kb.label(item) or item)

    parts = [f"Причина смерти: {result.threat_label}."]
    if not result.protectable:
        parts.append("От этого снаряжение не защищает.")
        return " ".join(parts)

    armor_worn = [m for m in result.worn if m.group == ARMOR]
    if any(m.group == ARMOR for m in result.unused + result.craftable) or (
            result.next_goal and DeathAnalyzer._slot(result.next_goal)):
        parts.append("Надето было: " + ", ".join(name(m.item) for m in armor_worn) + "."
                     if armor_worn else "Брони на тебе не было.")
    if result.unused:
        parts.append("В инвентаре без дела лежало: "
                     + ", ".join(name(m.item) for m in result.unused) + ".")
    if result.craftable:
        parts.append("Из того, что было в инвентаре, собиралось: " + ", ".join(
            f"{name(m.item)} ({m.steps} {_steps_word(m.steps)})" for m in result.craftable) + ".")
    if not result.preventable:
        if result.next_goal:
            parts.append(f"Из инвентаря защиту собрать было не из чего. К следующему разу "
                         f"добудь: {name(result.next_goal)}.")
        elif result.worn:
            parts.append("Лучшей защиты под рукой не было.")
    return " ".join(parts)


def _steps_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "действие"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "действия"
    return "действий"


def essentials(result: PostMortem, kb: KnowledgeBase) -> List[str]:
    """Что обязано прозвучать в пересказе: главная упущенная мера."""
    best = (result.unused or result.craftable or [None])[0]
    item = best.item if best else result.next_goal
    return [kb.label(item) or item] if item else []


def focus_plan(result: PostMortem) -> Optional[Plan]:
    """План для оверлея: как собрать главную упущенную меру."""
    if result.craftable:
        return result.craftable[0].plan
    return result.next_plan
