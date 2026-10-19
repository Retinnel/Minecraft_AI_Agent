"""Сквозной путь от реплики игрока до проверенного действия.

Порядок участия компонентов задаёт всю архитектуру, поэтому он вынесен в одно место:

    языковая модель  →  резолвер  →  валидатор  →  сборка команды
    (что хочет игрок)  (о чём речь)  (можно ли)   (что выполнить)

Модель участвует ровно на первом шаге и называет предмет словами игрока, а не
идентификатором. Идентификатор подставляет резолвер по онтологии, допустимость проверяет
валидатор, команду собирает сервер. Поэтому переформулировка запроса не меняет решения:
в самом решении модель не участвует.

Разбор реплики отделён от исполнения: сюда передаётся любая реализация IntentExtractor —
живая модель, локальная модель или заглушка в тестах.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence

from kb.graph import KnowledgeBase
from resolver.lexical import Candidate
from resolver.resolve import AMBIGUOUS, NOT_FOUND, Resolver
from solver.planner import Plan, Planner
from solver.render import render_alternatives, render_summary, render_tree
from solver.validator import ACT, CLARIFY, CREATIVE, REJECT, Validator

NONE = "none"
PLAN_GOAL = "plan_goal"

# Число, названное в реплике буквально, надёжнее числа из ответа модели: модель его
# пересказывает, а регулярное выражение читает. Знак значим — «дай -5 алмазов» должно
# отклоняться, а не молча превращаться в пятёрку.
LITERAL_COUNT = re.compile(r"(?<![\w.])(-?\d+)")

COMMANDS = {
    "give_item": "/give @s {id} {count}",
    "remove_item": "/clear @s {id} {count}",
}


@dataclass
class Proposal:
    """Что языковая модель поняла из реплики."""

    action: str                      # give_item | remove_item | summon_entity | none
    target: Optional[str] = None     # предмет или существо словами игрока, не идентификатор
    count: Any = 1
    raw: Dict[str, Any] = field(default_factory=dict)


class IntentExtractor(Protocol):
    """Источник намерения. Единственное место, где участвует языковая модель."""

    def extract(self, utterance: str) -> Proposal: ...


@dataclass
class Turn:
    """Итог обработки одной реплики со следом каждого компонента."""

    outcome: str                              # act | clarify | reject | none
    action: Optional[str] = None
    target: Optional[str] = None              # идентификатор из онтологии
    label: Optional[str] = None               # человеческое название цели
    count: Any = 1
    command: Optional[str] = None
    reason: Optional[str] = None
    options: Sequence[Candidate] = ()         # варианты для уточняющего вопроса
    explanation: List[str] = field(default_factory=list)
    stages: Dict[str, Any] = field(default_factory=dict)
    # Заполняется только для plan_goal: дерево достижения цели от текущего инвентаря.
    plan: Optional[Plan] = None
    plan_tree: Optional[str] = None
    plan_summary: Optional[str] = None
    # Чем ещё можно было получить цель и почему выбрано не оно.
    plan_alternatives: Optional[str] = None


class Pipeline:
    def __init__(
        self,
        extractor: IntentExtractor,
        kb: Optional[KnowledgeBase] = None,
        *,
        mode: str = CREATIVE,
    ) -> None:
        self.kb = kb or KnowledgeBase.shared()
        self.extractor = extractor
        self.resolver = Resolver(self.kb)
        self.validator = Validator(self.kb)
        self.mode = mode
        self._planner: Optional[Planner] = None

    @property
    def planner(self) -> Planner:
        """Строится по требованию: предварительный расчёт стоимостей занимает секунду."""
        if self._planner is None:
            self._planner = Planner(self.kb)
        return self._planner

    def handle(self, utterance: str, *, mode: Optional[str] = None,
               inventory: Optional[Dict[str, int]] = None) -> Turn:
        mode = mode or self.mode

        proposal = self.extractor.extract(utterance)
        stages: Dict[str, Any] = {
            "llm": {"action": proposal.action, "target": proposal.target, "count": proposal.count}
        }
        if not proposal.action or proposal.action == NONE:
            return Turn(outcome=NONE, stages=stages)

        count = self._reconcile_count(utterance, proposal.count)
        stages["count"] = count

        # Цель не названа — уточняем, а не додумываем. Вопрос формулирует валидатор.
        if not str(proposal.target or "").strip():
            verdict = self.validator.validate(proposal.action, None, count, mode)
            return Turn(outcome=verdict.outcome, action=proposal.action, count=count,
                        reason=verdict.reason, explanation=verdict.explanation, stages=stages)

        resolution = self.resolver.resolve(str(proposal.target), action=proposal.action)
        stages["resolver"] = {
            "status": resolution.status,
            "thing": resolution.thing,
            "confidence": resolution.confidence,
            "candidates": [item.thing for item in resolution.candidates],
        }

        if resolution.status == AMBIGUOUS:
            return Turn(
                outcome=CLARIFY,
                action=proposal.action,
                count=count,
                reason=self._ask_which(resolution.candidates),
                options=resolution.candidates,
                explanation=[f"запрос «{proposal.target}» допускает несколько прочтений"],
                stages=stages,
            )

        if resolution.status == NOT_FOUND:
            return Turn(
                outcome=REJECT,
                action=proposal.action,
                count=count,
                reason="Такого в игре нет.",
                explanation=[f"«{proposal.target}» не сопоставлено ни с одной сущностью базы знаний"],
                stages=stages,
            )

        verdict = self.validator.validate(proposal.action, resolution.thing, count, mode)
        stages["validator"] = {"outcome": verdict.outcome, "clamped": verdict.clamped}

        if verdict.outcome == ACT and verdict.action == PLAN_GOAL:
            return self._build_plan(verdict, inventory, stages)

        command = None
        if verdict.outcome == ACT:
            command = self._command(verdict.action, verdict.game_id or verdict.target, verdict.count)

        return Turn(
            outcome=verdict.outcome,
            action=verdict.action,
            target=verdict.target,
            label=self.kb.label(verdict.target) if verdict.target else None,
            count=verdict.count,
            command=command,
            reason=verdict.reason,
            explanation=verdict.explanation,
            stages=stages,
        )

    def _build_plan(self, verdict, inventory: Optional[Dict[str, int]],
                    stages: Dict[str, Any]) -> Turn:
        """План достижения цели от текущего инвентаря.

        Здесь символьная часть работает уже не арбитром, а исполнителем: ответ целиком
        выводится решателем, а модели остаётся его озвучить. Недостижимая цель даёт не
        отказ, а план с отметкой, на каком предмете вывод упёрся, — игроку важнее знать,
        чего не хватает, чем услышать «нельзя».
        """
        plan = self.planner.plan(verdict.target, verdict.count, inventory)
        stages["planner"] = {
            "feasible": plan.feasible,
            "steps": len(plan.actions()),
            "effort": plan.effort,
            "missing": plan.missing,
            "decisions": len(plan.decisions),
        }
        explanation = list(verdict.explanation)
        explanation.append(
            f"план построен: {len(plan.actions())} действий, трудоёмкость {plan.effort}"
            if plan.feasible else
            f"вывод упёрся на: {', '.join(plan.missing[:3])}"
        )
        return Turn(
            outcome=ACT,
            action=PLAN_GOAL,
            target=verdict.target,
            label=self.kb.label(verdict.target),
            count=verdict.count,
            reason=None if plan.feasible else "Цель недостижима из текущего состояния.",
            explanation=explanation,
            stages=stages,
            plan=plan,
            plan_tree=render_tree(plan, self.kb),
            plan_summary=render_summary(plan, self.kb),
            plan_alternatives=render_alternatives(plan, self.kb),
        )

    # --- вспомогательное ---

    @staticmethod
    def _reconcile_count(utterance: str, proposed: Any) -> Any:
        """Число из реплики важнее числа из ответа модели.

        Модель предлагает, система проверяет: если игрок назвал число цифрами, берётся оно.
        Словесные количества остаются за моделью — цифрами их в тексте нет.
        """
        literal = LITERAL_COUNT.search(utterance or "")
        return int(literal.group(1)) if literal else proposed

    @staticmethod
    def _ask_which(candidates: Sequence[Candidate]) -> str:
        names = ", ".join(candidate.label for candidate in candidates)
        return f"Уточни, что именно нужно: {names}?"

    @staticmethod
    def _command(action: str, game_id: str, count: int) -> str:
        if action == "summon_entity":
            return "\n".join(f"/summon {game_id} ~ ~ ~" for _ in range(max(1, count)))
        template = COMMANDS.get(action)
        return template.format(id=game_id, count=count) if template else ""
