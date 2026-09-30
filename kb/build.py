"""Сборка онтологии игрового мира из извлечённых данных.

Вход:
    data/game/*.json              результат kb.extract
    kb/restrictions.json          рукописный слой запретов
    kb/aliases.json               рукописный слой разговорных названий
    data/minecraft_catalog.json   разговорные алиасы прошлого семестра
    data/entity_catalog.json      то же для существ

Выход:
    data/kb/game.ttl              онтология в формате Turtle

Запуск:
    .venv/Scripts/python.exe -m kb.build
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from kb import ontology as ont

ROOT = Path(__file__).resolve().parent.parent
GAME_DIR = ROOT / "data" / "game"
KB_DIR = ROOT / "data" / "kb"
RESTRICTIONS_PATH = Path(__file__).resolve().parent / "restrictions.json"
ALIASES_PATH = Path(__file__).resolve().parent / "aliases.json"
LEGACY_ITEM_CATALOG = ROOT / "data" / "minecraft_catalog.json"
LEGACY_ENTITY_CATALOG = ROOT / "data" / "entity_catalog.json"

KIND_CLASSES = {"item": ont.ITEM, "block": ont.BLOCK, "entity": ont.CREATURE}


def _load(path: Path) -> Any:
    if not path.exists():
        raise SystemExit(
            f"Не найден файл: {path}\n"
            f"Запусти извлечение: .venv/Scripts/python.exe -m kb.extract"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _declare_schema(graph: Graph) -> None:
    """TBox: классы и свойства ядра, не зависящие от конкретной игры."""
    for cls in ont.CLASSES:
        graph.add((cls, RDF.type, OWL.Class))

    for subclass in (ont.ITEM, ont.BLOCK, ont.CREATURE):
        graph.add((subclass, RDFS.subClassOf, ont.THING))

    for prop in ont.OBJECT_PROPERTIES:
        graph.add((prop, RDF.type, OWL.ObjectProperty))
    for prop in ont.DATA_PROPERTIES:
        graph.add((prop, RDF.type, OWL.DatatypeProperty))

    domains_ranges = [
        (ont.PRODUCES, ont.RECIPE, ont.ITEM),
        (ont.REQUIRES, ont.RECIPE, ont.INGREDIENT),
        (ont.AT_STATION, ont.RECIPE, ont.STATION),
        (ont.HAS_MEMBER, ont.TAG, ont.ITEM),
        (ont.REQUIRES_TOOL_TIER, ont.BLOCK, ont.TOOL_TIER),
        (ont.REQUIRES_TOOL_KIND, ont.BLOCK, ont.TOOL_KIND),
        (ont.FORBIDS, ont.RESTRICTION, ont.THING),
    ]
    for prop, domain, range_ in domains_ranges:
        graph.add((prop, RDFS.domain, domain))
        graph.add((prop, RDFS.range, range_))


def _add_tool_tiers(graph: Graph) -> None:
    """Уровни инструмента упорядочены: решателю нужно сравнивать «хватит ли этого»."""
    for name, rank in ont.TOOL_TIERS.items():
        uri = ont.tier_uri(name)
        graph.add((uri, RDF.type, ont.TOOL_TIER))
        graph.add((uri, RDFS.label, Literal(name, lang="en")))
        graph.add((uri, ont.TIER_RANK, Literal(rank)))


def _add_things(graph: Graph, items: Dict[str, Dict[str, Any]]) -> None:
    for resource_id, entry in items.items():
        uri = ont.thing_uri(resource_id)
        graph.add((uri, RDF.type, ont.THING))
        graph.add((uri, ont.GAME_ID, Literal(resource_id)))
        for kind in entry.get("kinds", []):
            cls = KIND_CLASSES.get(kind)
            if cls is not None:
                graph.add((uri, RDF.type, cls))
        if entry.get("name_en"):
            graph.add((uri, RDFS.label, Literal(entry["name_en"], lang="en")))
        if entry.get("name_ru"):
            graph.add((uri, RDFS.label, Literal(entry["name_ru"], lang="ru")))


def _add_recipes(graph: Graph, recipes: List[Dict[str, Any]]) -> None:
    stations = set()
    for recipe in recipes:
        uri = ont.recipe_uri(recipe["id"])
        graph.add((uri, RDF.type, ont.RECIPE))
        graph.add((uri, ont.PRODUCES, ont.thing_uri(recipe["result"]["item"])))
        graph.add((uri, ont.PRODUCES_COUNT, Literal(recipe["result"]["count"])))
        graph.add((uri, ont.METHOD, Literal(recipe["method"])))

        station = recipe["station"]
        stations.add(station)
        graph.add((uri, ont.AT_STATION, ont.station_uri(station)))

        for index, ingredient in enumerate(recipe["inputs"]):
            node = URIRef(f"{uri}/in/{index}")
            graph.add((node, RDF.type, ont.INGREDIENT))
            graph.add((node, ont.QUANTITY, Literal(ingredient["count"])))
            graph.add((uri, ont.REQUIRES, node))
            for alternative in ingredient["alternatives"]:
                target = (ont.tag_uri if alternative["kind"] == "tag" else ont.thing_uri)(alternative["id"])
                graph.add((node, ont.ALTERNATIVE, target))

    for station in sorted(stations):
        uri = ont.station_uri(station)
        graph.add((uri, RDF.type, ont.STATION))
        graph.add((uri, RDFS.label, Literal(station, lang="en")))


def _add_tags(graph: Graph, item_tags: Dict[str, List[str]]) -> None:
    for tag_id, members in item_tags.items():
        uri = ont.tag_uri(tag_id)
        graph.add((uri, RDF.type, ont.TAG))
        graph.add((uri, RDFS.label, Literal(tag_id, lang="en")))
        for member in members:
            graph.add((uri, ont.HAS_MEMBER, ont.thing_uri(member)))


def _add_drops(graph: Graph, drops: Dict[str, Any], craftable: set) -> int:
    """Связи «источник -> что из него получается».

    Два правила при записи.

    Связь блока с самим собой сохраняется, но только если блок нельзя изготовить. Петли здесь
    нет: блок в мире и предмет в инвентаре — разные вещи, и добыча блока не требует иметь
    такой предмет. Для дерева, земли, песка и руд это единственный способ их получить.
    А вот для алмазного блока такая связь означала бы «найди алмазный блок в мире»: он
    выпадает сам собой просто потому, что его можно поднять обратно, естественным ресурсом
    он не является. Отсюда правило: что можно изготовить — то не добывают.

    Редкие выпадения помечаются флагом. Железный слиток действительно падает с зомби, но
    с шансом в доли процента, и строить на этом план нельзя.
    """
    rare_count = 0
    for source_id, produced in list(drops["blocks"].items()) + list(drops["entities"].items()):
        source = ont.thing_uri(source_id)
        for item_id, is_rare in produced.items():
            if item_id == source_id and item_id in craftable:
                continue
            target = ont.thing_uri(item_id)
            graph.add((source, ont.YIELDS, target))
            if is_rare:
                graph.add((source, ont.RARE_YIELD, target))
                rare_count += 1
    return rare_count


def _add_tool_requirements(graph: Graph, tools: Dict[str, Dict[str, Any]]) -> None:
    kinds = set()
    for block_id, requirement in tools.items():
        uri = ont.thing_uri(block_id)
        if "tier" in requirement:
            graph.add((uri, ont.REQUIRES_TOOL_TIER, ont.tier_uri(requirement["tier"])))
        if "tool" in requirement:
            kinds.add(requirement["tool"])
            graph.add((uri, ont.REQUIRES_TOOL_KIND, ont.tool_kind_uri(requirement["tool"])))
            graph.add((uri, ont.TOOL_REQUIRED, Literal(bool(requirement.get("tool_required")))))

    for kind in sorted(kinds):
        uri = ont.tool_kind_uri(kind)
        graph.add((uri, RDF.type, ont.TOOL_KIND))
        graph.add((uri, RDFS.label, Literal(kind, lang="en")))


def _add_tool_items(graph: Graph, item_tags: Dict[str, List[str]]) -> int:
    """Отмечает сами инструменты: какого они вида и какого уровня.

    Вид берётся из тега (`minecraft:pickaxes` и подобных), уровень — из приставки в
    идентификаторе: `wooden_pickaxe` даёт уровень `wood`. Приставка — единственный источник,
    отдельного тега на уровень в данных игры нет.
    """
    marked = 0
    for tag_id, kind in ont.TOOL_TAGS.items():
        for item_id in item_tags.get(tag_id, []):
            uri = ont.thing_uri(item_id)
            graph.add((uri, ont.IS_TOOL_KIND, ont.tool_kind_uri(kind)))
            prefix = item_id.split(":", 1)[-1].split("_", 1)[0]
            tier = ont.TOOL_MATERIAL_PREFIXES.get(prefix)
            if tier:
                graph.add((uri, ont.HAS_TOOL_TIER, ont.tier_uri(tier)))
            marked += 1
    return marked


def _add_restrictions(graph: Graph, restrictions: Dict[str, Any]) -> int:
    added = 0
    for action in ont.ACTIONS:
        for entry in restrictions.get(f"forbidden_{action}", []):
            uri = ont.restriction_uri(action, entry["id"])
            graph.add((uri, RDF.type, ont.RESTRICTION))
            graph.add((uri, ont.FORBIDS, ont.thing_uri(entry["id"])))
            graph.add((uri, ont.FORBIDDEN_ACTION, Literal(action)))
            graph.add((uri, ont.REASON, Literal(entry["reason"], lang="ru")))
            graph.add((uri, ont.CATEGORY, Literal(entry["category"])))
            added += 1
    return added


def _add_manual_aliases(graph: Graph, known: set) -> int:
    """Разговорные названия из рукописного слоя.

    Нужны там, где официальное название расходится с тем, как объект называют в речи.
    Для запрещённых сущностей это условие работоспособности запрета: чтобы отказать
    с объяснением, система должна сначала понять, о ком идёт речь.
    """
    added = 0
    for entry in _load(ALIASES_PATH).get("aliases", []):
        resource_id = entry.get("id")
        if not resource_id or resource_id not in known:
            continue
        uri = ont.thing_uri(resource_id)
        for alias in entry.get("aliases_ru") or []:
            text = (alias or "").strip()
            if text:
                graph.add((uri, SKOS.altLabel, Literal(text, lang="ru")))
                added += 1
    return added


def _add_legacy_aliases(graph: Graph, known: set) -> Dict[str, int]:
    """Разговорные названия из рукописных каталогов прошлого семестра.

    Официальная локаль их не содержит: «гэпл», «реда», «дебрис», «слаймбол» — это живая речь
    игроков, и для сопоставления запросов она ценнее формальных названий. Ручная работа
    прошлого семестра не выбрасывается, а переезжает в онтологию как дополнительные метки.

    Часть записей описывает варианты предметов с NBT-данными — зелья. В данных игры они
    отдельными предметами не числятся, поэтому такие сущности заводятся здесь.
    """
    added = 0
    variants = 0

    def absorb(entries: Iterable[Dict[str, Any]]) -> None:
        nonlocal added, variants
        for entry in entries:
            resource_id = entry.get("id")
            if not resource_id:
                continue
            uri = ont.thing_uri(resource_id)

            if resource_id not in known:
                # Вариант предмета, которого нет в данных игры как отдельной записи.
                graph.add((uri, RDF.type, ont.THING))
                graph.add((uri, RDF.type, ont.ITEM))
                graph.add((uri, ont.GAME_ID, Literal(resource_id)))
                if entry.get("display_name_ru"):
                    graph.add((uri, RDFS.label, Literal(entry["display_name_ru"], lang="ru")))
                if entry.get("display_name_en"):
                    graph.add((uri, RDFS.label, Literal(entry["display_name_en"], lang="en")))
                known.add(resource_id)
                variants += 1

            for alias in list(entry.get("aliases_ru") or []) + list(entry.get("aliases_en") or []):
                text = (alias or "").strip()
                if not text:
                    continue
                graph.add((uri, SKOS.altLabel, Literal(text, lang="ru")))
                added += 1

    absorb(_load(LEGACY_ITEM_CATALOG))
    entities = _load(LEGACY_ENTITY_CATALOG)
    absorb(entities if isinstance(entities, list) else entities.get("entities", []))
    return {"aliases": added, "variants": variants}


def build(game_dir: Path, output_dir: Path) -> Dict[str, Any]:
    items = _load(game_dir / "items.json")
    recipes = _load(game_dir / "recipes.json")
    tags = _load(game_dir / "tags.json")
    drops = _load(game_dir / "drops.json")
    tools = _load(game_dir / "tools.json")
    restrictions = _load(RESTRICTIONS_PATH)

    graph = Graph()
    for prefix, namespace in ont.PREFIXES.items():
        graph.bind(prefix, namespace)

    _declare_schema(graph)
    _add_tool_tiers(graph)
    _add_things(graph, items)
    _add_recipes(graph, recipes)
    _add_tags(graph, tags["items"])
    craftable = {recipe["result"]["item"] for recipe in recipes}
    rare_yields = _add_drops(graph, drops, craftable)
    _add_tool_requirements(graph, tools)
    tool_items = _add_tool_items(graph, tags["items"])
    restriction_count = _add_restrictions(graph, restrictions)
    legacy = _add_legacy_aliases(graph, set(items))
    # Рукописные названия добавляются последними: они относятся ко всем заведённым
    # сущностям, включая существ и варианты предметов из каталогов прошлого семестра.
    manual_aliases = _add_manual_aliases(
        graph, {str(value) for value in graph.objects(None, ont.GAME_ID)}
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "game.ttl"
    graph.serialize(destination=target, format="turtle")

    return {
        "граф": graph,
        "файл": target,
        "триплетов": len(graph),
        "сущностей": len(set(graph.subjects(RDF.type, ont.THING))),
        "рецептов": len(set(graph.subjects(RDF.type, ont.RECIPE))),
        "групп (тегов)": len(set(graph.subjects(RDF.type, ont.TAG))),
        "связей добычи": len(list(graph.triples((None, ont.YIELDS, None)))),
        "  из них редких": rare_yields,
        "инструментов размечено": tool_items,
        "запретов": restriction_count,
        "разговорных алиасов": legacy["aliases"],
        "  из рукописного слоя": manual_aliases,
        "  вариантов с NBT заведено": legacy["variants"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--game-dir", type=Path, default=GAME_DIR)
    parser.add_argument("--output-dir", type=Path, default=KB_DIR)
    args = parser.parse_args()

    stats = build(args.game_dir, args.output_dir)
    target = stats.pop("файл")
    stats.pop("граф")
    width = max(len(key) for key in stats)
    for key, value in stats.items():
        print(f"{key.ljust(width)}  {value}")
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"\nСохранено: {target.relative_to(ROOT)} ({size_mb:.1f} МБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
