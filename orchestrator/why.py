"""Ответ на «почему?» по следу правил предыдущей реплики.

Каждый исход конвейера несёт цепочку применённых правил (`Turn.explanation`): режим, поиск
в базе знаний, запрет, род цели, количество, а для плана — отвергнутые способы получения.
Вопрос «почему?» отвечается этой цепочкой, без модели: объяснение — то, что система
действительно проверяла, а не правдоподобный рассказ о причинах.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

WHY_COMMAND = re.compile(
    r"^\s*(?:а\s+|но\s+)?почему(?:\s+(?:так|нет|нельзя|отказ|отказал|не\s+дал|не\s+выдал|"
    r"именно\s+так|такой\s+план|план\s+такой|ты\s+отказал|ты\s+так\s+решил))?\s*[?.!]*\s*$",
    re.IGNORECASE)

MAX_LINES = 6
CONDITION = "условие:"

HEADS = {
    "reject": "Отказал, потому что проверка остановилась на правиле",
    "clarify": "Переспросил, потому что",
    "act": "Сделал так, потому что проверки пройдены",
}


# Вопрос о самой выдаче: «а выдать можешь?», «почему не можешь выдать?». Ответ — вердикт
# валидатора о выдаче в текущем режиме, а не рассуждение модели о своих возможностях.
GIVE_QUESTION = re.compile(
    r"^\s*(?:а\s+|но\s+)?(?:почему\s+)?(?:ты\s+)?(?:не\s+)?(?:можешь|можно|нельзя|умеешь)\s+"
    r"(?:просто\s+)?(?:выдать|дать|выдавать)(?:\s+(?:мне|предмет|предметы|её|его|это))?\s*[?.!]*\s*$"
    r"|^\s*(?:а\s+)?(?:выдать|дать)\s+(?:можешь|можно|нельзя)\s*[?.!]*\s*$"
    r"|^\s*почему\s+(?:ты\s+)?не\s+(?:выда[её]шь|да[её]шь)\s*[?.!]*\s*$",
    re.IGNORECASE)


def is_why(text: Optional[str]) -> bool:
    return bool(WHY_COMMAND.match(text or ""))


def is_give_question(text: Optional[str]) -> bool:
    return bool(GIVE_QUESTION.match(text or ""))


def render_give(verdict: Any) -> str:
    """Ответ на вопрос о выдаче по вердикту валидатора для текущего режима."""
    if getattr(verdict, "cause", None) == "mode":
        return " ".join(part for part in (verdict.reason, verdict.remedy) if part)
    return "В этом режиме выдаю: скажи «дай» и назови предмет. Запрещённое правилами не выдам."


def snapshot(turn: Any) -> Optional[Dict[str, Any]]:
    """Что запомнить о реплике, чтобы потом объяснить её исход; None — объяснять нечего."""
    trace = [str(line) for line in (getattr(turn, "explanation", None) or []) if line]
    if getattr(turn, "outcome", "none") == "none" or not (trace or getattr(turn, "reason", None)):
        return None
    return {"outcome": turn.outcome, "action": turn.action,
            "label": getattr(turn, "label", None), "reason": getattr(turn, "reason", None),
            "remedy": getattr(turn, "remedy", None), "trace": trace,
            "alternatives": getattr(turn, "plan_alternatives", None)}


def _numbered(lines: List[str]) -> str:
    return "; ".join(f"{index}) {line}" for index, line in enumerate(lines, 1))


def render(why: Optional[Dict[str, Any]]) -> str:
    """Объяснение словами — из запомненного следа."""
    if not why:
        return ("Пока объяснять нечего: спроси «почему?» после ответа на просьбу — "
                "расскажу, какие правила сработали.")
    trace = [line for line in why.get("trace") or [] if not line.startswith(CONDITION)]
    shown = trace[-MAX_LINES:]
    outcome = why.get("outcome")
    parts: List[str] = []
    if outcome == "reject":
        parts.append((why.get("reason") or "Просьба отклонена.").rstrip())
        if shown:
            parts.append("Проверял по порядку: " + _numbered(shown) + ".")
        if why.get("remedy"):
            parts.append(why["remedy"])
    elif outcome == "clarify":
        parts.append("Переспросил, потому что не стал выбирать за тебя: "
                     + (why.get("reason") or "запрос допускал несколько прочтений").rstrip())
        if shown:
            parts.append("Проверял по порядку: " + _numbered(shown) + ".")
    else:
        if why.get("remedy"):
            parts.append(why["remedy"])
        if shown:
            parts.append("Проверки пройдены: " + _numbered(shown) + ".")
        if why.get("alternatives"):
            parts.append("Почему план именно такой:\n" + str(why["alternatives"]).strip())
        if not parts:
            parts.append("Все проверки пройдены, отказывать было не в чем.")
    return " ".join(part for part in parts if part)
