"""Адаптер экрана: реплики персонажей из распознанных кадров → факты памяти.

Наблюдатель экрана разбирает кадр моделью зрения в `SceneFacts`; сами факты живут 30 секунд.
Здесь из них сохраняется то, о чём игрок может спросить позже, — реплика персонажа в диалоге
(«что говорила Кейра про лекарство?»). Снимки не хранятся, только распознанный текст.

Текст с экрана — данные, а не указания: он попадает в память с источником «экран» и
уверенностью 0.6, ответ называет источник. Повтор того же кадра отсекается хэшем реплики.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from memory.model import Fact
from memory.store import MemoryStore

# Короче этого — обрывок распознавания, а не реплика.
MIN_LINE = 8


def enabled(settings: Dict[str, Any]) -> bool:
    return bool(settings.get("enabled")) and bool(settings.get("screen_facts"))


def remember_scene(store: MemoryStore, facts: Any) -> List[Fact]:
    """Сохраняет реплику диалога с кадра; возвращает записанные факты."""
    dialogue = getattr(facts, "dialogue", None)
    if dialogue is None:
        return []
    line = _line(getattr(dialogue, "line", None))
    if line is None:
        return []
    fact = store.record_dialogue(getattr(dialogue, "speaker", None), line)
    return [fact] if fact is not None else []


def _line(text: Optional[str]) -> Optional[str]:
    text = " ".join(str(text or "").split())
    return text if len(text) >= MIN_LINE else None
