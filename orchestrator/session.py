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

# Игра по умолчанию — та, для которой есть онтология.
DEFAULT_GAME = "Minecraft"

# Длина названия игры: защита от того, чтобы в «игру» не попала целая фраза.
MAX_GAME_NAME = 60


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
    # Кампания одной строкой и персонаж, от лица которого идут поручения.
    campaign: Optional[str] = None
    persona: Optional[str] = None
    # Уточняющий вопрос, на который игрок ещё не ответил.
    pending: Optional[object] = None
    # След правил последней реплики — для ответа на «почему?» (orchestrator/why.py).
    last_why: Optional[dict] = None
    # Предмет и количество последней реплики по делу — для «сделай это».
    last_target: Optional[tuple] = None
    # Предыдущий обмен одной строкой — для продолжений вроде «и ещё крипера».
    last_exchange: Optional[str] = None
    # Игра, о которой спрашивает игрок. Для Minecraft работает онтология, для остальных —
    # только поиск. Когда появится распознавание экрана, игру будет определять оно.
    game: str = DEFAULT_GAME

    # --- игра ---

    def set_game(self, name: str) -> bool:
        name = " ".join((name or "").split()).strip(" .,!?«»\"")
        if not name or len(name) > MAX_GAME_NAME:
            return False
        if name.lower() != self.game.lower():
            self.pending = None
        self.game = name
        return True

    @property
    def is_default_game(self) -> bool:
        return self.game.lower() == DEFAULT_GAME.lower()

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
        # След держится до следующей реплики по делу: «почему?» после пары слов болтовни
        # всё ещё относится к последнему отказу или плану.
        from orchestrator.why import snapshot
        self.last_why = snapshot(turn) or self.last_why
        if getattr(turn, "target", None):
            self.last_target = (turn.target, turn.count)

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
            elif turn.action == "search_web":
                self._remember(self.asked_about, f"«{label}» — ответ найден в сети")
            elif turn.action == "create_quest":
                self._remember(self.quests, f"«{label}» ×{turn.count}")
            elif turn.action == "start_campaign":
                total = len(turn.campaign.stages) if turn.campaign else 0
                self.campaign = f"«{label}», этапов {total}"
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
        if self.campaign:
            lines.append(f"Кампания: {self.campaign}.")
        if self.persona:
            lines.append(f"Персонаж: {self.persona}.")
        if self.refused:
            lines.append("Отказано: " + "; ".join(self.refused) + ".")
        return "\n".join(lines)

    def reset(self) -> None:
        mode, persona, game = self.mode, self.persona, self.game
        self.__init__()
        self.mode = mode
        self.persona = persona
        self.game = game

    # --- сохранение между запусками ---

    def to_dict(self) -> Dict[str, object]:
        """Всё, что нужно, чтобы продолжить разговор после перезапуска.

        Неотвеченный уточняющий вопрос не сохраняется: он живёт одну реплику, и игрок,
        вернувшийся в мир на следующий день, отвечает уже не на него.
        """
        return {
            "mode": self.mode, "persona": self.persona, "game": self.game,
            "window": [list(pair) for pair in self._window],
            "given": dict(self.given), "removed": dict(self.removed),
            "summoned": dict(self.summoned),
            "asked_about": list(self.asked_about), "refused": list(self.refused),
            "quests": list(self.quests), "campaign": self.campaign,
            "last_exchange": self.last_exchange,
            "last_why": self.last_why,
            "last_target": list(self.last_target) if self.last_target else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "Session":
        """Сессия из сохранённого; повреждённые или чужие поля отбрасываются молча —
        разговор в худшем случае начнётся заново, а не упадёт."""
        session = cls()
        if not isinstance(data, dict):
            return session
        session.set_mode(str(data.get("mode") or CREATIVE))
        session.persona = data.get("persona") if isinstance(data.get("persona"), str) else None
        if isinstance(data.get("game"), str):
            session.set_game(data["game"])
        for pair in data.get("window") or []:
            if (isinstance(pair, (list, tuple)) and len(pair) == 2
                    and pair[0] in ("user", "assistant") and isinstance(pair[1], str)):
                session._window.append((pair[0], pair[1]))
        for name in ("given", "removed", "summoned"):
            counts = data.get(name)
            if isinstance(counts, dict):
                getattr(session, name).update(
                    {str(key): int(value) for key, value in counts.items()
                     if isinstance(value, (int, float))})
        for name in ("asked_about", "refused", "quests"):
            facts = data.get(name)
            if isinstance(facts, list):
                getattr(session, name).extend(str(fact) for fact in facts[-SUMMARY_LIMIT:])
        session.campaign = data.get("campaign") if isinstance(data.get("campaign"), str) else None
        session.last_why = data.get("last_why") if isinstance(data.get("last_why"), dict) else None
        target = data.get("last_target")
        if isinstance(target, (list, tuple)) and len(target) == 2 and isinstance(target[0], str):
            session.last_target = (target[0], target[1])
        session.last_exchange = (data.get("last_exchange")
                                 if isinstance(data.get("last_exchange"), str) else None)
        return session


def replace(session: Session) -> None:
    """Подменяет сеанс процесса: при входе в другой мир разговор продолжается с его места."""
    global _session
    _session = session


_session: Optional[Session] = None


def shared() -> Session:
    """Сеанс на процесс: мод однопользовательский, игрок у сервера один."""
    global _session
    if _session is None:
        _session = Session()
    return _session
