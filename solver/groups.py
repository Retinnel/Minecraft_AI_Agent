"""Группы взаимозаменяемых предметов: как назвать шаг плана, когда подойдёт любой из них.

Рецепт палок принимает любые доски. Планировщик выбирает из группы самый доступный
предмет — ему нужен конкретный путь, — но показывать игроку «берёзовые доски» значит
навязывать выбор, которого игра не требует. Шаг плана помнит, из какой группы взят его
предмет (`Step.group`), а здесь решается, называть ли шаг именем группы.

Правила:
    то, что уже лежит в инвентаре, называется своим именем — оно конкретное;
    группа с названием в слое `kb/groups.json` называется обобщённо;
    узкая группа (берёзовые брёвна) называется именем общей (любое бревно) только под
    шагом, который сам назван обобщённо: под берёзовыми досками брёвна нужны берёзовые.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from kb.graph import KnowledgeBase
from solver.planner import HAVE, Plan, Step

GROUPS_PATH = Path(__file__).resolve().parent.parent / "kb" / "groups.json"


@lru_cache(maxsize=1)
def _layer() -> Dict[str, Dict[str, str]]:
    data = json.loads(GROUPS_PATH.read_text(encoding="utf-8"))
    return {"labels": dict(data.get("labels") or {}), "within": dict(data.get("within") or {})}


def label(group: str) -> Optional[str]:
    """Название группы для игрока или None, если группа в слое не названа."""
    return _layer()["labels"].get(group)


def members(group: str, kb: KnowledgeBase) -> List[str]:
    """Предметы группы: состав тега из онтологии либо перечисленные в ключе варианты."""
    if "," in group or group.startswith("minecraft:"):
        return [item for item in group.split(",") if item]
    return kb.tag_members(group)


def shown_as(step: Step, parent_generic: bool) -> Optional[str]:
    """Группа, именем которой называется шаг, либо None — тогда шаг называется предметом."""
    if step.kind == HAVE or not step.group:
        return None
    layer = _layer()
    if step.group in layer["labels"]:
        return step.group
    wider = layer["within"].get(step.group)
    if parent_generic and wider in layer["labels"]:
        return wider
    return None


def in_plan(plan: Plan) -> Dict[int, str]:
    """Для каждого шага плана, названного обобщённо, — его группа. Ключ — id(шага)."""
    found: Dict[int, str] = {}
    # Предмет, который план уже изготовил под обобщённым именем: его остаток дальше по плану
    # («доски — есть») называется так же, иначе один и тот же предмет в плане менял бы имя.
    made: Dict[str, str] = {}

    def walk(step: Step, parent_generic: bool) -> None:
        group = shown_as(step, parent_generic)
        if group:
            made.setdefault(step.item, group)
        elif step.kind == HAVE and step.item in made and step.item not in plan.owned:
            # Остаток прежнего шага, а не запас игрока: запас называется своим именем.
            group = made[step.item]
        if group:
            found[id(step)] = group
        for child in step.children:
            walk(child, bool(group) and step.kind != HAVE)

    walk(plan.root, False)
    return found
