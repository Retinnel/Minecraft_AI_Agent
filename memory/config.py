"""Настройки памяти из settings.json, раздел "memory".

Радиус и частоту сканирования мод получает от сервера (`GET /api/memory/scan-list`), так
что нагрузку можно снизить без пересборки мода.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "scan_radius": 24,
    "scan_interval_s": 5,
    "max_facts": 5000,
    "screen_facts": True,
    "recall_limit": 10,
}

# Границы, за которые настройка не выходит, что бы ни было записано в файле: радиус 64
# уже заметен по частоте кадров, а реже раза в минуту сканер бесполезен.
LIMITS = {
    "scan_radius": (4, 48),
    "scan_interval_s": (1, 60),
    "max_facts": (100, 50000),
    "recall_limit": (1, 50),
}


def settings(path: Path = None) -> Dict[str, Any]:
    path = path or SETTINGS_PATH
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8")).get("memory") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        raw = {}
    result = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = raw.get(key, default) if isinstance(raw, dict) else default
        if isinstance(default, bool):
            result[key] = value if isinstance(value, bool) else default
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = default
        low, high = LIMITS[key]
        result[key] = min(high, max(low, value))
    return result
