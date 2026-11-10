"""Ступень развития игрока: где он сейчас, что это открыло и что дальше.

Развитие в предметной области идёт по уровням инструмента: добыча блоков следующего
уровня требует кирки предыдущего. Эти требования уже записаны в онтологии, поэтому ступень
не назначается руками, а выводится: по лучшей кирке в инвентаре определяется уровень, по
требованиям источников добычи — что он открыл, планировщиком — путь к следующему.

Модуль отвечает на вопрос «на каком я этапе и что дальше» фактами; языковая модель только
пересказывает их.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from kb.graph import KnowledgeBase, Tool
from solver.planner import Plan, Planner

PICKAXE = "pickaxe"

# Название ступени по уровню кирки. Золото в игре слабее камня и равно дереву, поэтому
# отдельной ступени не образует.
STAGE_LABELS = {-1: "начало пути", 0: "деревянная", 1: "каменная", 2: "железная",
                3: "алмазная", 4: "незеритовая"}

# Сколько открытых ступенью ресурсов называть.
MAX_UNLOCKS = 3


@dataclass
class ProgressReport:
    rank: int                          # -1, если кирки нет
    label: str
    tool: Optional[str] = None         # лучшая кирка в инвентаре
    unlocks: List[str] = field(default_factory=list)       # что добывается этой киркой
    next_tool: Optional[str] = None    # кирка следующей ступени
    next_unlocks: List[str] = field(default_factory=list)  # что она откроет
    next_plan: Optional[Plan] = None   # путь к ней от текущего инвентаря
    explanation: List[str] = field(default_factory=list)

    @property
    def final(self) -> bool:
        return self.next_tool is None


class Progression:
    def __init__(self, kb: KnowledgeBase, planner: Optional[Planner] = None) -> None:
        self.kb = kb
        self.planner = planner or Planner(kb)
        self._by_tier: Optional[Dict[int, List[str]]] = None

    def assess(self, inventory: Optional[Dict[str, int]] = None) -> ProgressReport:
        stock = {item: int(count) for item, count in (inventory or {}).items()
                 if int(count or 0) > 0}
        pickaxes = self.kb.tools_of_kind(PICKAXE)
        owned = [tool for tool in pickaxes if stock.get(tool.id, 0) > 0]
        best: Optional[Tool] = max(owned, key=lambda tool: (tool.tier_rank, tool.id)) if owned else None
        rank = best.tier_rank if best else -1

        report = ProgressReport(rank=rank, label=STAGE_LABELS.get(rank, str(rank)),
                                tool=best.id if best else None)
        report.explanation.append(
            f"лучшая кирка в инвентаре: «{self.kb.label(best.id) or best.id}», уровень {rank}"
            if best else "кирки в инвентаре нет")
        if best:
            report.unlocks = self._unlocked_by(rank)

        # Следующая ступень: кирка уровнем выше. Среди равных по уровню (дерево и золото)
        # берётся та, что проще достаётся.
        upper = [tool for tool in pickaxes if tool.tier_rank == rank + 1]
        if upper:
            plans = [(self.planner.plan(tool.id, 1, stock), tool) for tool in upper]
            feasible = [(plan, tool) for plan, tool in plans if plan.feasible]
            plan, tool = min(feasible or plans, key=lambda pair: (pair[0].effort, pair[1].id))
            report.next_tool, report.next_plan = tool.id, plan
            report.next_unlocks = self._unlocked_by(rank + 1)
            report.explanation.append(
                f"следующая ступень: «{self.kb.label(tool.id) or tool.id}», "
                f"действий до неё {len(plan.actions())}")
        else:
            report.explanation.append("выше этой ступени кирок нет")
        return report

    def _unlocked_by(self, rank: int) -> List[str]:
        """Предметы, для добычи которых нужна кирка ровно этого уровня."""
        if self._by_tier is None:
            craftable = self.kb.index_recipes()
            # Насколько предмет востребован: в скольких рецептах он участвует. Булыжник и
            # уголь нужны всюду, блок мёртвого коралла — нигде, хотя открывает их одна ступень.
            uses: Dict[str, int] = {}
            for recipes in craftable.values():
                for recipe in recipes:
                    for ingredient in recipe.inputs:
                        for alternative in ingredient.alternatives:
                            uses[alternative] = uses.get(alternative, 0) + 1
            by_tier: Dict[int, List[tuple]] = {}
            for item, all_sources in self.kb.index_sources().items():
                # Изделие, из которого предмет выпадает при поломке (обсидиан из сундука
                # Края), источником добычи не считается: его сначала нужно изготовить.
                sources = [source for source in all_sources
                           if source.id == item or not craftable.get(source.id)]
                gated = [source for source in sources
                         if source.tool_required and source.tool_kind == PICKAXE and not source.rare]
                if not gated or len(gated) != len([s for s in sources if not s.rare]):
                    # Предмет добывается и без кирки — ступень его не открывает.
                    continue
                need = min(source.tool_tier_rank for source in gated)
                at_tier = [source for source in gated if source.tool_tier_rank == need]
                # Что называть первым: ресурс, выпадающий из другого блока (сырое железо из
                # руды), важнее блока, который роняет сам себя, а тот — важнее изделия,
                # которое просто ломается киркой (бетон, шалкеровый ящик).
                if any(source.id != item for source in at_tier):
                    kind = 0
                elif not craftable.get(item):
                    kind = 1
                else:
                    kind = 2
                nearest = min(source.remote_rank for source in at_tier)
                by_tier.setdefault(need, []).append(
                    (kind, nearest, -uses.get(item, 0), self.kb.label(item) or item, item))
            self._by_tier = {tier: [entry[-1] for entry in sorted(entries)]
                             for tier, entries in by_tier.items()}
        return self._by_tier.get(rank, [])[:MAX_UNLOCKS]


def _inline(label: str) -> str:
    return label[:1].lower() + label[1:] if label else label


def render_progress(report: ProgressReport, kb: KnowledgeBase) -> str:
    """Отчёт о ступени словами — из тех же фактов, без модели."""
    def names(items: List[str]) -> str:
        return ", ".join(_inline(kb.label(item) or item) for item in items)

    if report.tool:
        parts = [f"Ступень: {report.label} — у тебя {_inline(kb.label(report.tool) or report.tool)}."]
        if report.unlocks:
            parts.append(f"Она открыла добычу: {names(report.unlocks)}.")
    else:
        parts = ["Ступень: начало пути — кирки у тебя ещё нет."]

    if report.final:
        parts.append("Выше ступени нет.")
        return " ".join(parts)

    goal = _inline(kb.label(report.next_tool) or report.next_tool)
    steps = len(report.next_plan.actions()) if report.next_plan else 0
    parts.append(f"Дальше — {goal}: до неё {steps} {_steps_word(steps)}.")
    if report.next_unlocks:
        parts.append(f"Она откроет: {names(report.next_unlocks)}.")
    return " ".join(parts)


def _steps_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "действие"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "действия"
    return "действий"
