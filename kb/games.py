"""Игры, для которых есть база знаний помимо Minecraft.

Мир описывается папкой `kb/worlds/<ключ>/`: те же файлы, что `data/game/` для Minecraft
(предметы, рецепты, группы, добыча, требования к инструменту), и `world.json` — название
игры, шкала уровней инструмента, станции, стартовый инвентарь. Онтология собирается тем же
сборщиком `kb.build` в `data/kb/<ключ>/game.ttl` при первом обращении и после правки данных.

Решатель, резолвер и валидатор о мире ничего не знают заранее: всё берётся из онтологии.
Поэтому новая игра — это новая папка с данными, а не новый код.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from kb.graph import KnowledgeBase

ROOT = Path(__file__).resolve().parent.parent
WORLDS_DIR = Path(__file__).resolve().parent / "worlds"
BUILD_DIR = ROOT / "data" / "kb"


@dataclass
class World:
    key: str
    game: str
    names: List[str]
    source: Path
    starting_inventory: Dict[str, int] = field(default_factory=dict)
    # Витрина древа развития: какие предметы показывать (узлы и ступени выводит решатель).
    showcase: List[str] = field(default_factory=list)
    _kb: Optional[KnowledgeBase] = None

    @property
    def path(self) -> Path:
        return BUILD_DIR / self.key / "game.ttl"

    def _stale(self) -> bool:
        if not self.path.exists():
            return True
        built = self.path.stat().st_mtime
        return any(file.stat().st_mtime > built for file in self.source.glob("*.json"))

    def kb(self) -> KnowledgeBase:
        """База знаний мира; собирается из данных, если её нет или данные новее."""
        if self._kb is None:
            if self._stale():
                from kb.build import build
                build(self.source, self.path.parent)
            self._kb = KnowledgeBase.load(self.path)
        return self._kb


_worlds: Optional[Dict[str, World]] = None


def worlds() -> Dict[str, World]:
    global _worlds
    if _worlds is None:
        found: Dict[str, World] = {}
        for manifest in sorted(WORLDS_DIR.glob("*/world.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            game = str(data.get("game") or "").strip()
            if not game:
                continue
            key = manifest.parent.name
            names = [game.lower()] + [str(name).lower() for name in data.get("names") or []]
            found[key] = World(key=key, game=game, names=names, source=manifest.parent,
                               starting_inventory=dict(data.get("starting_inventory") or {}),
                               showcase=list(data.get("showcase") or []))
        _worlds = found
    return _worlds


def find(game: Optional[str]) -> Optional[World]:
    """Мир с базой знаний по названию игры, как его произнёс игрок."""
    name = (game or "").strip().lower()
    if not name:
        return None
    for world in worlds().values():
        if name in world.names:
            return world
    return None


def reset() -> None:
    global _worlds
    _worlds = None
