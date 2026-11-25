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
