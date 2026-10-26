"""Рассказчик — нейросетевой шаг кампании: реплики персонажа по фактам решателя.

Этапы, награды и причины уже выведены решателем; персонажу остаётся сказать их в своём
образе. Поэтому модель получает не историю и не просьбу «придумай поручение», а факты с
запретом что-либо к ним добавлять — ровно как `responder`. Сюжет здесь — пересказ цепочки
вывода: «без железной кирки алмазную руду не разбить» становится словами кузнеца, но
остаётся фактом из онтологии.

Персонаж — пресет из конфигурации промптов (`campaign_personas`): имя, род занятий, манера
речи, обращение к игроку. Для каждой реплики есть шаблон из тех же фактов; он используется,
если модель недоступна или потеряла существенное — название этапа, награду. Модель может
сделать поручение живым, но не может сделать его неверным.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

TEMPLATE = "template"
MODEL = "model"

# Пресеты на случай, если в конфигурации их нет. Те же лежат в llm_prompts.json и
# правятся там без изменения кода.
DEFAULT_PERSONAS: Dict[str, Dict[str, str]] = {
    "smith": {
        "name": "Кузнец Торвальд",
        "role": "мастер-кузнец, у которого игрок ходит в учениках",
        "style": "немногословен, по-доброму ворчлив, говорит о деле и инструменте",
        "address": "ученик",
    },
    "elder": {
        "name": "Старейшина Марта",
        "role": "старейшина деревни, которая раздаёт поручения на благо поселения",
        "style": "спокойная, обстоятельная, с достоинством, иногда с присказкой",
        "address": "путник",
    },
    "cartographer": {
        "name": "Картограф Илай",
        "role": "руководитель экспедиции, составляющий карту неизведанных земель",
        "style": "бодрый, любопытный, говорит об открытиях и дороге",
        "address": "напарник",
    },
}

PERSONA_PROMPT = """Ты — {name}, {role}. Ты говоришь с игроком в Minecraft от лица этого персонажа.
Манера речи: {style}. Игрока называй «{address}».

Решение уже принято системой; тебе нужно только сообщить его игроку в своём образе.

Правила:
- Одна-две короткие фразы по-русски, в образе персонажа.
- Не меняй и не добавляй факты: названия, числа, причины и награды бери только из блока «Факты».
- Ничего не обещай и не советуй сверх фактов.
- Не используй разметку и не подписывайся именем.

Кампания:
{campaign}

