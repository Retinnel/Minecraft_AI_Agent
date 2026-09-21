"""Словарь онтологии.

Разделён на два пространства имён, и это осознанное решение.

`GA` — ядро, не зависящее от конкретной игры: предметы, рецепты, добыча, требования к
инструменту, запреты. `MC` — индивиды конкретной игры, здесь Minecraft 1.20.1.

Такое разделение нужно уже в курсовой, потому что решатель работает только со словарём ядра и
ничего не знает про Minecraft. В дипломе, где заявлена поддержка нескольких игровых сред,
подключение новой игры сводится к наполнению второго пространства имён.
"""

from __future__ import annotations

import re

from rdflib import Namespace
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

GA = Namespace("http://bsuir.by/kp/game-assistant/core#")
MC = Namespace("http://bsuir.by/kp/game-assistant/minecraft/1.20.1/")

PREFIXES = {"ga": GA, "mc": MC, "owl": OWL, "rdf": RDF, "rdfs": RDFS, "skos": SKOS, "xsd": XSD}

# --- классы ядра ---

THING = GA.Thing              # всё, что имеет идентификатор в игровом мире
ITEM = GA.Item                # может лежать в инвентаре
BLOCK = GA.Block              # может стоять в мире
CREATURE = GA.Creature        # существо
RECIPE = GA.Recipe            # способ получения предмета
INGREDIENT = GA.Ingredient    # одна позиция в составе рецепта с количеством
TAG = GA.Tag                  # именованная группа взаимозаменяемых предметов
STATION = GA.Station          # оборудование, на котором выполняется рецепт
TOOL_TIER = GA.ToolTier       # уровень материала инструмента
TOOL_KIND = GA.ToolKind       # вид инструмента
RESTRICTION = GA.Restriction  # запрет на действие с сущностью

CLASSES = (THING, ITEM, BLOCK, CREATURE, RECIPE, INGREDIENT, TAG, STATION,
           TOOL_TIER, TOOL_KIND, RESTRICTION)

# --- свойства ---

# Рецепт
PRODUCES = GA.produces                # Recipe -> Item
PRODUCES_COUNT = GA.producesCount     # Recipe -> integer
REQUIRES = GA.requires                # Recipe -> Ingredient
METHOD = GA.method                    # Recipe -> literal: craft | smelt | stonecut | smith
AT_STATION = GA.atStation             # Recipe -> Station

# Позиция состава
ALTERNATIVE = GA.alternative          # Ingredient -> Item | Tag
QUANTITY = GA.quantity                # Ingredient -> integer

# Группы
HAS_MEMBER = GA.hasMember             # Tag -> Item

# Добыча
YIELDS = GA.yields                    # Block | Creature -> Item
REQUIRES_TOOL_TIER = GA.requiresToolTier  # Block -> ToolTier
REQUIRES_TOOL_KIND = GA.requiresToolKind  # Block -> ToolKind
TIER_RANK = GA.tierRank               # ToolTier -> integer, для сравнения уровней

# Запреты
FORBIDS = GA.forbids                  # Restriction -> Thing
FORBIDDEN_ACTION = GA.forbiddenAction  # Restriction -> literal: give | summon
REASON = GA.reason                    # Restriction -> literal, текст объяснения игроку
CATEGORY = GA.category                # Restriction -> literal: boss | service | unbreakable

# Точный идентификатор сущности в игре. Хранится отдельно от URI, потому что у части
# предметов он содержит NBT-данные: `minecraft:potion{Potion:"minecraft:healing"}`. Такую
# строку нельзя поместить в URI, но именно её нужно подставлять в игровую команду.
GAME_ID = GA.gameId                   # Thing -> literal

OBJECT_PROPERTIES = (PRODUCES, REQUIRES, AT_STATION, ALTERNATIVE, HAS_MEMBER, YIELDS,
                     REQUIRES_TOOL_TIER, REQUIRES_TOOL_KIND, FORBIDS)
DATA_PROPERTIES = (PRODUCES_COUNT, METHOD, QUANTITY, TIER_RANK, FORBIDDEN_ACTION,
                   REASON, CATEGORY, GAME_ID)

# Порядок уровней инструмента. Нужен решателю, чтобы отвечать на вопрос «этого хватит?»:
# требование `iron` удовлетворяется железом, алмазом и незеритом, но не камнем.
TOOL_TIERS = {"wood": 0, "gold": 0, "stone": 1, "iron": 2, "diamond": 3, "netherite": 4}

# Действия, на которые может накладываться запрет.
ACTIONS = ("give", "summon")


def local_name(resource_id: str) -> str:
    """Превращает игровой идентификатор в пригодный для URI фрагмент.

    Обычные идентификаторы проходят без изменений: `minecraft:diamond` -> `diamond`.
    Идентификаторы с NBT-данными приводятся к читаемому виду:
    `minecraft:potion{Potion:"minecraft:healing"}` -> `potion.healing`.
    Исходная строка при этом не теряется — она записывается свойством `ga:gameId`.
    """
    name = resource_id.split(":", 1)[-1] if resource_id.startswith("minecraft:") else resource_id
    name = name.replace("minecraft:", "")
    cleaned = re.sub(r"[^A-Za-z0-9_./-]+", ".", name)
    return cleaned.strip(".") or "unnamed"


def thing_uri(resource_id: str):
    """minecraft:diamond -> mc:diamond"""
    return MC[local_name(resource_id)]


def recipe_uri(resource_id: str):
    return MC[f"recipe/{local_name(resource_id)}"]


def tag_uri(resource_id: str):
    return MC[f"tag/{local_name(resource_id)}"]


def station_uri(name: str):
    return MC[f"station/{name}"]


def tier_uri(name: str):
    return MC[f"tier/{name}"]


def tool_kind_uri(name: str):
    return MC[f"tool/{name}"]


def restriction_uri(action: str, resource_id: str):
    return MC[f"restriction/{action}/{local_name(resource_id)}"]
