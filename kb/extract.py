"""Извлечение данных игры из jar-файла Minecraft в нормализованный вид.

Первый шаг построения базы знаний. Читает штатные данные версии 1.20.1 и раскладывает их
в плоские JSON-файлы, пригодные для дальнейшей сборки онтологии:

    data/game/items.json      предметы, блоки и существа с названиями
    data/game/recipes.json    рецепты в едином формате, независимо от типа
    data/game/tags.json       теги с рекурсивно раскрытыми значениями
    data/game/drops.json      что выпадает из блоков и существ
    data/game/tools.json      требования к инструменту для добычи блоков

Jar появляется в кэше fabric-loom после сборки мода, русская локаль — после kb.fetch_locale.

Запуск:
    .venv/Scripts/python.exe -m kb.extract
"""

from __future__ import annotations

import argparse
import json
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

ROOT = Path(__file__).resolve().parent.parent
GAME_DIR = ROOT / "data" / "game"
LOOM_CACHE = Path.home() / ".gradle" / "caches" / "fabric-loom"

DATA_PREFIX = "data/minecraft"
LANG_PATH = "assets/minecraft/lang/en_us.json"

# Рецепты, которые задаются кодом игры, а не данными: у них нет ингредиентов в JSON.
SPECIAL_RECIPE_PREFIX = "minecraft:crafting_special_"
# Косметическая отделка брони — к планированию отношения не имеет.
SKIPPED_RECIPE_TYPES = {"minecraft:smithing_trim", "minecraft:crafting_decorated_pot"}

# Станция, на которой выполняется рецепт. Нужна, чтобы плавка в печи и в доменной печи не
# выглядели дубликатами: данные у них совпадают, отличается только оборудование.
RECIPE_STATIONS = {
    "minecraft:crafting_shaped": "crafting_table",
    "minecraft:crafting_shapeless": "crafting_table",
    "minecraft:smelting": "furnace",
    "minecraft:blasting": "blast_furnace",
    "minecraft:smoking": "smoker",
    "minecraft:campfire_cooking": "campfire",
    "minecraft:stonecutting": "stonecutter",
    "minecraft:smithing_transform": "smithing_table",
}

# Теги уровня инструмента, из которых выводится requires-tool-tier.
TOOL_TIER_TAGS = {
    "minecraft:needs_stone_tool": "stone",
    "minecraft:needs_iron_tool": "iron",
    "minecraft:needs_diamond_tool": "diamond",
}
# Теги типа инструмента.
TOOL_KIND_TAGS = ("pickaxe", "axe", "shovel", "hoe")


class GameJar:
    """Доступ к содержимому jar-файла игры."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._zip = zipfile.ZipFile(path)
        self._names = set(self._zip.namelist())

    def read_json(self, name: str) -> Any:
        return json.loads(self._zip.read(name))

    def list_json(self, prefix: str) -> List[str]:
        return sorted(n for n in self._names if n.startswith(prefix) and n.endswith(".json"))

    @staticmethod
    def resource_id(name: str, prefix: str) -> str:
        """data/minecraft/recipes/oak_planks.json -> minecraft:oak_planks"""
        stem = name[len(prefix):].removesuffix(".json").strip("/")
        return f"minecraft:{stem}"


def locate_jar(version: str) -> Path:
    path = LOOM_CACHE / version / "minecraft-client.jar"
    if not path.exists():
        raise SystemExit(
            f"Не найден jar игры: {path}\n"
            f"Собери мод один раз: cd mod/minecraft-mod && ./gradlew.bat build"
        )
    return path


# --- теги ---------------------------------------------------------------------------------


def _tag_entry_id(entry: Any) -> Optional[str]:
    """Значение тега бывает строкой или объектом {"id": ..., "required": false}."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        value = entry.get("id")
        return value if isinstance(value, str) else None
    return None


