"""Представление плана в читаемом виде.

Дерево нужно и игроку в оверлее, и при отладке, и в пояснительной записке, поэтому вывод
собирается здесь, а не в планировщике.
"""

from __future__ import annotations

from typing import List, Optional

from kb.graph import KnowledgeBase
from solver.planner import (CRAFT, HAVE, HUNT, MINE, MISSING, SMELT, SMITH, STONECUT,
                            Plan, Step)

STATION_RU = {
    "crafting_table": "на верстаке",
    "furnace": "в печи",
    "blast_furnace": "в доменной печи",
    "smoker": "в коптильне",
    "campfire": "на костре",
    "stonecutter": "камнерезом",
    "smithing_table": "на кузнечном столе",
}

TOOL_KIND_RU = {"pickaxe": "кирка", "axe": "топор", "shovel": "лопата", "hoe": "мотыга"}
TIER_RU = {"wood": "деревянная", "gold": "золотая", "stone": "каменная",
           "iron": "железная", "diamond": "алмазная", "netherite": "незеритовая"}


def _describe(step: Step, kb: KnowledgeBase) -> str:
    name = kb.label(step.item) or step.item
    head = f"{name} ×{step.count}"

    if step.kind == HAVE:
        return f"{head} — есть"
    if step.kind in (CRAFT, SMELT, STONECUT, SMITH):
        return f"{head} — {'скрафтить' if step.kind == CRAFT else 'изготовить'} " \
               f"{STATION_RU.get(step.station, step.station or '')}".rstrip()
    if step.kind == MINE:
        source = kb.label(step.source) or step.source
        suffix = ""
        if step.tool_kind:
            tool = TOOL_KIND_RU.get(step.tool_kind, step.tool_kind)
            tier = TIER_RU.get(step.tool_tier or "")
            suffix = f" ({tier + ' ' + tool if tier else tool} и выше)" if tier else f" ({tool})"
        return f"{head} — добыть из «{source}»{suffix}"
    if step.kind == HUNT:
        source = kb.label(step.source) or step.source
        return f"{head} — выбить из «{source}»"
    if step.kind == MISSING:
        return f"{head} — НЕ ПОЛУЧИТЬ: {step.reason}"
    return head


def render_tree(plan: Plan, kb: KnowledgeBase) -> str:
    """Дерево плана с отметками наличия — то, что показывается в оверлее."""
    lines: List[str] = []

    def walk(step: Step, prefix: str, is_last: bool, root: bool = False) -> None:
        if root:
            lines.append(_describe(step, kb))
            child_prefix = ""
        else:
            lines.append(f"{prefix}{'└── ' if is_last else '├── '}{_describe(step, kb)}")
            child_prefix = prefix + ("    " if is_last else "│   ")
        for index, child in enumerate(step.children):
            walk(child, child_prefix, index == len(step.children) - 1)

    walk(plan.root, "", True, root=True)
    return "\n".join(lines)


def render_summary(plan: Plan, kb: KnowledgeBase) -> str:
    """Короткая сводка для игрового чата: помещается в одну-две строки."""
    goal = kb.label(plan.goal) or plan.goal

    if not plan.feasible:
        blocked = ", ".join((kb.label(item) or item) for item in plan.missing[:3])
        return f"{goal} сейчас не получить: не удалось вывести {blocked}."

    actions = plan.actions()
    if not actions:
        return f"{goal} уже есть в инвентаре."

    mined = [step for step in actions if step.kind in (MINE, HUNT)]
    crafted = [step for step in actions if step.kind in (CRAFT, SMELT, STONECUT, SMITH)]

    parts = []
    if mined:
        names = ", ".join(dict.fromkeys((kb.label(s.item) or s.item) for s in mined[:3]))
        parts.append(f"добыть {names}")
    if crafted:
        parts.append(f"изготовить {len(crafted)} предмет(ов)")
    return f"{goal}: " + "; ".join(parts) + f". Всего шагов: {len(actions)}."
