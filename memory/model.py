"""Место в мире игры: измерение и координаты блока.

Координаты всегда ставит система — из сообщений мода, а не из ответа модели. Модель их не
называет и не пересказывает по памяти: в ответ они попадают готовой строкой, и пересказ,
потерявший число, заменяется шаблоном.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

OVERWORLD = "minecraft:overworld"
NETHER = "minecraft:the_nether"
END = "minecraft:the_end"

DIMENSION_LABELS = {
    OVERWORLD: "Верхний мир",
    NETHER: "Незер",
    END: "Край",
}

# Восемь сторон света по углу. В Minecraft север — убывание z, восток — рост x.
DIRECTIONS = ("на восток", "на юго-восток", "на юг", "на юго-запад",
              "на запад", "на северо-запад", "на север", "на северо-восток")

# Координаты дальше этого предела в игре не встречаются (граница мира — 30 млн блоков).
MAX_COORDINATE = 30_000_000


def _number(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or abs(number) > MAX_COORDINATE:
        return None
    return int(math.floor(number))


def _signed(value: int) -> str:
    """Минус в координатах — типографский: «x −120», как в ответах ассистента."""
    return f"−{abs(value)}" if value < 0 else str(value)


@dataclass(frozen=True)
class Position:
    dimension: str
    x: int
    y: int
    z: int

    @classmethod
    def parse(cls, payload: Any) -> Optional["Position"]:
        """Позиция из сообщения мода; None, если координаты неполные или не числа."""
        if not isinstance(payload, dict):
            return None
        x, y, z = (_number(payload.get(axis)) for axis in ("x", "y", "z"))
        if x is None or y is None or z is None:
            return None
        dimension = str(payload.get("dimension") or OVERWORLD).strip().lower()[:80] or OVERWORLD
        if ":" not in dimension:
            dimension = f"minecraft:{dimension}"
        return cls(dimension=dimension, x=x, y=y, z=z)

    def to_dict(self) -> Dict[str, Any]:
        return {"dimension": self.dimension, "x": self.x, "y": self.y, "z": self.z}

    @property
    def dimension_label(self) -> str:
        return DIMENSION_LABELS.get(self.dimension, self.dimension)

    def coordinates(self) -> str:
        return f"x {_signed(self.x)}, y {_signed(self.y)}, z {_signed(self.z)}"

    def describe(self, *, with_dimension: bool = True) -> str:
        text = self.coordinates()
        return f"{text}, {self.dimension_label}" if with_dimension else text

    def distance(self, other: Optional["Position"]) -> Optional[int]:
        """Расстояние по горизонтали в блоках; между измерениями его нет."""
        if other is None or other.dimension != self.dimension:
            return None
        return int(round(math.hypot(self.x - other.x, self.z - other.z)))

    def direction_from(self, origin: Optional["Position"]) -> Optional[str]:
        """В какую сторону идти от origin, чтобы прийти сюда."""
        if origin is None or origin.dimension != self.dimension:
            return None
        dx, dz = self.x - origin.x, self.z - origin.z
        if dx == 0 and dz == 0:
            return None
        angle = math.degrees(math.atan2(dz, dx)) % 360
        return DIRECTIONS[int((angle + 22.5) // 45) % 8]

    def relative(self, origin: Optional["Position"]) -> Optional[str]:
        """«около 190 блоков на юго-запад» — от места, где игрок сейчас."""
        distance = self.distance(origin)
        if distance is None:
            return None
        if distance < 8:
            return "совсем рядом"
        direction = self.direction_from(origin)
        return f"около {distance} блоков {direction}" if direction else f"около {distance} блоков"


# --- факт памяти ---

# Виды фактов.
PLACE = "place"            # отмеченное место: «запомни, тут портал»
RESOURCE = "resource"      # увиденные ценные блоки: руды, порталы, спаунеры
CONTAINER = "container"    # хранилище и его содержимое
DEATH = "death"            # место и обстоятельства гибели
NOTE = "note"              # заметка игрока без места
DIALOGUE = "dialogue"      # реплика персонажа, распознанная на экране
KINDS = (PLACE, RESOURCE, CONTAINER, DEATH, NOTE, DIALOGUE)

# Откуда факт и насколько ему верить. Ответ называет источник: распознанному с экрана
# доверия меньше, чем сообщению мода.
MOD = "mod"
PLAYER = "player"
SCREEN = "screen"
# Помощник — отдельный игрок под управлением сервера: видит мир так же точно, как мод.
COMPANION = "companion"
CONFIDENCE = {MOD: 1.0, PLAYER: 0.9, SCREEN: 0.6, COMPANION: 1.0}

# Состояние факта.
ACTIVE = "active"
STALE = "stale"            # при повторном визите не подтвердился: блок добыт или пропал
REMOVED = "removed"        # игрок попросил забыть
STATUSES = (ACTIVE, STALE, REMOVED)

MAX_LABEL = 80
MAX_TEXT = 500
MAX_BLOCKS = 64


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Fact:
    """Одна запись памяти. Значения — координаты, количества — ставит система."""

    id: str
    game: str
    world: str
    kind: str
    subject: Optional[str] = None        # в Minecraft — сущность онтологии, в других играх — текст
    label: Optional[str] = None
    position: Optional[Position] = None
    contents: Dict[str, int] = None      # для хранилищ: предмет → количество
    text: Optional[str] = None           # заметка или реплика персонажа
    source: str = MOD
    confidence: float = 1.0
    first_seen: str = ""
    last_seen: str = ""
    seen_count: int = 1
    status: str = ACTIVE
    # Для руд: координаты блоков участка 16×16 — по ним видно, сколько блоков и какие пропали.
    blocks: list = None

    def __post_init__(self) -> None:
        if self.contents is None:
            self.contents = {}
        if self.blocks is None:
            self.blocks = []

    @property
    def active(self) -> bool:
        return self.status == ACTIVE

    @property
    def amount(self) -> int:
        """Сколько блоков у ресурса; у прочих фактов — 0."""
        return len(self.blocks)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id, "game": self.game, "world": self.world, "kind": self.kind,
            "subject": self.subject, "label": self.label,
            "position": self.position.to_dict() if self.position else None,
            "contents": dict(self.contents), "text": self.text,
            "source": self.source, "confidence": self.confidence,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "seen_count": self.seen_count, "status": self.status,
        }
        if self.blocks:
            payload["blocks"] = [list(block) for block in self.blocks]
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> Optional["Fact"]:
        """Запись из файла; повреждённая или чужая строка даёт None, а не исключение."""
        if not isinstance(payload, dict) or not payload.get("id"):
            return None
        kind = payload.get("kind")
        if kind not in KINDS:
            return None
        contents = {}
        for key, value in (payload.get("contents") or {}).items():
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count > 0:
                contents[str(key)] = count
        blocks = []
        for block in payload.get("blocks") or []:
            if isinstance(block, (list, tuple)) and len(block) == 3:
                coordinates = [_number(value) for value in block]
                if None not in coordinates:
                    blocks.append(tuple(coordinates))
        source = payload.get("source") if payload.get("source") in CONFIDENCE else MOD
        try:
            confidence = float(payload.get("confidence", CONFIDENCE[source]))
        except (TypeError, ValueError):
            confidence = CONFIDENCE[source]
        try:
            seen_count = max(1, int(payload.get("seen_count") or 1))
        except (TypeError, ValueError):
            seen_count = 1
        return cls(
            id=str(payload["id"]), game=str(payload.get("game") or ""),
            world=str(payload.get("world") or ""), kind=kind,
            subject=payload.get("subject") or None, label=payload.get("label") or None,
            position=Position.parse(payload.get("position")),
            contents=contents, text=payload.get("text") or None,
            source=source, confidence=confidence,
            first_seen=str(payload.get("first_seen") or ""),
            last_seen=str(payload.get("last_seen") or ""),
            seen_count=seen_count,
            status=payload.get("status") if payload.get("status") in STATUSES else ACTIVE,
            blocks=blocks,
        )
