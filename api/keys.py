"""Ключи провайдеров: источник — файл `.env` проекта, окружение — только запасной вариант.

Раньше ключи читались из окружения процесса, куда `.env` подмешивается без права
перезаписи. Если в системе уже была переменная с ключом — например, `DEEPSEEK_API_KEY`
от другого инструмента, — сервер молча работал на ней, а не на ключе из `.env`: имена
разные, и системное проверялось первым. Найдено 2026-11-24: языковая модель ходила с
ключом из переменных среды Windows, а распознавание экрана — с ключом из `.env`.

Правило одно: если ключ записан в `.env` проекта, используется он. Окружение читается,
только когда в файле ключа нет (контейнер, CI). Файл перечитывается при каждом запросе
ключа, поэтому замена ключа подхватывается при следующем создании клиента.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

FROM_FILE = ".env"
FROM_ENVIRONMENT = "окружение"


def _stored() -> Dict[str, str]:
    """Содержимое `.env` проекта; пусто, если файла нет или он не читается."""
    try:
        from dotenv import dotenv_values
        if not ENV_PATH.is_file():
            return {}
        return {name: value for name, value in dotenv_values(ENV_PATH).items() if value}
    except Exception:
        return {}


def lookup(*names: str) -> Tuple[Optional[str], Optional[str]]:
    """Ключ и то, откуда он взят («.env: ИМЯ» или «окружение: ИМЯ»).

    Имена перечисляются по убыванию предпочтения. Сначала все имена ищутся в файле, и
    только потом в окружении: ключ из файла важнее любого ключа из окружения.
    """
    stored = _stored()
    for name in names:
        value = (stored.get(name) or "").strip()
        if value:
            return value, f"{FROM_FILE}: {name}"
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value, f"{FROM_ENVIRONMENT}: {name}"
    return None, None


def get(*names: str) -> Optional[str]:
    return lookup(*names)[0]


def fingerprint(key: Optional[str]) -> Optional[str]:
    """Короткий отпечаток ключа: сравнить два ключа, не показывая ни одного."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:10] if key else None


def shadowed(*names: str) -> List[str]:
    """Имена переменных окружения, в которых лежит другой ключ, чем используется.

    Такой ключ не применяется, но о нём стоит сказать: это он раньше подменял ключ из файла.
    """
    used, _ = lookup(*names)
    if not used:
        return []
    return [name for name in names
            if (os.getenv(name) or "").strip() not in ("", used)]


def describe(*names: str) -> Dict[str, object]:
    """Сведения о ключе для журнала запуска и страницы настроек — без самого ключа."""
    key, source = lookup(*names)
    return {"present": bool(key), "source": source, "fingerprint": fingerprint(key),
            "ignored_environment": shadowed(*names)}