def extract_tags(jar: GameJar, kind: str) -> Dict[str, List[str]]:
    """Читает теги одного вида (items/blocks/entity_types) и раскрывает вложенные ссылки."""
    prefix = f"{DATA_PREFIX}/tags/{kind}/"
    raw: Dict[str, List[str]] = {}
    for name in jar.list_json(prefix):
        payload = jar.read_json(name)
        values = [_tag_entry_id(entry) for entry in payload.get("values", [])]
        raw[GameJar.resource_id(name, prefix)] = [v for v in values if v]

    resolved: Dict[str, List[str]] = {}

    def resolve(tag: str, seen: Set[str]) -> List[str]:
        if tag in resolved:
            return resolved[tag]
        if tag in seen:
            # Циклическая ссылка между тегами: в ванильных данных не встречается,
            # но проверка дешевле отладки бесконечной рекурсии.
            return []
        seen = seen | {tag}
        members: List[str] = []
        for value in raw.get(tag, []):
            if value.startswith("#"):
                members.extend(resolve(value[1:], seen))
            else:
                members.append(value)
        # Порядок сохраняем, дубли убираем.
        unique = list(dict.fromkeys(members))
        resolved[tag] = unique
        return unique

    return {tag: resolve(tag, set()) for tag in raw}


# --- рецепты ------------------------------------------------------------------------------


def _normalize_ingredient(raw: Any) -> List[Dict[str, str]]:
    """Ингредиент — это набор допустимых альтернатив.

    В данных встречается три формы: {"item": id}, {"tag": id} и список из них.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        alternatives: List[Dict[str, str]] = []
        for entry in raw:
            alternatives.extend(_normalize_ingredient(entry))
        return alternatives
    if isinstance(raw, dict):
        if "item" in raw:
            return [{"kind": "item", "id": raw["item"]}]
        if "tag" in raw:
            return [{"kind": "tag", "id": raw["tag"]}]
    return []


def _normalize_result(raw: Any) -> Optional[Dict[str, Any]]:
    """Результат — строка у плавки и резки, объект у крафта."""
    if isinstance(raw, str):
        return {"item": raw, "count": 1}
    if isinstance(raw, dict) and "item" in raw:
        return {"item": raw["item"], "count": int(raw.get("count", 1))}
    return None


def _shaped_inputs(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Для фигурного рецепта количество ингредиента = сколько раз символ встречается в сетке."""
    pattern = payload.get("pattern") or []
    counts = Counter(char for row in pattern for char in row if char != " ")
    inputs: List[Dict[str, Any]] = []
    for char, ingredient in (payload.get("key") or {}).items():
        alternatives = _normalize_ingredient(ingredient)
        if not alternatives:
            continue
        inputs.append({"alternatives": alternatives, "count": counts.get(char, 0)})
    return inputs


