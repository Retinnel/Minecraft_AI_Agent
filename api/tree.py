"""Древо развития для страницы /tree и оверлея мода.

Строение древа и состояние узлов выводит `solver.techtree` из онтологии и инвентаря;
этот модуль только отдаёт их по HTTP. Инвентарь — тот же снимок, что присылает мод, поэтому
древо «загорается» по мере игры.

Иконки предметов читаются из jar игры, который лежит в кэше сборки мода на машине
пользователя. В репозиторий они не попадают: текстуры принадлежат Mojang.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from solver.planner import CRAFT, HUNT, MINE, SMELT, SMITH, STONECUT
from solver.render import REMOTE_RU, render_alternatives, render_summary, render_tree
from solver.techtree import HAVE_IT, LOCKED, OPEN, READY, UNKNOWN, TechTree

router = APIRouter(prefix="/api/tree", tags=["tree"])

GAME_VERSION = "1.20.1"

# Заголовок ступени: что нужно иметь, чтобы её предметы стали доступны.
TIER_LABELS = ["Голыми руками", "Деревянная кирка", "Каменная кирка", "Железная кирка",
               "Алмазная кирка", "Незеритовая кирка"]

HOW_RU = {CRAFT: "изготовить", SMELT: "переплавить", STONECUT: "вырезать", SMITH: "выковать",
          MINE: "добыть", HUNT: "выбить"}

STATUS_RU = {HAVE_IT: "есть в инвентаре", READY: "получается прямо сейчас",
             OPEN: "ступень открыта, нужна добыча", LOCKED: "нужна кирка следующей ступени",
             UNKNOWN: "план не строится"}

_tree_instance: Optional[TechTree] = None
_world_trees: Dict[str, TechTree] = {}


def _world():
    """Мир со своей базой знаний, если разговор сейчас о такой игре (kb/worlds)."""
    from api import advice
    from kb import games
    session = advice._assistant_session()
    return None if session.is_default_game else games.find(session.game)


def _tree() -> TechTree:
    """Древо для игры разговора: Minecraft либо мир из kb/worlds — тот же вывод, другая
    онтология и своя витрина."""
    global _tree_instance
    world = _world()
    if world is not None:
        if world.key not in _world_trees:
            from solver.planner import Planner
            kb = world.kb()
            tree = TechTree(kb, Planner(kb), showcase=world.showcase,
                            base=world.starting_inventory)
            # Названия ступеней — по киркам этого мира: ступень N открывает кирка уровня N-1.
            picks = {}
            for tool in kb.tools_of_kind("pickaxe"):
                picks.setdefault(tool.tier_rank, kb.label(tool.id) or tool.id)
            tree.tier_labels = [TIER_LABELS[0]] + [picks[rank] for rank in sorted(picks)]
            _world_trees[world.key] = tree
        return _world_trees[world.key]
    if _tree_instance is None:
        from api import advice
        pipeline = advice._assistant_pipeline()
        _tree_instance = TechTree(pipeline.kb, pipeline.planner)
    return _tree_instance


def _inventory() -> Dict[str, int]:
    world = _world()
    if world is not None:
        # Мода для этой игры нет: состояние узлов считается от стартового инвентаря мира.
        return dict(world.starting_inventory)
    from api import advice
    return advice._inventory_counts()


def tree_payload(tree: TechTree, inventory: Dict[str, int]) -> Dict[str, Any]:
    """Древо целиком: строение и состояние узлов при данном инвентаре."""
    kb = tree.kb
    structure = tree.structure()
    states = tree.state(inventory)
    rank = tree.rank({item: count for item, count in inventory.items() if count > 0})

    nodes: List[Dict[str, Any]] = []
    for node in structure.nodes.values():
        state = states[node.id]
        nodes.append({
            "id": node.id,
            "label": node.label,
            "tier": node.tier,
            "depth": node.depth,
            "showcase": node.showcase,
            "how": HOW_RU.get(node.how, node.how),
            "source": (kb.label(node.source) or node.source) if node.source else None,
            "where": REMOTE_RU.get(node.remote or ""),
            "status": state.status,
            "status_label": STATUS_RU.get(state.status, state.status),
            "count": state.count,
            "steps": state.steps,
        })
    nodes.sort(key=lambda entry: (entry["tier"], entry["depth"], entry["label"]))

    used = sorted({node["tier"] for node in nodes})
    labels = getattr(tree, "tier_labels", None) or TIER_LABELS
    return {
        "rank": rank,
        # Ступени, открытые игроку: всё до уровня его лучшей кирки включительно.
        "open_tier": rank + 1,
        "tiers": [{"index": tier, "label": labels[tier] if tier < len(labels) else str(tier)}
                  for tier in used],
        "nodes": nodes,
        "edges": [{"source": edge.source, "target": edge.target, "kind": edge.kind}
                  for edge in structure.edges],
        "skipped": [kb.label(item) or item for item in structure.skipped],
        "counts": {status: sum(1 for node in nodes if node["status"] == status)
                   for status in (HAVE_IT, READY, OPEN, LOCKED)},
    }


def compact_payload(tree: TechTree, inventory: Dict[str, int]) -> Dict[str, Any]:
    """Сжатое древо для оверлея мода: только витрина, по ступеням, без рёбер."""
    full = tree_payload(tree, inventory)
    tiers = []
    for tier in full["tiers"]:
        entries = [{"id": node["id"], "label": node["label"], "status": node["status"],
                    "steps": node["steps"]}
                   for node in full["nodes"]
                   if node["tier"] == tier["index"] and node["showcase"]]
        if entries:
            tiers.append({"index": tier["index"], "label": tier["label"],
                          "open": tier["index"] <= full["open_tier"], "nodes": entries})
    return {"rank": full["rank"], "counts": full["counts"], "tiers": tiers}


@router.get("/")
async def get_tree():
    return tree_payload(_tree(), _inventory())


@router.get("/compact")
async def get_tree_compact():
    return compact_payload(_tree(), _inventory())


@router.get("/plan")
async def get_node_plan(item: str):
    """План до узла от текущего инвентаря — то, что показывается по щелчку."""
    tree = _tree()
    kb = tree.kb
    if not kb.exists(item):
        raise HTTPException(status_code=404, detail="нет такого предмета")
    inventory = _inventory()
    plan = tree.plan_for(item, inventory)
    have = int(inventory.get(item, 0) or 0)
    return {
        "item": item,
        "label": kb.label(item) or item,
        "have": have,
        "feasible": plan.feasible,
        "steps": len(plan.actions()),
        "plan_tree": render_tree(plan, kb),
        "plan_summary": render_summary(plan, kb),
        "alternatives": render_alternatives(plan, kb),
    }


# --- иконки ---

_jar: Optional[zipfile.ZipFile] = None
_jar_checked = False
_icon_cache: Dict[str, Optional[bytes]] = {}

# Где искать изображение предмета: у предметов своя текстура, у блоков — грань блока.
ICON_PATTERNS = ("item/{name}", "item/{name}_00", "block/{name}", "block/{name}_front",
                 "block/{name}_side", "block/{name}_top")


def _game_jar() -> Optional[zipfile.ZipFile]:
    global _jar, _jar_checked
    if not _jar_checked:
        _jar_checked = True
        path = Path.home() / ".gradle" / "caches" / "fabric-loom" / GAME_VERSION / "minecraft-client.jar"
        if path.exists():
            try:
                _jar = zipfile.ZipFile(path)
            except (OSError, zipfile.BadZipFile):
                _jar = None
    return _jar


def icon_bytes(name: str) -> Optional[bytes]:
    if name not in _icon_cache:
        data: Optional[bytes] = None
        jar = _game_jar()
        if jar is not None and name.replace("_", "").isalnum():
            for pattern in ICON_PATTERNS:
                entry = f"assets/minecraft/textures/{pattern.format(name=name)}.png"
                try:
                    data = jar.read(entry)
                    break
                except KeyError:
                    continue
        _icon_cache[name] = data
    return _icon_cache[name]


@router.get("/icon/{name}.png")
async def get_icon(name: str):
    # Иконки берутся из jar Minecraft: для другой игры одноимённая картинка была бы чужой.
    if _world() is not None:
        raise HTTPException(status_code=404, detail="иконок для этой игры нет")
    data = icon_bytes(name)
    if data is None:
        raise HTTPException(status_code=404, detail="иконки нет")
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})
