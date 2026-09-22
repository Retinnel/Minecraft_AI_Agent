"""Доступ к базе знаний.

Единственная точка, через которую остальная система обращается к онтологии. Решатель и
резолвер работают с этим интерфейсом и не знают ни про RDF, ни про SPARQL, ни про то, что
предметная область — именно Minecraft.

    kb = KnowledgeBase.load()
    kb.recipes_for("minecraft:diamond_pickaxe")
    kb.sources_of("minecraft:diamond")
    kb.restriction_for("minecraft:ender_dragon", "summon")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, SKOS

from kb import ontology as ont

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "data" / "kb" / "game.ttl"


@dataclass(frozen=True)
class Ingredient:
    """Одна позиция состава рецепта: сколько нужно и чем это можно закрыть."""

    quantity: int
    # Идентификаторы допустимых предметов. Теги уже раскрыты в конкретные предметы,
    # потому что решателю нужны варианты, а не ссылка на группу.
    alternatives: Sequence[str]
    tag: Optional[str] = None

    @property
    def is_choice(self) -> bool:
        return len(self.alternatives) > 1


@dataclass(frozen=True)
class Recipe:
    id: str
    method: str            # craft | smelt | stonecut | smith
    station: str           # crafting_table | furnace | blast_furnace | ...
    produces: str
    produces_count: int
    inputs: Sequence[Ingredient] = field(default_factory=tuple)


@dataclass(frozen=True)
class Source:
    """Откуда предмет берётся в мире, если его нельзя изготовить."""

    id: str                # блок или существо
    kind: str              # block | creature
    tool_kind: Optional[str] = None   # pickaxe | axe | shovel | hoe
    tool_tier: Optional[str] = None   # stone | iron | diamond
    tool_tier_rank: int = 0
    # Обязателен ли инструмент для выпадения. Для дерева и земли нет — они добываются руками,
    # инструмент лишь ускоряет. Для камня и руд обязателен.
    tool_required: bool = False
    # Выпадение случайное и редкое: связь есть, но план на ней строить нельзя.
    rare: bool = False


@dataclass(frozen=True)
class Tool:
    """Инструмент как предмет: чем он является и какого уровня."""

    id: str
    kind: str              # pickaxe | axe | shovel | hoe
    tier: Optional[str]    # wood | gold | stone | iron | diamond | netherite
    tier_rank: int = 0


@dataclass(frozen=True)
class Restriction:
    thing: str
    action: str            # give | summon
    category: str          # boss | service | unbreakable
    reason: str


class KnowledgeBase:
    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self._by_uri: Dict[URIRef, str] = {
            subject: str(game_id)
            for subject, game_id in graph.subject_objects(ont.GAME_ID)
        }

    # --- загрузка ---

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "KnowledgeBase":
        if not path.exists():
            raise FileNotFoundError(
                f"База знаний не собрана: {path}\n"
                f"Запусти: .venv/Scripts/python.exe -m kb.build"
            )
        graph = Graph()
        graph.parse(path, format="turtle")
        return cls(graph)

    @classmethod
    @lru_cache(maxsize=1)
    def shared(cls, path: Path = DEFAULT_PATH) -> "KnowledgeBase":
        """Единственный экземпляр на процесс: разбор графа занимает несколько секунд."""
        return cls.load(path)

    # --- сущности ---

    def exists(self, thing_id: str) -> bool:
        return (ont.thing_uri(thing_id), RDF.type, ont.THING) in self.graph

    def game_id(self, thing_id: str) -> Optional[str]:
        """Точный идентификатор для подстановки в игровую команду.

        Отличается от переданного для предметов с NBT-данными, поэтому команды должны
        собираться именно из этого значения.
        """
        return self._by_uri.get(ont.thing_uri(thing_id))

    def label(self, thing_id: str, lang: str = "ru") -> Optional[str]:
        for value in self.graph.objects(ont.thing_uri(thing_id), RDFS.label):
            if isinstance(value, Literal) and value.language == lang:
                return str(value)
        return None

    def aliases(self, thing_id: str) -> List[str]:
        return sorted(str(value) for value in self.graph.objects(ont.thing_uri(thing_id), SKOS.altLabel))

    def kinds(self, thing_id: str) -> List[str]:
        mapping = {ont.ITEM: "item", ont.BLOCK: "block", ont.CREATURE: "creature"}
        uri = ont.thing_uri(thing_id)
        return sorted(name for cls, name in mapping.items() if (uri, RDF.type, cls) in self.graph)

    # --- группы ---

    def tag_members(self, tag_id: str) -> List[str]:
        uri = ont.tag_uri(tag_id)
        return sorted(
            self._by_uri[member]
            for member in self.graph.objects(uri, ont.HAS_MEMBER)
            if member in self._by_uri
        )

    # --- рецепты ---

    def recipes_for(self, item_id: str) -> List[Recipe]:
        """Все способы изготовить предмет, включая плавку, резку и ковку."""
        target = ont.thing_uri(item_id)
        recipes: List[Recipe] = []

        for recipe_uri in self.graph.subjects(ont.PRODUCES, target):
            inputs: List[Ingredient] = []
            for node in self.graph.objects(recipe_uri, ont.REQUIRES):
                quantity = int(next(self.graph.objects(node, ont.QUANTITY)))
                alternatives: List[str] = []
                tag: Optional[str] = None
                for alternative in self.graph.objects(node, ont.ALTERNATIVE):
                    if (alternative, RDF.type, ont.TAG) in self.graph:
                        tag = str(alternative).rsplit("/", 1)[-1]
                        alternatives.extend(
                            self._by_uri[member]
                            for member in self.graph.objects(alternative, ont.HAS_MEMBER)
                            if member in self._by_uri
                        )
                    elif alternative in self._by_uri:
                        alternatives.append(self._by_uri[alternative])
                inputs.append(Ingredient(quantity=quantity,
                                         alternatives=tuple(sorted(set(alternatives))),
                                         tag=tag))

            recipes.append(Recipe(
                id=str(recipe_uri).rsplit("/", 1)[-1],
                method=str(next(self.graph.objects(recipe_uri, ont.METHOD))),
                station=str(next(self.graph.objects(recipe_uri, ont.AT_STATION))).rsplit("/", 1)[-1],
                produces=item_id,
                produces_count=int(next(self.graph.objects(recipe_uri, ont.PRODUCES_COUNT))),
                inputs=tuple(inputs),
            ))

        # Устойчивый порядок: сначала крафт, потом остальное — так план читается привычнее.
        method_order = {"craft": 0, "smelt": 1, "stonecut": 2, "smith": 3}
        return sorted(recipes, key=lambda r: (method_order.get(r.method, 9), r.id))

    # --- добыча ---

    def sources_of(self, item_id: str) -> List[Source]:
        """Блоки и существа, из которых предмет может быть получен в мире."""
        target = ont.thing_uri(item_id)
        sources: List[Source] = []

        for source_uri in self.graph.subjects(ont.YIELDS, target):
            source_id = self._by_uri.get(source_uri)
            if source_id is None:
                continue
            kind = "creature" if (source_uri, RDF.type, ont.CREATURE) in self.graph else "block"

            tool_kind = next(
                (str(value).rsplit("/", 1)[-1]
                 for value in self.graph.objects(source_uri, ont.REQUIRES_TOOL_KIND)), None)
            tier_uri = next(iter(self.graph.objects(source_uri, ont.REQUIRES_TOOL_TIER)), None)
            tool_tier = str(tier_uri).rsplit("/", 1)[-1] if tier_uri is not None else None
            rank = 0
            if tier_uri is not None:
                rank_value = next(iter(self.graph.objects(tier_uri, ont.TIER_RANK)), None)
                rank = int(rank_value) if rank_value is not None else 0

            required = next(iter(self.graph.objects(source_uri, ont.TOOL_REQUIRED)), None)

            rare = (source_uri, ont.RARE_YIELD, target) in self.graph

            sources.append(Source(id=source_id, kind=kind, tool_kind=tool_kind,
                                  tool_tier=tool_tier, tool_tier_rank=rank,
                                  tool_required=bool(required) if required is not None else False,
                                  rare=rare))

        # Сначала надёжные источники, затем то, что добыть проще.
        return sorted(sources, key=lambda s: (s.rare, s.tool_tier_rank, s.id))

    def tool_profile(self, item_id: str) -> Optional[Tool]:
        """Если предмет — инструмент, возвращает его вид и уровень."""
        uri = ont.thing_uri(item_id)
        kind_uri = next(iter(self.graph.objects(uri, ont.IS_TOOL_KIND)), None)
        if kind_uri is None:
            return None
        tier_uri = next(iter(self.graph.objects(uri, ont.HAS_TOOL_TIER)), None)
        tier = str(tier_uri).rsplit("/", 1)[-1] if tier_uri is not None else None
        return Tool(id=item_id,
                    kind=str(kind_uri).rsplit("/", 1)[-1],
                    tier=tier,
                    tier_rank=self.tool_tier_rank(tier) if tier else 0)

    def tools_of_kind(self, kind: str) -> List[Tool]:
        """Все инструменты заданного вида, от простого к сложному."""
        found: List[Tool] = []
        for uri in self.graph.subjects(ont.IS_TOOL_KIND, ont.tool_kind_uri(kind)):
            item_id = self._by_uri.get(uri)
            if item_id is None:
                continue
            profile = self.tool_profile(item_id)
            if profile is not None:
                found.append(profile)
        return sorted(found, key=lambda tool: (tool.tier_rank, tool.id))

    def tool_tier_rank(self, tier: Optional[str]) -> int:
        return ont.TOOL_TIERS.get(tier or "", 0)

    def tier_satisfies(self, available: Optional[str], required: Optional[str]) -> bool:
        """Хватит ли инструмента уровня `available` для требования `required`."""
        if not required:
            return True
        if not available:
            return False
        return self.tool_tier_rank(available) >= self.tool_tier_rank(required)

    # --- запреты ---

    def restriction_for(self, thing_id: str, action: str) -> Optional[Restriction]:
        """Явный запрет на действие с сущностью, если он есть.

        Отсутствие записи о предмете в базе знаний и запрет на действие с ним — разные вещи.
        Первое означает «не знаю такого», второе — «знаю и не дам», с причиной для игрока.
        """
        target = ont.thing_uri(thing_id)
        for restriction_uri in self.graph.subjects(ont.FORBIDS, target):
            forbidden = str(next(self.graph.objects(restriction_uri, ont.FORBIDDEN_ACTION)))
            if forbidden != action:
                continue
            return Restriction(
                thing=thing_id,
                action=action,
                category=str(next(self.graph.objects(restriction_uri, ont.CATEGORY))),
                reason=str(next(self.graph.objects(restriction_uri, ont.REASON))),
            )
        return None

    def restrictions(self) -> List[Restriction]:
        found: List[Restriction] = []
        for restriction_uri in self.graph.subjects(RDF.type, ont.RESTRICTION):
            thing_uri = next(self.graph.objects(restriction_uri, ont.FORBIDS))
            thing_id = self._by_uri.get(thing_uri)
            if thing_id is None:
                continue
            found.append(Restriction(
                thing=thing_id,
                action=str(next(self.graph.objects(restriction_uri, ont.FORBIDDEN_ACTION))),
                category=str(next(self.graph.objects(restriction_uri, ont.CATEGORY))),
                reason=str(next(self.graph.objects(restriction_uri, ont.REASON))),
            ))
        return sorted(found, key=lambda r: (r.action, r.thing))

    # --- сводка ---

    def stats(self) -> Dict[str, int]:
        return {
            "triples": len(self.graph),
            "things": len(set(self.graph.subjects(RDF.type, ont.THING))),
            "recipes": len(set(self.graph.subjects(RDF.type, ont.RECIPE))),
            "tags": len(set(self.graph.subjects(RDF.type, ont.TAG))),
            "restrictions": len(set(self.graph.subjects(RDF.type, ont.RESTRICTION))),
        }
