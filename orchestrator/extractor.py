"""Извлечение намерения языковой моделью — нейросетевой шаг на входе конвейера.

Модель отвечает на вопрос «что игрок хочет и о чём говорит» и переносит название объекта
словами самого игрока. Идентификаторы она не называет: их подставляет резолвер по онтологии.

Перечень действий, предъявляемый модели, собирается из режима работы. В выживании выдачи
в нём нет, и модель не может её предложить, — а если всё же предложит, валидатор отклонит.
Режим управляет составом действий, а не текстом уговоров в промпте.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

from orchestrator.pipeline import Proposal

# Что означает каждое действие — ровно в тех словах, что увидит модель.
ACTION_HINTS = {
    "give_item": "игрок просит выдать предмет или блок",
    "remove_item": "игрок просит забрать или убрать предмет из инвентаря",
    "summon_entity": "игрок просит призвать существо",
    "plan_goal": "игрок спрашивает, как что-то получить, скрафтить или добыть",
    "create_quest": "игрок просит одно короткое задание, испытание или челлендж",
    "start_campaign": ("игрок называет дальнюю цель и хочет, чтобы его вели к ней по этапам: "
                       "«веди меня к…», «хочу пройти путь до…», «начни кампанию»; "
                       "в \"target\" — сама цель"),
    "propose_goal": ("игрок просит предложить ему цель или занятие, не называя её: "
                     "«дай цель», «чем заняться», «что делать дальше»; \"target\" — null"),
}

EXTRACTABLE = tuple(ACTION_HINTS)

PROMPT = """Ты разбираешь реплику игрока в Minecraft.

Верни ТОЛЬКО JSON без пояснений:
{{
  "action": {actions} | "none",
  "target": "предмет или существо словами игрока, либо null",
  "count": число{quest_fields}
}}

Значения action:
{hints}
- "none" — во всех остальных случаях: разговор, вопрос, приветствие.

Правила:
- В "target" перенеси название так, как его назвал игрок. Идентификаторы вида
  minecraft:... не выдумывай: их подставит система по своей базе знаний.
- В "target" не добавляй ничего от себя: если игрок сказал «меч», так и пиши «меч».
- Если количество не названо, ставь 1.{quest_rules}
{feedback}{context}
Реплика игрока: "{{utterance}}"
"""

# Предыдущий обмен нужен для продолжений: «и ещё крипера» после призыва зомби означает
# призыв, хотя глагола в реплике нет. Передаётся одна строка, а не история целиком.
CONTEXT = """
Предыдущий обмен: {context}
Если реплика продолжает его («и ещё …», «а теперь …»), сохрани то же действие.
"""

QUEST_FIELDS = """,
  "goal_kind": "collect" | "kill" — только для create_quest,
  "reward": "награда словами — только для create_quest",
  "reward_count": число — только для create_quest"""

QUEST_RULES = """
- Для create_quest придумай задание сам: в "target" — что собрать или кого победить,
  в "count" — сколько, в "reward" — что дать в награду. Задание должно быть посильным
  без редких предметов, награда — соразмерной."""

FEEDBACK = """
Предыдущее предложение отклонено: {reason}
Предложи другое, учитывая причину.
"""

# В выживании выдачи нет, и просьба «дай X» должна вести к плану, как X добыть. Без явного
# правила модель ищет ближайшее доступное действие и иногда выбирает задание.
NO_GIVE_RULE = """
- Выдача предметов сейчас недоступна. Просьбу дать предмет трактуй как "plan_goal":
  игрок получит план, как добыть его самому."""


def build_prompt(actions: Sequence[str], feedback: Optional[str] = None,
                 context: Optional[str] = None) -> str:
    """Промпт под набор действий, доступных в текущем режиме."""
    usable = [action for action in actions if action in ACTION_HINTS]
    quest = "create_quest" in usable
    rules = QUEST_RULES if quest else ""
    if "give_item" not in usable and "plan_goal" in usable:
        rules += NO_GIVE_RULE
    return PROMPT.format(
        actions=" | ".join(f'"{action}"' for action in usable),
        hints="\n".join(f'- "{action}" — {ACTION_HINTS[action]}.' for action in usable),
        quest_fields=QUEST_FIELDS if quest else "",
        quest_rules=rules,
        feedback=FEEDBACK.format(reason=feedback) if feedback else "",
        context=CONTEXT.format(context=context) if context else "",
    )


# Английское название запрашивается вместе с русским: его модель знает твёрже, русские
# переводы у неё «плавают» («слизневый блок» вместо «Блок слизи»). Оба есть в базе знаний.
RENAME_PROMPT = """Игрок в Minecraft назвал предмет или существо так: "{utterance}".

