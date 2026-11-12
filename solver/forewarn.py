"""Предупреждение об угрозе: кто рядом, чем это грозит и какая защита под рукой.

Разбор смерти отвечает на вопрос «что могло спасти» задним числом. Здесь тот же вывод
делается заранее: мод сообщает, какие враждебные существа рядом, рукописный слой
`kb/threats.json` сводит существо к классу угрозы, а `DeathAnalyzer.assess` устанавливает,
какая мера защиты лежит в инвентаре без дела или собирается из него без единой добычи.

Предупреждение считается содержательным, только если такая мера есть. Сказать «рядом крипер,
будь осторожен» может и строка в интерфейсе; ассистент говорит, когда ему есть что
предложить, и молчит, когда игрок уже защищён либо защищаться нечем.

Языковая модель не участвует: предупреждение должно прийти раньше взрыва.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from kb.graph import KnowledgeBase
from solver.postmortem import ARMOR, DeathAnalyzer, PostMortem, _inline, _steps_word

# Если существо в слое не названо, оно дерётся врукопашную: так устроено большинство.
DEFAULT_THREAT = "combat"
# Порядок выбора, когда рядом несколько существ: сначала то, что убивает быстрее.
DEFAULT_PRIORITY = ("explosion", "ranged", "combat")


@dataclass
class Forewarning:
    mobs: List[str]            # существа, от которых исходит выбранная угроза
    threat: str
    threat_label: str
    state: PostMortem          # что надето, что лежит без дела, что собирается

    @property
    def actionable(self) -> bool:
        """Есть ли что предложить: мера в инвентаре или собираемая из него."""
        return self.state.preventable

    @property
    def key(self) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
        """Содержание предупреждения: пока оно то же, повторять его незачем.

        Статус меры входит в ключ: «щит собирается» и «щит лежит, возьми в руку» — разные
        советы об одном предмете.
        """
        advised = tuple((m.item, m.status) for m in self.state.unused + self.state.craftable)
        return self.threat, advised


def normalize_mob(raw: str) -> str:
    """Идентификатор существа из того, что шлёт мод.

    Мод передаёт строковое представление типа — «entity.minecraft.creeper»; страница
    симуляции и тесты — «minecraft:creeper». Оба вида сводятся ко второму.
    """
    text = str(raw or "").strip().lower()
    if text.startswith("entity."):
        text = text[len("entity."):]
        namespace, _, name = text.partition(".")
        return f"{namespace}:{name}" if name else text
    return text if ":" in text else f"minecraft:{text}" if text else ""


class ThreatAdvisor:
    def __init__(self, analyzer: DeathAnalyzer) -> None:
        self.analyzer = analyzer
        self.kb: KnowledgeBase = analyzer.kb
        layer = analyzer.layer
        self.mob_threats: Dict[str, str] = dict(layer.get("mobs") or {})
        self.priority: Sequence[str] = tuple(layer.get("threat_priority") or DEFAULT_PRIORITY)

    def assess(self, mobs: Iterable[str], inventory: Optional[Dict[str, int]] = None,
               armor: Sequence[str] = (), held: Sequence[str] = ()) -> Optional[Forewarning]:
        """Предупреждение по существам рядом; None, если угрозы среди них нет."""
        by_threat: Dict[str, List[str]] = {}
        for raw in mobs:
            mob = normalize_mob(raw)
            if not mob or not self.kb.exists(mob):
                continue
            threat = self.mob_threats.get(mob, DEFAULT_THREAT)
            if mob not in by_threat.setdefault(threat, []):
                by_threat[threat].append(mob)

        threat = next((name for name in self.priority if name in by_threat), None)
        if threat is None:
            return None
        state = self.analyzer.assess(threat, inventory, armor=armor, held=held)
        names = ", ".join(f"«{self.kb.label(mob) or mob}»" for mob in by_threat[threat])
        state.explanation.insert(0, f"рядом {names} — угроза «{state.threat_label}»")
        if not state.preventable:
            state.explanation.append(
                "предложить нечего: защита уже в деле либо из инвентаря не собирается — "
                "ассистент молчит")
        return Forewarning(mobs=by_threat[threat], threat=threat,
                           threat_label=state.threat_label, state=state)


def render_forewarning(warning: Forewarning, kb: KnowledgeBase) -> str:
    """Предупреждение словами — из фактов решателя, без модели."""
    def name(item: str) -> str:
        return _inline(kb.label(item) or item)

    state = warning.state
    who = ", ".join(name(mob) for mob in warning.mobs)
    parts = [f"Рядом {who}: угроза — {warning.threat_label}."]
    if state.unused:
        worn = [m for m in state.unused if m.group == ARMOR]
        other = [m for m in state.unused if m.group != ARMOR]
        if worn:
            parts.append("В инвентаре лежит, а не надето: "
                         + ", ".join(name(m.item) for m in worn) + ".")
        if other:
            parts.append("Возьми в руку: " + ", ".join(name(m.item) for m in other) + ".")
    if state.craftable:
        parts.append("Из инвентаря прямо сейчас собирается: " + ", ".join(
            f"{name(m.item)} ({m.steps} {_steps_word(m.steps)})" for m in state.craftable) + ".")
    return " ".join(parts)
