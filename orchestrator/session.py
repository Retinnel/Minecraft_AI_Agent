"""Состояние диалога: режим работы, окно последних реплик и сводка фактами.

Принцип: модель не должна ничего помнить — помнит система. Всё, что можно вычислить,
вычисляется и подаётся модели готовым. Из истории диалога модель получает две вещи:

- несколько последних реплик дословно, чтобы работали продолжения вроде «и ещё крипера»;
- сводку того, что произошло раньше, собранную из журнала действий, а не пересказанную
  моделью.

Сводка фактами дешевле пересказа, не деградирует на слабой модели и воспроизводима, а
значит проверяется обычными тестами без обращения к модели.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from solver.validator import CREATIVE, MODES

# Сколько обменов репликами передаётся дословно.
WINDOW_EXCHANGES = 3

# Сколько последних отказов и вопросов держать в сводке: сводка должна оставаться короткой.
SUMMARY_LIMIT = 4

MODE_LABELS = {"creative": "творческий", "survival": "выживание"}


@dataclass
class Session:
    mode: str = CREATIVE
    _window: Deque[Tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=WINDOW_EXCHANGES * 2))
    given: Counter = field(default_factory=Counter)
    removed: Counter = field(default_factory=Counter)
    summoned: Counter = field(default_factory=Counter)
    asked_about: List[str] = field(default_factory=list)
    refused: List[str] = field(default_factory=list)
    quests: List[str] = field(default_factory=list)
    # Уточняющий вопрос, на который игрок ещё не ответил.
    pending: Optional[object] = None
    # Предыдущий обмен одной строкой — для продолжений вроде «и ещё крипера».
    last_exchange: Optional[str] = None

    # --- режим ---

    def set_mode(self, mode: str) -> bool:
        if mode not in MODES:
            return False
        self.mode = mode
        return True

    @property
    def mode_label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode)

    # --- журнал ---

    def record(self, utterance: str, turn, reply: str) -> None:
        """Записывает обмен репликами и его последствия в виде фактов."""
        self._window.append(("user", utterance))
        self._window.append(("assistant", reply))

        # Вопрос живёт ровно одну реплику: если игрок ответил не на него, а сказал что-то
        # другое, держать вопрос открытым значит угадывать дальше.
        self.pending = turn if turn.outcome == "clarify" and turn.options else None
        self.last_exchange = f"игрок: «{utterance}»; итог: {reply}"

        label = turn.label or turn.target
        if turn.outcome == "act" and label:
            if turn.action == "give_item":
                self.given[label] += int(turn.count or 0)
            elif turn.action == "remove_item":
                self.removed[label] += int(turn.count or 0)
            elif turn.action == "summon_entity":
                self.summoned[label] += int(turn.count or 0)
            elif turn.action == "plan_goal":
                self._remember(self.asked_about, f"как получить «{label}»")
            elif turn.action == "create_quest":
                self._remember(self.quests, f"«{label}» ×{turn.count}")
        elif turn.outcome == "reject" and turn.reason:
            subject = f"«{label}»: " if label else ""
            self._remember(self.refused, f"{subject}{turn.reason}")

    @staticmethod
    def _remember(bucket: List[str], fact: str) -> None:
        bucket.append(fact)
        del bucket[:-SUMMARY_LIMIT]

    # --- то, что получает модель ---

    def window(self) -> List[Dict[str, str]]:
        return [{"role": role, "content": text} for role, text in self._window]

    def summary(self) -> str:
        """Сводка сеанса фактами. Пустая строка, если ничего не происходило."""
        lines: List[str] = [f"Режим: {self.mode_label}."]
        for title, counts in (("Выдано", self.given), ("Убрано", self.removed),
                              ("Призвано", self.summoned)):
            if counts:
                items = ", ".join(f"{name} ×{count}" for name, count in counts.most_common(5))
                lines.append(f"{title}: {items}.")
        if self.asked_about:
            lines.append("Спрашивал: " + "; ".join(self.asked_about) + ".")
        if self.quests:
            lines.append("Задания: " + "; ".join(self.quests) + ".")
        if self.refused:
            lines.append("Отказано: " + "; ".join(self.refused) + ".")
        return "\n".join(lines)

    def reset(self) -> None:
        mode = self.mode
        self.__init__()
        self.mode = mode


_session: Optional[Session] = None


def shared() -> Session:
    """Сеанс на процесс: мод однопользовательский, игрок у сервера один."""
    global _session
    if _session is None:
        _session = Session()
    return _session
