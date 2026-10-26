"""Валидатор действий.

Проверяет то, что предложила языковая модель, по правилам базы знаний и выносит вердикт.
Право вето принадлежит валидатору: модель не может его обойти, потому что команду собирает
сервер, а не она.

Проверки идут по принятой политике поведения — никаких тихих исправлений. Запрос понят не
полностью, значит уточнение; некорректен — отказ с объяснением. Обрезание количества по
пределу допускается, но итоговое число обязано прозвучать в ответе.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from kb.graph import KnowledgeBase

# Режимы работы ассистента.
CREATIVE = "creative"    # выдаёт предметы по запросу
SURVIVAL = "survival"    # ничего не выдаёт, ведёт к цели планом и заданиями
MODES = (CREATIVE, SURVIVAL)

# Какие действия доступны в каждом режиме. Режим управляет составом инструментов, а не текстом
# промпта: в выживании модель физически не может предложить выдачу, потому что такого
# инструмента ей не показывают. Валидатор повторяет ту же проверку на своей стороне.
MODE_ACTIONS = {
    CREATIVE: ("give_item", "remove_item", "summon_entity", "plan_goal",
               "create_quest", "claim_quest_reward", "start_campaign", "propose_goal"),
    SURVIVAL: ("remove_item", "plan_goal", "create_quest", "claim_quest_reward",
               "start_campaign", "propose_goal"),
}

# Действие требует, чтобы целью был предмет либо существо.
ACTION_SUBJECT = {
    "give_item": "item",
    "remove_item": "item",
    "summon_entity": "creature",
    "plan_goal": "item",
    "start_campaign": "item",
}

MAX_ITEM_COUNT = 64
MAX_SUMMON_COUNT = 16
ACTION_LIMITS = {"give_item": MAX_ITEM_COUNT, "remove_item": MAX_ITEM_COUNT,
                 "summon_entity": MAX_SUMMON_COUNT, "plan_goal": MAX_ITEM_COUNT,
                 "start_campaign": MAX_ITEM_COUNT}

# Какому запрету соответствует действие.
ACTION_RESTRICTION = {"give_item": "give", "summon_entity": "summon"}

ACT = "act"
CLARIFY = "clarify"
REJECT = "reject"


@dataclass
class Verdict:
    """Результат проверки одного предложенного действия."""

    outcome: str                      # act | clarify | reject
    action: str
    target: Optional[str] = None
    count: int = 1
    # Точный идентификатор для подстановки в игровую команду: у части предметов он содержит
    # NBT-данные и отличается от того, по которому шёл поиск.
    game_id: Optional[str] = None
    reason: Optional[str] = None
    # Количество было уменьшено до предела. Ответ игроку обязан назвать итоговое число.
    clamped: bool = False
    # Цепочка применённых правил — основа объяснения и материал для протокола тестирования.
    explanation: List[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.outcome == ACT


class Validator:
    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb

    def available_actions(self, mode: str) -> Sequence[str]:
        return MODE_ACTIONS.get(mode, MODE_ACTIONS[CREATIVE])

    def validate(self, action: str, target: Optional[str] = None, count: Any = 1,
                 mode: str = CREATIVE) -> Verdict:
        trace: List[str] = []

        if action not in MODE_ACTIONS[CREATIVE]:
            return Verdict(REJECT, action, reason=f"Действие «{action}» не поддерживается.",
                           explanation=trace + [f"действие {action} отсутствует в реестре"])

        # 1. Режим
        if action not in self.available_actions(mode):
            trace.append(f"режим {mode}: действие {action} недоступно")
            return Verdict(REJECT, action, target=target,
                           reason="В режиме выживания я не выдаю предметы — "
                                  "могу построить план или предложить задание.",
                           explanation=trace)
        trace.append(f"режим {mode}: действие {action} разрешено")

        # 2. Цель указана
        if not target or not str(target).strip():
            trace.append("цель действия не указана")
            return Verdict(CLARIFY, action,
                           reason="Не понял, о чём речь. Уточни, что именно нужно.",
                           explanation=trace)

        # 3. Цель известна базе знаний
        if not self.kb.exists(target):
            trace.append(f"{target} отсутствует в базе знаний")
            return Verdict(REJECT, action, target=target,
                           reason="Такого в игре нет.", explanation=trace)
        label = self.kb.label(target) or target
        trace.append(f"{target} найден в базе знаний: «{label}»")

        # 4. Явный запрет из базы знаний.
        # Проверяется раньше рода цели: «запрещено правилом, вот почему» — более содержательный
        # ответ, чем «это нельзя взять в инвентарь», даже когда верны оба.
        restricted_action = ACTION_RESTRICTION.get(action)
        if restricted_action:
            restriction = self.kb.restriction_for(target, restricted_action)
            if restriction is not None:
                trace.append(f"правило запрета ({restriction.category}) применено к «{label}»")
                return Verdict(REJECT, action, target=target, reason=restriction.reason,
                               explanation=trace)
            trace.append(f"запретов на «{label}» нет")

        # 5. Цель подходящего рода
        expected = ACTION_SUBJECT.get(action)
        kinds = self.kb.kinds(target)
        if expected == "creature" and not self._is_creature(kinds):
            trace.append(f"«{label}» не существо, призыв неприменим")
            return Verdict(REJECT, action, target=target,
                           reason=f"«{label}» — это не существо, призвать не получится.",
                           explanation=trace)
        if expected == "item" and not self._is_carryable(kinds):
            trace.append(f"«{label}» не может попасть в инвентарь")
            return Verdict(REJECT, action, target=target,
                           reason=f"«{label}» нельзя взять в инвентарь.", explanation=trace)

        # 6. Количество
        verdict_count, clamped, error = self._normalize_count(count, ACTION_LIMITS.get(action, MAX_ITEM_COUNT))
        if error is not None:
            trace.append(f"количество отклонено: {error}")
            return Verdict(REJECT, action, target=target, reason=error, explanation=trace)
        if clamped:
            trace.append(f"количество уменьшено до предела: {verdict_count}")
        else:
            trace.append(f"количество принято: {verdict_count}")

        return Verdict(ACT, action, target=target, count=verdict_count,
                       game_id=self.kb.game_id(target), clamped=clamped, explanation=trace)

    @staticmethod
    def _is_carryable(kinds: Sequence[str]) -> bool:
        """Может ли сущность оказаться в инвентаре.

        Блок считается таким же носимым, как предмет: почти всякий блок существует и в виде
        предмета, а в локали игры у них общий ключ, поэтому отдельной пометки `item` у блока
        может не быть. Различие, которое здесь важно, — между вещью и существом.
        """
        return "item" in kinds or "block" in kinds

    @staticmethod
    def _is_creature(kinds: Sequence[str]) -> bool:
        return "creature" in kinds

    @staticmethod
    def _normalize_count(raw: Any, maximum: int):
        """Возвращает количество, признак обрезания и текст ошибки.

        Ноль и отрицательное отклоняются, а не превращаются в единицу: подмена была бы тихим
        исправлением, которого игрок не видит.
        """
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 0, False, "Не понял количество — назови число."
        if value <= 0:
            return 0, False, f"Количество должно быть от 1 до {maximum}."
        if value > maximum:
            return maximum, True, None
        return value, False, None