def _shapeless_inputs(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Для бесформенного рецепта одинаковые ингредиенты перечислены по одному разу за штуку."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for ingredient in payload.get("ingredients") or []:
        alternatives = _normalize_ingredient(ingredient)
        if not alternatives:
            continue
        key = json.dumps(alternatives, sort_keys=True)
        bucket = buckets.setdefault(key, {"alternatives": alternatives, "count": 0})
        bucket["count"] += 1
    return list(buckets.values())


def _single_input(payload: Dict[str, Any], field: str = "ingredient") -> List[Dict[str, Any]]:
    alternatives = _normalize_ingredient(payload.get(field))
    return [{"alternatives": alternatives, "count": 1}] if alternatives else []


def extract_recipes(jar: GameJar) -> List[Dict[str, Any]]:
    prefix = f"{DATA_PREFIX}/recipes/"
    recipes: List[Dict[str, Any]] = []

    for name in jar.list_json(prefix):
        payload = jar.read_json(name)
        recipe_type = payload.get("type", "")
        if recipe_type.startswith(SPECIAL_RECIPE_PREFIX) or recipe_type in SKIPPED_RECIPE_TYPES:
            continue

        if recipe_type == "minecraft:crafting_shaped":
            method, inputs = "craft", _shaped_inputs(payload)
        elif recipe_type == "minecraft:crafting_shapeless":
            method, inputs = "craft", _shapeless_inputs(payload)
        elif recipe_type in ("minecraft:smelting", "minecraft:blasting",
                             "minecraft:smoking", "minecraft:campfire_cooking"):
            method, inputs = "smelt", _single_input(payload)
        elif recipe_type == "minecraft:stonecutting":
            method, inputs = "stonecut", _single_input(payload)
        elif recipe_type == "minecraft:smithing_transform":
            method = "smith"
            inputs = _single_input(payload, "base") + _single_input(payload, "addition")
        else:
            # Неизвестный тип — лучше узнать об этом явно, чем молча потерять рецепты.
            raise ValueError(f"Неизвестный тип рецепта {recipe_type!r} в {name}")

        result = _normalize_result(payload.get("result"))
        if result is None or not inputs:
            continue

        if recipe_type == "minecraft:stonecutting":
            result["count"] = int(payload.get("count", 1))

        recipes.append({
            "id": GameJar.resource_id(name, prefix),
            "type": recipe_type,
            "method": method,
            "station": RECIPE_STATIONS[recipe_type],
            "result": result,
            "inputs": inputs,
        })

    return recipes


# --- лут-таблицы --------------------------------------------------------------------------


def _collect_items(node: Any, found: Set[str]) -> None:
    """Обходит дерево лут-таблицы и собирает все достижимые предметы.

    Структура вложенная: pools -> entries, а entry бывает alternatives/group/sequence
    со своими children. Условия выпадения (шёлковое касание, удача, убит игроком) здесь
    не учитываются: для планирования важен сам факт «из этого может получиться то».
    """
    if isinstance(node, list):
        for item in node:
            _collect_items(item, found)
        return
    if not isinstance(node, dict):
        return

    if node.get("type") == "minecraft:item":
        name = node.get("name")
        if isinstance(name, str):
            found.add(name)

    for key in ("pools", "entries", "children"):
        if key in node:
            _collect_items(node[key], found)


def extract_drops(jar: GameJar, kind: str) -> Dict[str, List[str]]:
    prefix = f"{DATA_PREFIX}/loot_tables/{kind}/"
    drops: Dict[str, List[str]] = {}
    for name in jar.list_json(prefix):
        found: Set[str] = set()
        _collect_items(jar.read_json(name), found)
        if found:
            drops[GameJar.resource_id(name, prefix)] = sorted(found)
    return drops


# --- требования к инструменту -------------------------------------------------------------


def extract_tool_requirements(block_tags: Dict[str, List[str]]) -> Dict[str, Dict[str, str]]:
    """Сводит теги needs_*_tool и mineable/* в требования по каждому блоку."""
    requirements: Dict[str, Dict[str, str]] = {}

    for tag, tier in TOOL_TIER_TAGS.items():
        for block in block_tags.get(tag, []):
            requirements.setdefault(block, {})["tier"] = tier

    for kind in TOOL_KIND_TAGS:
        for block in block_tags.get(f"minecraft:mineable/{kind}", []):
            requirements.setdefault(block, {})["tool"] = kind

    return requirements


# --- предметы -----------------------------------------------------------------------------


def _smithing_template_keys(suffix: str) -> List[str]:
    """Ключи локали для кузнечных шаблонов.

    У них нет обычного `item.minecraft.<id>`: название собирается игрой из названия улучшения
    или узора отделки. Например, `netherite_upgrade_smithing_template` именуется через
    `upgrade.minecraft.netherite_upgrade`, а `coast_armor_trim_smithing_template` — через
    `trim_pattern.minecraft.coast`.
    """
    base = suffix.removesuffix("_smithing_template")
    keys = [f"upgrade.minecraft.{base}"]
    pattern = base.removesuffix("_armor_trim")
    keys.append(f"trim_pattern.minecraft.{pattern}")
    return keys


def _fallback_name(suffix: str) -> str:
    return suffix.replace("_", " ").capitalize()


def extract_items(
    jar: GameJar,
    locale_ru: Dict[str, str],
    referenced: Iterable[str] = (),
) -> Dict[str, Dict[str, Any]]:
    """Собирает вселенную сущностей.

    Основа — ключи локали: она перечисляет всё, что игра умеет называть. Плюс идентификаторы,
    на которые ссылаются рецепты, теги и лут-таблицы: часть предметов называется не напрямую,
    и без этого дополнения они потерялись бы.
    """
    locale_en: Dict[str, str] = jar.read_json(LANG_PATH)

    entries: Dict[str, Dict[str, Any]] = {}
    for key, name_en in locale_en.items():
        for prefix, kind in (("item.minecraft.", "item"),
                             ("block.minecraft.", "block"),
                             ("entity.minecraft.", "entity")):
            if not key.startswith(prefix):
                continue
            suffix = key[len(prefix):]
            # У части сущностей есть вложенные ключи вроде entity.minecraft.villager.farmer —
            # это профессии и варианты, отдельными сущностями они не являются.
            if "." in suffix:
                continue
            resource = f"minecraft:{suffix}"
            entry = entries.get(resource)
            if entry is None:
                entries[resource] = {
                    "id": resource,
                    "kinds": [kind],
                    "name_en": name_en,
                    "name_ru": locale_ru.get(key),
                }
            elif kind not in entry["kinds"]:
                # Почти всё, что блок, существует и как предмет.
                entry["kinds"].append(kind)
                entry["name_ru"] = entry["name_ru"] or locale_ru.get(key)
            break

    for resource in referenced:
        if resource in entries:
            continue
        suffix = resource.split(":", 1)[-1]
        keys = _smithing_template_keys(suffix) if suffix.endswith("_smithing_template") else []
        name_en = next((locale_en[key] for key in keys if key in locale_en), None)
        name_ru = next((locale_ru[key] for key in keys if key in locale_ru), None)
        entries[resource] = {
            "id": resource,
            "kinds": ["item"],
            "name_en": name_en or _fallback_name(suffix),
            "name_ru": name_ru,
            # Название получено не из собственного ключа предмета, а косвенно.
            "name_derived": True,
        }

    return entries


# --- сборка -------------------------------------------------------------------------------


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def extract_all(version: str, output_dir: Path) -> Dict[str, int]:
    jar = GameJar(locate_jar(version))

    locale_path = output_dir / f"ru_ru.{version}.json"
    if not locale_path.exists():
        raise SystemExit(
            f"Не найдена русская локаль: {locale_path}\n"
            f"Загрузи её: .venv/Scripts/python.exe -m kb.fetch_locale"
        )
    locale_ru: Dict[str, str] = json.loads(locale_path.read_text(encoding="utf-8"))

    recipes = extract_recipes(jar)
    item_tags = extract_tags(jar, "items")
    block_tags = extract_tags(jar, "blocks")
    entity_tags = extract_tags(jar, "entity_types")
    tools = extract_tool_requirements(block_tags)
    block_drops = extract_drops(jar, "blocks")
    entity_drops = extract_drops(jar, "entities")

    # Вселенная предметов строится с учётом всего, на что ссылаются данные: иначе теряются
    # сущности без собственного ключа локали, например кузнечные шаблоны.
    referenced: Set[str] = set()
    for recipe in recipes:
        referenced.add(recipe["result"]["item"])
        for ingredient in recipe["inputs"]:
            referenced.update(
                alternative["id"]
                for alternative in ingredient["alternatives"]
                if alternative["kind"] == "item"
            )
    for members in item_tags.values():
        referenced.update(members)
    for items_dropped in list(block_drops.values()) + list(entity_drops.values()):
        referenced.update(items_dropped)

    items = extract_items(jar, locale_ru, referenced)

    tags = {"items": item_tags, "blocks": block_tags, "entity_types": entity_tags}
    drops = {"blocks": block_drops, "entities": entity_drops}

    _write(output_dir / "items.json", items)
    _write(output_dir / "recipes.json", recipes)
    _write(output_dir / "tags.json", tags)
    _write(output_dir / "drops.json", drops)
    _write(output_dir / "tools.json", tools)

    without_ru = sum(1 for entry in items.values() if not entry.get("name_ru"))
    return {
        "предметов, блоков и существ": len(items),
        "  из них без русского названия": without_ru,
        "рецептов": len(recipes),
        "  крафт": sum(1 for r in recipes if r["method"] == "craft"),
        "  плавка": sum(1 for r in recipes if r["method"] == "smelt"),
        "  резка": sum(1 for r in recipes if r["method"] == "stonecut"),
        "  ковка": sum(1 for r in recipes if r["method"] == "smith"),
        "тегов предметов": len(item_tags),
        "тегов блоков": len(block_tags),
        "тегов существ": len(entity_tags),
        "блоков с требованием к инструменту": len(tools),
        "блоков с известным выпадением": len(block_drops),
        "существ с известным выпадением": len(entity_drops),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", default="1.20.1")
    parser.add_argument("--output-dir", type=Path, default=GAME_DIR)
    args = parser.parse_args()

    stats = extract_all(args.version, args.output_dir)
    width = max(len(key) for key in stats)
    for key, value in stats.items():
        print(f"{key.ljust(width)}  {value}")
    print(f"\nСохранено в {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
