"""Загрузка русской локали Minecraft из официальных ассетов Mojang.

В minecraft-client.jar лежит только en_us.json, остальные языки распространяются отдельными
объектами через индекс ассетов. Путь такой:

    minecraft-info.json (кэш loom) -> assetIndex.url -> объект minecraft/lang/ru_ru.json

Индекс версии уже скачан сборкой мода, поэтому обращений в сеть ровно два: сам индекс и файл
локали. Результат кладётся в data/game/ и дальше используется импортёром онтологии.

Запуск:
    .venv/Scripts/python.exe -m kb.fetch_locale
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "data" / "game"

# Кэш fabric-loom с метаданными версии; заполняется при сборке мода.
LOOM_CACHE = Path.home() / ".gradle" / "caches" / "fabric-loom"

RESOURCES_BASE = "https://resources.download.minecraft.net"
LANG_ASSET = "minecraft/lang/ru_ru.json"
USER_AGENT = "kp-game-assistant/1.0 (course project, local tooling)"


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def read_version_info(version: str) -> Dict[str, Any]:
    path = LOOM_CACHE / version / "minecraft-info.json"
    if not path.exists():
        raise SystemExit(
            f"Не найдены метаданные версии: {path}\n"
            f"Собери мод один раз, чтобы loom их скачал: cd mod/minecraft-mod && ./gradlew.bat build"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_locale(version: str, asset: str = LANG_ASSET) -> Dict[str, str]:
    info = read_version_info(version)
    asset_index = info.get("assetIndex") or {}
    index_url = asset_index.get("url")
    if not index_url:
        raise SystemExit(f"В метаданных версии {version} нет assetIndex.url")

    print(f"Индекс ассетов ({asset_index.get('id')}): {index_url}")
    index = json.loads(_get(index_url))

    entry = index.get("objects", {}).get(asset)
    if not entry:
        raise SystemExit(f"В индексе ассетов нет объекта {asset}")

    digest = entry["hash"]
    object_url = f"{RESOURCES_BASE}/{digest[:2]}/{digest}"
    print(f"Локаль {asset}: {object_url} ({entry.get('size')} байт)")
    return json.loads(_get(object_url))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", default="1.20.1", help="версия Minecraft")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    locale = fetch_locale(args.version)

    names = {
        key: value
        for key, value in locale.items()
        if key.startswith(("item.minecraft.", "block.minecraft.", "entity.minecraft."))
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / f"ru_ru.{args.version}.json"
    target.write_text(json.dumps(locale, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Сохранено: {target.relative_to(ROOT)}")
    print(f"Всего ключей: {len(locale)}, из них предметов/блоков/существ: {len(names)}")
    for key in ("item.minecraft.diamond", "block.minecraft.crafting_table", "entity.minecraft.ender_dragon"):
        if key in locale:
            print(f"  {key} -> {locale[key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
