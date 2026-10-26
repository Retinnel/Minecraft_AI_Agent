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
from solver.campaign import Campaign, CampaignBuilder, GoalCandidate
from solver.planner import Plan, Planner
from solver.quest import COLLECT, KILL, QuestChecker
from solver.render import render_alternatives, render_summary, render_tree
from solver.validator import ACT, CLARIFY, CREATIVE, REJECT, Validator

NONE = "none"
PLAN_GOAL = "plan_goal"
CREATE_QUEST = "create_quest"
START_CAMPAIGN = "start_campaign"
PROPOSE_GOAL = "propose_goal"

# Действия, которые может предложить модель. Остальные действия реестра (например, получение
# награды за задание) выполняются системой сами и модели не предъявляются.
PROPOSABLE = ("give_item", "remove_item", "summon_entity", PLAN_GOAL, CREATE_QUEST,
              START_CAMPAIGN, PROPOSE_GOAL)

# Сколько раз модели дают переделать отклонённое задание. Одна попытка — по описанию
# целевой системы: модель учитывает причину отказа, но перебором не занимается.
QUEST_RETRIES = 1

# Число, названное в реплике буквально, надёжнее числа из ответа модели: модель его
# пересказывает, а регулярное выражение читает. Знак значим — «дай -5 алмазов» должно
# отклоняться, а не молча превращаться в пятёрку.
LITERAL_COUNT = re.compile(r"(?<![\w.])(-?\d+)")

COMMANDS = {
    "give_item": "/give @s {id} {count}",
    "remove_item": "/clear @s {id} {count}",
}

# Согласие в ответ на вопрос с единственным вариантом.
AFFIRMATIVE = re.compile(r"^(да|ага|угу|давай|верно|точно|конечно|он самый|она самая|yes|yep)\b",
                         re.IGNORECASE)

# Признаки просьбы что-то убрать. Нужны только для удаления — единственного действия, ошибка
# в котором отнимает у игрока то, что у него было.
REMOVAL_CUES = re.compile(
    r"(забер|забир|убер|убир|удал|выброс|выкин|очист|сотр|clear|remove|delete|take\s)",
    re.IGNORECASE,
)


@dataclass
class Proposal:
    """Что языковая модель поняла из реплики."""

    action: str                      # give_item | remove_item | summon_entity | plan_goal | ...
    target: Optional[str] = None     # предмет или существо словами игрока, не идентификатор
    count: Any = 1
    # Только для create_quest: вид цели и награда, придуманные моделью.
    goal_kind: Optional[str] = None
    reward: Optional[str] = None
    reward_count: Any = 1
    raw: Dict[str, Any] = field(default_factory=dict)


