"""Представление плана в читаемом виде.

Дерево нужно и игроку в оверлее, и при отладке, и в пояснительной записке, поэтому вывод
собирается здесь, а не в планировщике.
"""

from __future__ import annotations

from typing import List, Optional

from kb.graph import KnowledgeBase
from solver.planner import (CRAFT, HAVE, HUNT, MINE, MISSING, SMELT, SMITH, STONECUT,
                            Decision, Option, Plan, Step)

STATION_RU = {
    "inventory": "в руках",
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


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Согласование существительного с числом: 1 шаг, 3 шага, 18 шагов."""
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    if count % 10 == 1:
        return one
    if 2 <= count % 10 <= 4:
        return few
    return many


def _inline(label: str) -> str:
    """Название внутри фразы пишется со строчной: «добыть алмаз», а не «добыть Алмаз»."""
    return label[:1].lower() + label[1:] if label else label


def render_summary(plan: Plan, kb: KnowledgeBase) -> str:
    """Короткая сводка для игрового чата: помещается в одну-две строки."""
    goal = kb.label(plan.goal) or plan.goal

    if not plan.feasible:
        blocked = ", ".join(_inline(kb.label(item) or item) for item in plan.missing[:3])
        text = f"{goal} сейчас не получить: не удалось вывести {blocked}."
        # Ближайшие достижимые подцели: составные части цели, которые получить можно.
        # Игроку полезнее знать, с чего начать, чем услышать только «нельзя».
        ready = [step for step in plan.root.children if step.satisfied and step.kind != HAVE]
        if ready:
            names = ", ".join(dict.fromkeys(_inline(kb.label(step.item) or step.item)
                                            for step in ready))
            text += f" Уже сейчас можно получить: {names}."
        return text

    actions = plan.actions()
    if not actions:
        return f"{goal} уже есть в инвентаре."

    mined = [step for step in actions if step.kind in (MINE, HUNT)]
    crafted = [step for step in actions if step.kind in (CRAFT, SMELT, STONECUT, SMITH)]

    parts = []
    if mined:
        names = list(dict.fromkeys(_inline(kb.label(s.item) or s.item) for s in mined))
        listed = ", ".join(names[:3]) + (" и другое" if len(names) > 3 else "")
        parts.append(f"добыть {listed}")
    if crafted:
        parts.append(f"изготовить {len(crafted)} "
                     f"{_plural(len(crafted), 'предмет', 'предмета', 'предметов')}")
    steps = len(actions)
    return (f"{goal}: " + "; ".join(parts)
            + f". Всего {steps} {_plural(steps, 'шаг', 'шага', 'шагов')}.")


KIND_RU = {CRAFT: "изготовить", SMELT: "переплавить", STONECUT: "вырезать",
           SMITH: "выковать", MINE: "добыть", HUNT: "выбить"}


def _describe_option(option: Option, kb: KnowledgeBase) -> str:
    action = KIND_RU.get(option.kind, option.kind)
    if option.kind in (MINE, HUNT):
        where = kb.label(option.detail) or option.detail
        return f"{action} из «{where}»"
    where = STATION_RU.get(option.detail, option.detail)
    source = f" из «{kb.label(option.via) or option.via}»" if option.via else ""
    return f"{action} {where}{source}"


def _why_rejected(option: Option, chosen: Optional[Option]) -> str:
    """Причина отказа от варианта, выраженная в тех же величинах, по которым шёл выбор."""
    if not option.feasible:
        return "недостижимо"
    if chosen is None:
        return "не выбрано"
    if option.dead_ends > chosen.dead_ends:
        return f"тупиков больше: {option.dead_ends} против {chosen.dead_ends}"
    if option.effort > chosen.effort:
        return f"трудоёмкость выше: {option.effort} против {chosen.effort}"
    # Равноценные варианты различает устойчивый порядок перебора, а не оценка.
    # Называть это «трудоёмкость выше» было бы неправдой.
    return f"равноценно по трудоёмкости ({option.effort}), выбран первый по порядку"


def render_alternatives(plan: Plan, kb: KnowledgeBase, limit: int = 4) -> str:
    """Развилки вывода: что ещё рассматривалось и почему выбрано не оно.

    Это ответ решателя на вопрос «почему план именно такой». Веса, по которым идёт
    сравнение, — проектное решение о том, что удобнее игроку, и они названы явно, чтобы
    выбор можно было оспорить по существу, а не принимать на веру.
    """
    if not plan.decisions:
        return "Развилок не было: каждый предмет получается единственным способом."

    # Сначала развилки ближе к цели: они понятнее и интереснее при разборе. Один и тот же
    # предмет раскрывается в нескольких ветвях одинаково, поэтому остаётся первое вхождение.
    ordered: List[Decision] = []
    seen: set = set()
    for decision in sorted(plan.decisions, key=lambda item: (item.depth, item.item)):
        if decision.item not in seen:
            seen.add(decision.item)
            ordered.append(decision)

    lines: List[str] = []
    for decision in ordered[:limit]:
        name = kb.label(decision.item) or decision.item
        chosen = decision.chosen
        lines.append(f"{name}: выбрано — {_describe_option(chosen, kb)} "
                     f"(трудоёмкость {chosen.effort})" if chosen
                     else f"{name}: вариант не выбран")
        for option in decision.rejected:
            lines.append(f"    отвергнуто: {_describe_option(option, kb)} "
                         f"— {_why_rejected(option, chosen)}")

    if len(ordered) > limit:
        lines.append(f"    ещё развилок: {len(ordered) - limit}")
    return "\n".join(lines)
