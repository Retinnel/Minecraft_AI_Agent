"""Проверка выполнимости заданий.

Языковая модель придумывает задание, а решатель доказывает, что оно достижимо из текущего
состояния мира, и отклоняет невыполнимое до того, как игрок его увидит. Награда проверяется
теми же правилами, что и обычная выдача.

Это и есть «гарантированно выполнимое задание»: гарантию даёт не модель, а вывод по базе
знаний. Проверка сложности нужна, чтобы задания не вырождались ни в «убей одного зомби», ни
в «добудь стопку алмазов».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from typing import Dict, List, Optional

from kb.graph import KnowledgeBase
from solver.planner import Plan, Planner
from solver.validator import ACT, CLARIFY, REJECT, Validator

# Виды целей задания.
COLLECT = "collect"   # собрать предметы
KILL = "kill"         # победить существ
GOAL_KINDS = (COLLECT, KILL)

# Границы соразмерности. Задание короче нижней границы не ощущается достижением, длиннее
# верхней — превращается в самостоятельный проект и обесценивает награду.
MIN_EFFORT = 1
MAX_EFFORT = 24

# На сколько единиц объёма приходится один пункт трудоёмкости. Собрать восемь предметов
# ощутимо тяжелее, чем один, а восемьдесят и восемьдесят восемь — примерно одинаково.
VOLUME_DIVISOR = 8


@dataclass
class QuestVerdict:
    outcome: str                      # act | clarify | reject
    goal_kind: Optional[str] = None
    target: Optional[str] = None
    goal_count: int = 1
    reward: Optional[str] = None
    reward_count: int = 1
    reward_game_id: Optional[str] = None
    reason: Optional[str] = None
    # Во что обойдётся выполнение: вес плана с поправкой на объём для сбора, число существ
    # для победы.
    effort: int = 0
    explanation: List[str] = field(default_factory=list)
    plan: Optional[Plan] = None

    @property
    def accepted(self) -> bool:
        return self.outcome == ACT


class QuestChecker:
    def __init__(self, kb: KnowledgeBase, planner: Optional[Planner] = None,
                 validator: Optional[Validator] = None) -> None:
        self.kb = kb
        self.planner = planner or Planner(kb)
        self.validator = validator or Validator(kb)

    def check(self, goal_kind: str, target: str, goal_count: int,
              reward: str, reward_count: int = 1,
              inventory: Optional[Dict[str, int]] = None,
              mode: str = "creative") -> QuestVerdict:
        trace: List[str] = []

        if goal_kind not in GOAL_KINDS:
            return QuestVerdict(REJECT, reason=f"Неизвестный вид цели «{goal_kind}».",
                                explanation=[f"вид цели {goal_kind} не поддерживается"])

        # 1. Цель существует и подходит по роду
        if not target or not self.kb.exists(target):
            trace.append(f"цель {target} отсутствует в базе знаний")
            return QuestVerdict(REJECT, goal_kind, target,
                                reason="Цель задания не существует в игре.", explanation=trace)
        label = self.kb.label(target) or target
        kinds = self.kb.kinds(target)

        if goal_kind == KILL and not Validator._is_creature(kinds):
            trace.append(f"«{label}» не существо, победить его нельзя")
            return QuestVerdict(REJECT, goal_kind, target,
                                reason=f"«{label}» — не существо, такое задание не составить.",
                                explanation=trace)
        if goal_kind == COLLECT and (Validator._is_creature(kinds)
                                     or not Validator._is_carryable(kinds)):
            trace.append(f"«{label}» нельзя положить в инвентарь")
            return QuestVerdict(REJECT, goal_kind, target,
                                reason=f"«{label}» нельзя собрать.", explanation=trace)
        trace.append(f"цель задания: «{label}» ({goal_kind})")

        if goal_count < 1:
            trace.append("количество цели меньше единицы")
            return QuestVerdict(REJECT, goal_kind, target,
                                reason="Количество в задании должно быть положительным.",
                                explanation=trace)

        # 2. Награда проходит обычные правила выдачи
        reward_verdict = self.validator.validate("give_item", reward, reward_count, mode="creative")
        if not reward_verdict.allowed:
            trace.extend(reward_verdict.explanation)
            trace.append("награда недопустима")
            return QuestVerdict(REJECT, goal_kind, target, goal_count,
                                reward=reward,
                                reason=f"Награда не подходит: {reward_verdict.reason}",
                                explanation=trace)
        trace.append(f"награда допустима: «{self.kb.label(reward) or reward}» "
                     f"×{reward_verdict.count}")

        # 3. Цель достижима
        plan: Optional[Plan] = None
        if goal_kind == COLLECT:
            plan = self.planner.plan(target, goal_count, inventory or {})
            if not plan.feasible:
                blocked = ", ".join((self.kb.label(item) or item) for item in plan.missing[:3])
                trace.append(f"план сбора не выводится, упёрся в {blocked}")
                return QuestVerdict(REJECT, goal_kind, target, goal_count, reward,
                                    reward_verdict.count,
                                    reason=f"«{label}» сейчас не достать, задание невыполнимо.",
                                    explanation=trace, plan=plan)
            # Трудоёмкость складывается из сложности пути и запрошенного объёма: собрать
            # десять руд тяжелее, чем одну, хотя действие в плане одно и то же.
            effort = plan.effort + ceil(goal_count / VOLUME_DIVISOR)
            trace.append(f"план сбора построен: действий {len(plan.actions())}, "
                         f"трудоёмкость {effort}")
        else:
            restriction = self.kb.restriction_for(target, "summon")
            if restriction is not None and restriction.category == "boss":
                trace.append(f"«{label}» относится к боссам")
                return QuestVerdict(REJECT, goal_kind, target, goal_count, reward,
                                    reward_verdict.count,
                                    reason=f"Задание на победу над «{label}» слишком опасно.",
                                    explanation=trace)
            effort = goal_count
            trace.append(f"цель — победить {goal_count} существ")

        # 4. Соразмерность
        if effort > MAX_EFFORT:
            trace.append(f"трудоёмкость {effort} выше предела {MAX_EFFORT}")
            return QuestVerdict(REJECT, goal_kind, target, goal_count, reward,
                                reward_verdict.count,
                                reason="Задание слишком тяжёлое, давай что-нибудь попроще.",
                                explanation=trace, effort=effort, plan=plan)
        if effort < MIN_EFFORT:
            trace.append(f"трудоёмкость {effort} ниже предела {MIN_EFFORT}")
            return QuestVerdict(REJECT, goal_kind, target, goal_count, reward,
                                reward_verdict.count,
                                reason="Задание слишком простое, оно уже выполнено.",
                                explanation=trace, effort=effort, plan=plan)
        trace.append(f"соразмерность подтверждена: {effort} в пределах "
                     f"{MIN_EFFORT}–{MAX_EFFORT}")

        return QuestVerdict(ACT, goal_kind, target, goal_count, reward, reward_verdict.count,
                            reward_game_id=reward_verdict.game_id, effort=effort,
                            explanation=trace, plan=plan)