class IntentExtractor(Protocol):
    """Источник намерения. Единственное место на входе конвейера, где участвует модель.

    actions — действия, доступные в текущем режиме: модели предъявляются только они.
    feedback — причина, по которой предыдущее предложение отклонено.
    context — предыдущий обмен репликами одной строкой.
    """

    def extract(self, utterance: str, *, actions: Sequence[str] = PROPOSABLE,
                feedback: Optional[str] = None, context: Optional[str] = None) -> Proposal: ...


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
    query: Optional[str] = None               # название словами игрока — для продолжения диалога
    options: Sequence[Candidate] = ()         # варианты для уточняющего вопроса
    explanation: List[str] = field(default_factory=list)
    stages: Dict[str, Any] = field(default_factory=dict)
    # Заполняется только для plan_goal: дерево достижения цели от текущего инвентаря.
    plan: Optional[Plan] = None
    plan_tree: Optional[str] = None
    plan_summary: Optional[str] = None
    # Чем ещё можно было получить цель и почему выбрано не оно.
    plan_alternatives: Optional[str] = None
    # Заполняется только для create_quest: принятое задание в виде, пригодном для сохранения.
    quest: Optional[Dict[str, Any]] = None
    # Заполняется только для start_campaign: доказанная цепочка этапов.
    campaign: Optional[Campaign] = None
    # Цель выбрана ассистентом из кандидатов решателя, а не названа игроком.
    proposed: bool = False
    candidates: Sequence[GoalCandidate] = ()


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
        self._campaigns: Optional[CampaignBuilder] = None

    @property
    def planner(self) -> Planner:
        """Строится по требованию: предварительный расчёт стоимостей занимает секунду."""
        if self._planner is None:
            self._planner = Planner(self.kb)
        return self._planner

    @property
    def campaigns(self) -> CampaignBuilder:
        if self._campaigns is None:
            self._campaigns = CampaignBuilder(self.kb, self.planner, self.validator)
        return self._campaigns

    def actions_for(self, mode: str) -> Sequence[str]:
        """Действия, которые предъявляются модели в данном режиме."""
        allowed = set(self.validator.available_actions(mode))
        return tuple(action for action in PROPOSABLE if action in allowed)

    def handle(self, utterance: str, *, mode: Optional[str] = None,
               inventory: Optional[Dict[str, int]] = None,
               pending: Optional["Turn"] = None,
               context: Optional[str] = None,
               state: Optional[str] = None) -> Turn:
        """Обрабатывает реплику.

        pending — предыдущий исход, если им был уточняющий вопрос: тогда реплика может
        оказаться ответом на него («алмазный» после «какой меч?»).
        context — предыдущий обмен репликами одной строкой, для продолжений вроде
        «и ещё крипера».
        state — снимок состояния игрока словами, для выбора цели кампании.
        """
        mode = mode or self.mode
        actions = self.actions_for(mode)

        follow_up = self._follow_up(utterance, pending, mode, inventory)
        if follow_up is not None:
            return follow_up

        proposal = self.extractor.extract(utterance, actions=actions, context=context)
        stages: Dict[str, Any] = {
            "llm": {"action": proposal.action, "target": proposal.target, "count": proposal.count}
        }
        if not proposal.action or proposal.action == NONE:
            return Turn(outcome=NONE, stages=stages)

        if proposal.action == CREATE_QUEST and CREATE_QUEST in actions:
            return self._build_quest(utterance, proposal, actions, mode, inventory, stages)

        if proposal.action == PROPOSE_GOAL and PROPOSE_GOAL in actions:
            return self._propose_goal(mode, inventory, state, stages)

        count = self._reconcile_count(utterance, proposal.count)
        stages["count"] = count

        # Удаление необратимо, поэтому одного слова модели для него мало: в реплике должна
        # быть просьба что-то убрать. Без этого модель, не найдя в перечне выдачи, хватает
        # ближайший глагол — и «дай 10 факелов» в выживании превращается в «убираю 10
        # факелов». Сомнение здесь разрешается вопросом, а не действием.
        if proposal.action == "remove_item" and not REMOVAL_CUES.search(utterance or ""):
            return Turn(
                outcome=CLARIFY,
                action="remove_item",
                count=count,
                query=proposal.target,
                reason="Убрать это из инвентаря? Если да, скажи прямо: «убери …».",
                explanation=["удаление предложено моделью, но в реплике нет просьбы что-либо убрать"],
                stages=stages,
            )

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
                query=proposal.target,
                reason=self._ask_which(resolution.candidates),
                options=resolution.candidates,
                explanation=[f"запрос «{proposal.target}» допускает несколько прочтений"],
                stages=stages,
            )

        if resolution.status == NOT_FOUND:
            suggestions = self._suggest(str(proposal.target), proposal.action, stages)
            if suggestions:
                names = ", ".join(f"«{option.label}»" for option in suggestions)
                return Turn(
                    outcome=CLARIFY,
                    action=proposal.action,
                    count=count,
                    query=proposal.target,
                    reason=f"Такого названия я не знаю. Возможно, ты имел в виду {names}?",
                    options=suggestions,
                    explanation=[f"«{proposal.target}» нет в базе знаний; модель предложила "
                                 f"другое название, оно найдено, но без подтверждения не выполняется"],
                    stages=stages,
                )
            return Turn(
                outcome=REJECT,
                action=proposal.action,
                count=count,
                reason="Такого в игре нет.",
                explanation=[f"«{proposal.target}» не сопоставлено ни с одной сущностью базы знаний"],
                stages=stages,
            )

        return self._decide(proposal.action, resolution.thing, count, mode, inventory, stages)

    def _suggest(self, phrase: str, action: str, stages: Dict[str, Any]) -> List[Candidate]:
        """Одна попытка переформулировки, когда резолвер ничего не нашёл.

        Модель знает игру и переводит слова игрока в официальное название; резолвер
        проверяет, что такое название есть в базе знаний. Найденное предлагается вопросом,
        а не выполняется: переформулировка — догадка модели, и подтверждать её должен игрок.
        Векторное сопоставление для этой роли проверялось и отклонено: близость не отделяет
        парафразы от выдуманных названий (eval/reports/2026-10-19-embedding-probe.md).
        """
        rename = getattr(self.extractor, "rename", None)
        if rename is None:
            return []
        try:
            names = rename(phrase)
        except Exception:
            return []
        if isinstance(names, str):
            names = [names]
        stages["rename"] = {"from": phrase, "to": list(names or [])}

        for name in names or []:
            if name.strip().lower() == phrase.strip().lower():
                continue
            resolution = self.resolver.resolve(name, action=action)
            if resolution.ok:
                return [candidate for candidate in resolution.candidates
                        if candidate.thing == resolution.thing][:1]
            if resolution.status == AMBIGUOUS:
                return list(resolution.candidates[:3])
        return []

    def _follow_up(self, utterance: str, pending: Optional["Turn"], mode: str,
                   inventory: Optional[Dict[str, int]]) -> Optional["Turn"]:
        """Ответ на уточняющий вопрос разбирается без модели.

        После «какой именно: алмазный, железный…?» реплика «алмазный» сама по себе ни о чём
        не говорит, но вместе с исходным запросом однозначна. Ответ ищется только среди
        предложенных вариантов: если реплика ни одному не соответствует, это уже новый
        запрос, и он идёт обычным путём.
        """
        if pending is None or pending.outcome != CLARIFY or not pending.options:
            return None
        offered = {option.thing for option in pending.options}

        # «Возможно, ты имел в виду X?» — «да». Согласие относится только к вопросу с одним
        # вариантом: при нескольких «да» ничего не выбирает.
        if len(pending.options) == 1 and AFFIRMATIVE.match((utterance or "").strip()):
            chosen = pending.options[0].thing
            stages = {"follow_up": {"question": pending.reason, "answer": utterance,
                                    "chosen": chosen}}
            return self._decide(pending.action, chosen, pending.count, mode, inventory, stages)

        for query in (f"{utterance} {pending.query or ''}".strip(), utterance):
            resolution = self.resolver.resolve(query, action=pending.action)
            chosen = resolution.thing if resolution.ok else None
            if chosen in offered:
                stages = {"follow_up": {"question": pending.reason, "answer": utterance,
                                        "chosen": chosen}}
                return self._decide(pending.action, chosen, pending.count, mode, inventory, stages)
        return None

    def _decide(self, action: str, thing: str, count: Any, mode: str,
                inventory: Optional[Dict[str, int]], stages: Dict[str, Any]) -> "Turn":
        """Вердикт валидатора и, если действие допустимо, команда или план."""
        verdict = self.validator.validate(action, thing, count, mode)
        stages["validator"] = {"outcome": verdict.outcome, "clamped": verdict.clamped}

        if verdict.outcome == ACT and verdict.action == PLAN_GOAL:
            return self._build_plan(verdict, inventory, stages)
        if verdict.outcome == ACT and verdict.action == START_CAMPAIGN:
            return self._build_campaign(verdict, mode, inventory, stages)

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

    def _build_quest(self, utterance: str, proposal: Proposal, actions: Sequence[str],
                     mode: str, inventory: Optional[Dict[str, int]],
                     stages: Dict[str, Any]) -> Turn:
        """Задание, придуманное моделью, принимается только после доказательства выполнимости.

        Модель предлагает цель и награду; решатель строит план достижения цели из текущего
        состояния, проверяет награду правилами выдачи и оценивает трудоёмкость. Отклонённое
        возвращается модели с причиной один раз. Если и второе предложение не проходит,
        игрок получает отказ, а не невыполнимое задание.
        """
        checker = QuestChecker(self.kb, self.planner, self.validator)
        attempts: List[Dict[str, Any]] = []
        current = proposal
        reason = "Не получилось составить посильное задание."

        for attempt in range(QUEST_RETRIES + 1):
            verdict, reason = self._check_quest(current, checker, inventory, mode)
            attempts.append({"target": current.target, "count": current.count,
                             "reward": current.reward, "accepted": bool(verdict and verdict.accepted),
                             "reason": None if verdict and verdict.accepted else reason})
            stages["quest"] = attempts

            if verdict is not None and verdict.accepted:
                return Turn(
                    outcome=ACT,
                    action=CREATE_QUEST,
                    target=verdict.target,
                    label=self.kb.label(verdict.target),
                    count=verdict.goal_count,
                    explanation=verdict.explanation,
                    stages=stages,
                    plan=verdict.plan,
                    plan_tree=render_tree(verdict.plan, self.kb) if verdict.plan else None,
                    plan_summary=render_summary(verdict.plan, self.kb) if verdict.plan else None,
                    quest={
                        "goal_kind": verdict.goal_kind,
                        "target": verdict.target,
                        "goal_count": verdict.goal_count,
                        "reward": verdict.reward,
                        "reward_label": self.kb.label(verdict.reward) if verdict.reward else None,
                        "reward_count": verdict.reward_count,
                        "reward_game_id": verdict.reward_game_id,
                        "effort": verdict.effort,
                        "attempts": len(attempts),
                    },
                )

            if attempt < QUEST_RETRIES:
                current = self.extractor.extract(utterance, actions=(CREATE_QUEST,), feedback=reason)
                if current.action != CREATE_QUEST:
                    break

        return Turn(outcome=REJECT, action=CREATE_QUEST, reason=reason,
                    explanation=[f"задание отклонено: {reason}"], stages=stages)

    def _check_quest(self, proposal: Proposal, checker: QuestChecker,
                     inventory: Optional[Dict[str, int]], mode: str):
        """Сопоставляет предложенное задание с онтологией и проверяет решателем."""
        goal_kind = (proposal.goal_kind or "").strip().lower()
        if goal_kind not in (COLLECT, KILL):
            goal_kind = COLLECT
        if not proposal.target or not proposal.reward:
            return None, "в задании не названы цель или награда"

        # Цель задания сопоставляется как существо для «победить» и как вещь для «собрать».
        goal_action = "summon_entity" if goal_kind == KILL else "give_item"
        target = self.resolver.resolve(proposal.target, action=goal_action)
        if not target.ok:
            return None, f"цель «{proposal.target}» не опознана однозначно, назови конкретный объект"
        reward = self.resolver.resolve(proposal.reward, action="give_item")
        if not reward.ok:
            return None, f"награда «{proposal.reward}» не опознана однозначно, назови конкретный предмет"

        try:
            goal_count = int(proposal.count)
            reward_count = int(proposal.reward_count or 1)
        except (TypeError, ValueError):
            return None, "количество в задании должно быть числом"

        verdict = checker.check(goal_kind, target.thing, goal_count, reward.thing, reward_count,
                                inventory=inventory, mode=mode)
        return verdict, verdict.reason or "задание не прошло проверку"

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

    def _build_campaign(self, verdict, mode: str, inventory: Optional[Dict[str, int]],
                        stages: Dict[str, Any], *, proposed: bool = False,
                        candidates: Sequence[GoalCandidate] = ()) -> Turn:
        """Кампания к цели: цепочка этапов, доказанная решателем.

        Модель здесь не участвует: что бы она ни сказала о цели, этапы и награды выводятся
        из дерева плана и инвентаря. Недостижимая цель — отказ с причиной решателя.
        """
        campaign = self.campaigns.build(verdict.target, verdict.count, inventory or {}, mode)
        stages["campaign"] = {
            "feasible": campaign.feasible,
            "stages": campaign.targets,
            "effort": campaign.effort,
        }
        label = self.kb.label(verdict.target)
        if not campaign.feasible:
            return Turn(
                outcome=REJECT, action=START_CAMPAIGN, target=verdict.target, label=label,
                count=verdict.count, reason=campaign.reason,
                explanation=list(verdict.explanation) + list(campaign.explanation),
                stages=stages, proposed=proposed, candidates=tuple(candidates),
            )
        first = campaign.stages[0]
        return Turn(
            outcome=ACT, action=START_CAMPAIGN, target=verdict.target, label=label,
            count=verdict.count,
            explanation=list(verdict.explanation) + [
                f"кампания: этапов {len(campaign.stages)}, трудоёмкость {campaign.effort}"],
            stages=stages,
            campaign=campaign,
            plan=first.plan,
            plan_tree=render_tree(first.plan, self.kb),
            plan_summary=render_summary(first.plan, self.kb),
            proposed=proposed,
            candidates=tuple(candidates),
        )

    def _propose_goal(self, mode: str, inventory: Optional[Dict[str, int]],
                      state: Optional[str], stages: Dict[str, Any]) -> Turn:
        """Цель предлагает ассистент: решатель отбирает достижимые, модель выбирает уместную.

        Выбор модели сверяется со списком кандидатов; что-либо вне списка — или отсутствие
        модели — даёт самую короткую из доказанных целей. Так кампания к предложенной цели
        не может оказаться невыполнимой, кто бы её ни назвал.
        """
        candidates = self.campaigns.propose_goals(inventory or {}, mode)
        stages["candidates"] = [candidate.item for candidate in candidates]
        if not candidates:
            return Turn(outcome=REJECT, action=PROPOSE_GOAL,
                        reason="Из того, что у тебя есть, посильной цели не вывести.",
                        explanation=["решатель не нашёл достижимых целей"], stages=stages)

        chosen = candidates[0]
        choice: Dict[str, Any] = {"goal": None, "why": None, "accepted": False}
        choose = getattr(self.extractor, "choose", None)
        if choose is not None:
            try:
                listed = [{"item": candidate.item, "label": self.kb.label(candidate.item) or candidate.item,
                           "stages": candidate.stages, "effort": candidate.effort}
                          for candidate in candidates]
                answer = choose(listed, state or "") or {}
                choice.update({"goal": answer.get("goal"), "why": answer.get("why")})
                wanted = str(answer.get("goal") or "").strip().lower()
                for candidate, entry in zip(candidates, listed):
                    if wanted and wanted in (candidate.item.lower(), entry["label"].lower()):
                        chosen = candidate
                        choice["accepted"] = True
                        break
            except Exception:
                pass
        stages["choice"] = choice

        verdict = self.validator.validate(START_CAMPAIGN, chosen.item, 1, mode)
        stages["validator"] = {"outcome": verdict.outcome, "clamped": verdict.clamped}
        if verdict.outcome != ACT:
            return Turn(outcome=verdict.outcome, action=START_CAMPAIGN, target=chosen.item,
                        reason=verdict.reason, explanation=verdict.explanation, stages=stages,
                        proposed=True, candidates=tuple(candidates))
        return self._build_campaign(verdict, mode, inventory, stages, proposed=True,
                                    candidates=candidates)

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