Факты:
{facts}"""


@dataclass
class Reply:
    text: str
    source: str            # model | template


# --- шаблоны: те же факты без модели ------------------------------------------------------
# Используются и состоянием кампании как запасной текст, поэтому живут на уровне модуля.


def stage_reason(stage: Dict[str, Any]) -> str:
    """Зачем этап нужен — факт из онтологии, который персонаж пересказывает игроку."""
    unlocks = stage.get("unlocks")
    for_label = stage.get("for_label")
    if unlocks == "tool" and for_label:
        return f"Открывает добычу: {for_label}."
    if unlocks == "station" and for_label:
        return f"Открывает изготовление: {for_label}."
    if unlocks == "material" and for_label:
        return f"Понадобится для: {for_label}."
    return "Цель кампании."


def stage_brief(stage: Dict[str, Any]) -> str:
    """Поручение этапа, если рассказчик его не написал."""
    text = (stage.get("texts") or {}).get("brief")
    if text:
        return text
    return (f"Этап {stage['index']} из {stage['total']}: собери {stage['count']} × "
            f"{stage['label']}. {stage_reason(stage)}")


def stage_done(stage: Dict[str, Any]) -> str:
    """Реплика на завершение этапа, если рассказчик её не написал."""
    text = (stage.get("texts") or {}).get("done")
    if text:
        return text
    reward = (f" Награда — {stage['reward_label']} ×{stage['reward_count']}."
              if stage.get("reward") else "")
    return f"Этап {stage['index']} из {stage['total']} пройден: {stage['label']}.{reward}"


def campaign_intro(campaign: Dict[str, Any], *, proposed: bool = False,
                   why: Optional[str] = None) -> str:
    text = (campaign.get("texts") or {}).get("intro")
    if text:
        return text
    total = len(campaign.get("stages") or [])
    goal = f"{campaign['label']} ×{campaign['count']}" if int(campaign.get("count") or 1) > 1 \
        else campaign["label"]
    if proposed:
        reason = f" {why.rstrip('.')}." if why else ""
        return f"Предлагаю цель: {goal}.{reason} Этапов {total}."
    return f"Путь к цели «{goal}»: этапов {total}."


def campaign_outro(campaign: Dict[str, Any]) -> str:
    text = (campaign.get("texts") or {}).get("outro")
    if text:
        return text
    return f"Кампания «{campaign['title']}» пройдена: {campaign['label']} в руках."


def load_personas() -> Dict[str, Dict[str, str]]:
    """Пресеты из конфигурации промптов; без них — встроенные."""
    try:
        from api.llm_config_manager import get_prompt_config
        personas = get_prompt_config().campaign_personas
    except Exception:
        personas = None
    return dict(personas) if personas else dict(DEFAULT_PERSONAS)


# --- рассказчик ---------------------------------------------------------------------------


class Narrator:
    def __init__(self, ask_text: Optional[Callable[[List[Dict[str, str]]], str]] = None,
                 personas: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        if ask_text is None:
            from orchestrator.llm import ask_text as live
            ask_text = live
        self._ask = ask_text
        self.personas = personas or load_personas()

    # --- персонажи ---

    def persona(self, key: Optional[str]) -> Tuple[str, Dict[str, str]]:
        """Пресет по ключу; неизвестный или пустой ключ даёт первый из перечня."""
        if key and key in self.personas:
            return key, self.personas[key]
        first = next(iter(self.personas))
        return first, self.personas[first]

    def persona_name(self, key: Optional[str]) -> str:
        return self.persona(key)[1].get("name") or "Рассказчик"

    def persona_keys(self) -> List[str]:
        return list(self.personas)

    # --- реплики ---

    def intro(self, campaign: Dict[str, Any], *, proposed: bool = False,
              why: Optional[str] = None) -> Reply:
        """Открытие кампании: цель, число этапов; для предложенной цели — почему она."""
        facts = [f"Цель кампании: {campaign['label']} ×{campaign['count']}.",
                 f"Этапов всего: {len(campaign.get('stages') or [])}."]
        if proposed:
            facts.append("Цель предложил ассистент, игрок её не называл.")
            if why:
                facts.append(f"Почему именно она: {why}")
        template = campaign_intro(campaign, proposed=proposed, why=why)
        return self._say(campaign, facts, template, essentials=[campaign["label"]])

    def brief(self, campaign: Dict[str, Any], stage: Dict[str, Any]) -> Reply:
        """Поручение этапа: что собрать, сколько, зачем, какая награда."""
        facts = [f"Этап {stage['index']} из {stage['total']}.",
                 f"Собрать: {stage['label']} ×{stage['count']}.",
                 f"Зачем: {stage_reason(stage)}"]
        if stage.get("source_label"):
            facts.append(f"Где добывается: {stage['source_label']}.")
        if stage.get("reward"):
            facts.append(f"Награда за этап: {stage['reward_label']} ×{stage['reward_count']}.")
        template = stage_brief(stage)
        return self._say(campaign, facts, template, essentials=[stage["label"]])

    def done(self, campaign: Dict[str, Any], stage: Dict[str, Any]) -> Reply:
        """Этап пройден: похвала и вручение награды."""
        facts = [f"Этап {stage['index']} из {stage['total']} пройден: {stage['label']} ×{stage['count']}."]
        essentials: List[str] = []
        if stage.get("reward"):
            facts.append(f"Награда выдана: {stage['reward_label']} ×{stage['reward_count']}.")
            essentials.append(stage["reward_label"])
        template = stage_done(stage)
        return self._say(campaign, facts, template, essentials=essentials)

    def outro(self, campaign: Dict[str, Any]) -> Reply:
        facts = [f"Кампания пройдена, цель достигнута: {campaign['label']} ×{campaign['count']}."]
        template = campaign_outro(campaign)
        return self._say(campaign, facts, template, essentials=[campaign["label"]])

    # --- внутреннее ---

    def _say(self, campaign: Dict[str, Any], facts: List[str], template: str,
             essentials: List[str]) -> Reply:
        key, persona = self.persona(campaign.get("persona"))
        name = persona.get("name") or "Рассказчик"
        prompt = PERSONA_PROMPT.format(
            name=name, role=persona.get("role", ""), style=persona.get("style", ""),
            address=persona.get("address", "игрок"),
            campaign=self._summary(campaign), facts="\n".join(facts))
        try:
            text = self._ask([{"role": "system", "content": prompt},
                              {"role": "user", "content": "Скажи это игроку."}])
        except Exception:
            return Reply(self._signed(name, template), TEMPLATE)
        if not text or not self._keeps_essentials(text, essentials):
            return Reply(self._signed(name, template), TEMPLATE)
        return Reply(self._signed(name, text.strip()), MODEL)

    @staticmethod
    def _signed(name: str, text: str) -> str:
        """Реплика идёт от имени персонажа: в чате это единственный признак, кто говорит."""
        return text if text.lower().startswith(name.lower()) else f"{name}: {text}"

    @staticmethod
    def _keeps_essentials(text: str, essentials: List[str]) -> bool:
        """Названия этапа и награды обязаны прозвучать: без них поручение не исполнить."""
        lowered = text.lower()
        return all(item.lower() in lowered for item in essentials if item)

    @staticmethod
    def _summary(campaign: Dict[str, Any]) -> str:
        stages = campaign.get("stages") or []
        done = [stage["label"] for stage in stages if stage.get("status") == "completed"]
        lines = [f"«{campaign.get('title') or campaign['label']}», цель {campaign['label']} "
                 f"×{campaign['count']}, этапов {len(stages)}."]
        if done:
            lines.append("Пройдено: " + ", ".join(done) + ".")
        current = campaign.get("current")
        if current:
            lines.append(f"Текущий этап: {current}.")
        return "\n".join(lines)