Какой это предмет или существо в игре? Верни ТОЛЬКО JSON:
{"en": "официальное английское название" или null, "ru": "официальное русское название" или null}

Если игрок описал существующий предмет своими словами, назови его. Если такого предмета
или существа в Minecraft нет, верни null в обоих полях."""


# Выбор цели кампании. Решатель уже отобрал цели, достижимые из инвентаря; модели остаётся
# выбрать уместную. Выбор вне списка не проходит: конвейер сверяет ответ с кандидатами.
CHOOSE_PROMPT = """Ты — игровой ассистент в Minecraft. Игрок просит предложить ему цель.
Система уже отобрала цели, достижимые из его инвентаря. Выбери одну — самую полезную для
развития игрока прямо сейчас.

Кандидаты (название — этапов, трудоёмкость):
{candidates}

Состояние игрока:
{state}

Верни ТОЛЬКО JSON без пояснений:
{{"goal": "название из списка, точно как написано", "why": "одна короткая фраза по-русски, почему"}}"""


def _text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


class LanguageModelIntent:
    """Извлечение намерения языковой моделью.

    Обращение к модели передаётся снаружи: на сервере это живой провайдер, в тестах —
    заглушка. Так конвейер проверяется целиком без сети и без квоты.
    """

    def __init__(self, ask: Optional[Callable[[str, str], Dict[str, Any]]] = None) -> None:
        if ask is None:
            from orchestrator.llm import ask_json
            ask = ask_json
        self._ask = ask

    def extract(self, utterance: str, *, actions: Sequence[str] = EXTRACTABLE,
                feedback: Optional[str] = None, context: Optional[str] = None) -> Proposal:
        payload = self._ask(build_prompt(actions, feedback, context), utterance)
        action = str(payload.get("action") or "none").strip()
        return Proposal(
            action=action,
            target=_text(payload.get("target")),
            count=payload.get("count", 1),
            goal_kind=_text(payload.get("goal_kind")),
            reward=_text(payload.get("reward")),
            reward_count=payload.get("reward_count", 1),
            raw=payload,
        )

    def rename(self, phrase: str) -> Sequence[str]:
        """Официальные названия того, что игрок назвал по-своему; пусто, если такого нет.

        Вызывается, когда резолвер ничего не нашёл: «светящийся камень» в базе знаний
        не записан, а светокамень — записан. Названия, которые вернёт модель, проходят тот
        же резолвер, так что выдумать несуществующий предмет она не может.
        """
        payload = self._ask(RENAME_PROMPT, phrase)
        names = [_text(payload.get(key)) for key in ("en", "ru", "name")]
        return [name for name in names if name]

    def choose(self, candidates: Sequence[Dict[str, Any]], state: str = "") -> Dict[str, Any]:
        """Выбор цели кампании среди кандидатов решателя.

        Возвращает {"goal": название, "why": причина}. Название сверяется с кандидатами
        на стороне конвейера; что-либо вне списка выбрать нельзя.
        """
        listed = "\n".join(
            f"- {candidate['label']} — {candidate['stages']}, {candidate['effort']}"
            for candidate in candidates)
        prompt = CHOOSE_PROMPT.format(candidates=listed, state=state or "нет сведений")
        payload = self._ask(prompt, "")
        return {"goal": _text(payload.get("goal")), "why": _text(payload.get("why"))}
