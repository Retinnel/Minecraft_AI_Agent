"""Формулирование ответа — нейросетевой шаг на выходе конвейера.

Решение уже принято решателем; модели остаётся сказать его по-человечески. Поэтому она
получает не реплику игрока с просьбой «ответь», а факты, выведенные решателем, с запретом
что-либо к ним добавлять. Знания о предметной области в запрос не попадают вовсе: всё, что
модель может сообщить игроку, ей передано готовым.

Модель здесь не обязательна. Для каждого исхода есть шаблонный ответ, собранный из тех же
фактов, и он используется, если модель недоступна или её ответ потерял существенное —
например, итоговое число при уменьшении количества до предела. Так модель может сделать
ответ живее, но не может сделать его неверным.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from orchestrator.pipeline import CREATE_QUEST, NONE, PLAN_GOAL, START_CAMPAIGN, Turn
from solver.validator import ACT, CLARIFY, MAX_ITEM_COUNT, MAX_SUMMON_COUNT, REJECT

TEMPLATE = "template"
MODEL = "model"

PHRASING_PROMPT = """Ты — игровой ассистент в Minecraft. Решение по запросу игрока уже принято,
тебе нужно только сообщить его игроку.

Правила:
- Одна-две короткие фразы по-русски, дружелюбно.
- Не меняй и не добавляй факты: названия, числа и причины бери только из блока «Факты».
- Ничего не обещай и не советуй сверх фактов.
- Не используй разметку.

Факты:
{facts}"""

CHAT_PROMPT = """Ты — игровой ассистент в Minecraft, говоришь с игроком в игровом чате.
Отвечай одной короткой фразой по-русски, с лёгким юмором.

Ты ничего не выдаёшь и не выполняешь в этой реплике: если игрок о чём-то просит, предложи
сказать это прямо, например «дай факелы» или «как сделать печь».
Не придумывай рецептов и свойств предметов: таких сведений у тебя здесь нет.

Состояние игрока:
{state}

Что было в этом сеансе:
{summary}"""

ACTION_VERBS = {"give_item": "Выдаю", "remove_item": "Убираю", "summon_entity": "Призываю"}


def _inline(label: str) -> str:
    """Название внутри фразы пишется со строчной: «выдаю алмаз», а не «выдаю Алмаз»."""
    return label[:1].lower() + label[1:] if label else label


@dataclass
class Reply:
    text: str
    source: str            # model | template


class Responder:
    def __init__(self, ask_text: Optional[Callable[[List[Dict[str, str]]], str]] = None) -> None:
        if ask_text is None:
            from orchestrator.llm import ask_text as live
            ask_text = live
        self._ask = ask_text

    # --- шаблоны ---

    @staticmethod
    def template(turn: Turn) -> str:
        """Ответ, собранный из выхода решателя без участия модели."""
        if turn.outcome == ACT:
            if turn.action == PLAN_GOAL:
                return turn.plan_summary or "План построен."
            if turn.action == START_CAMPAIGN and turn.campaign:
                # Обычно кампанию озвучивает рассказчик; шаблон — на случай, если его нет.
                return (f"Кампания к цели «{turn.label or turn.target}»: "
                        f"этапов {len(turn.campaign.stages)}.")
            if turn.action == CREATE_QUEST and turn.quest:
                quest = turn.quest
                verb = "Победи" if quest.get("goal_kind") == "kill" else "Собери"
                return (f"Задание: {verb.lower()} {_inline(turn.label or turn.target)} "
                        f"×{quest['goal_count']}. Награда — "
                        f"{_inline(quest.get('reward_label') or quest['reward'])} "
                        f"×{quest['reward_count']}.")
            verb = ACTION_VERBS.get(turn.action or "", "Готово:")
            text = f"{verb} {_inline(turn.label or turn.target)} ×{turn.count}."
            if (turn.stages.get("validator") or {}).get("clamped"):
                limit = MAX_SUMMON_COUNT if turn.action == "summon_entity" else MAX_ITEM_COUNT
                text += f" Больше {limit} за раз нельзя."
            return text
        if turn.outcome in (CLARIFY, REJECT):
            return turn.reason or ("Уточни запрос." if turn.outcome == CLARIFY else "Не могу.")
        return "..."

    @staticmethod
    def facts(turn: Turn) -> str:
        """Выход решателя в виде, пригодном для формулирования."""
        lines = [f"Ответ, который нужно передать: {Responder.template(turn)}"]
        if turn.outcome == ACT and turn.action == PLAN_GOAL and turn.plan is not None:
            lines.append("Подробный план показан игроку на отдельном экране.")
        if turn.outcome == CLARIFY and turn.options:
            lines.append("Варианты: " + ", ".join(option.label for option in turn.options) + ".")
        return "\n".join(lines)

    # --- формулирование ---

    def reply(self, turn: Turn, utterance: str, *, state: str = "", summary: str = "",
              window: Optional[List[Dict[str, str]]] = None) -> Reply:
        if turn.outcome == NONE:
            return self._chat(utterance, state, summary, window or [])
        return self._phrase(turn)

    def _phrase(self, turn: Turn) -> Reply:
        template = self.template(turn)
        try:
            text = self._ask([
                {"role": "system", "content": PHRASING_PROMPT.format(facts=self.facts(turn))},
                {"role": "user", "content": "Сообщи это игроку."},
            ])
        except Exception:
            return Reply(template, TEMPLATE)
        if not text or not self._keeps_essentials(turn, text):
            return Reply(template, TEMPLATE)
        return Reply(text, MODEL)

    @staticmethod
    def _keeps_essentials(turn: Turn, text: str) -> bool:
        """Сохранила ли модель то, без чего ответ становится неверным.

        Для выполненного действия это число: игрок должен узнать, сколько получил, особенно
        если количество уменьшено до предела. Для уточнения — хотя бы один из вариантов,
        иначе вопрос «какой именно?» повисает в воздухе.
        """
        if turn.outcome == ACT and turn.action in ACTION_VERBS:
            return str(turn.count) in text
        if turn.outcome == CLARIFY and turn.options:
            lowered = text.lower()
            return any(option.label.lower() in lowered for option in turn.options)
        return True

    def _chat(self, utterance: str, state: str, summary: str,
              window: List[Dict[str, str]]) -> Reply:
        messages = [{"role": "system",
                     "content": CHAT_PROMPT.format(state=state or "нет сведений",
                                                   summary=summary or "ничего")}]
        messages.extend(window)
        messages.append({"role": "user", "content": utterance})
        try:
            text = self._ask(messages)
        except Exception:
            text = ""
        return Reply(text, MODEL) if text else Reply("Я тут. Скажи, что нужно.", TEMPLATE)
