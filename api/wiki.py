"""Поиск рецепта для вкладки «Рецепты» оверлея.

О каком предмете идёт речь, решает резолвер по онтологии, а не нечёткий поиск по
справочнику: справочник сам по себе превращал «меч» в деревянный меч, а «алмаз» — в
алмазную кирку. Справочник используется только как источник раскладки сетки для уже
определённого предмета; состав и способы получения берутся из базы знаний.

Языковая модель здесь не участвует. Раньше при отсутствии рецепта в справочнике его
придумывала модель, и это противоречило принципу системы: знания о мире не берутся из модели,
потому что она выдаёт правдоподобное, а не верное, — и деградирует первой при ослаблении.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from api.minecraft_recipes import search_recipe, validate_recipe_grid

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/wiki", tags=["wiki"])


def _grid_for(label: str) -> Optional[List[List[str]]]:
    """Раскладка сетки из справочника — только если он знает именно этот предмет."""
    recipe = search_recipe(label)
    if not recipe or str(recipe.get("name") or "").strip().lower() != label.strip().lower():
        return None
    grid = recipe.get("grid")
    return grid if validate_recipe_grid(grid or []) else None


def _describe(kb, thing: str) -> Optional[str]:
    """Как получить предмет: откуда добывается и из чего изготавливается."""
    from solver.render import STATION_RU

    parts: List[str] = []
    sources = [source for source in kb.sources_of(thing) if not source.rare]
    if sources:
        where = ", ".join(dict.fromkeys(kb.label(source.id) or source.id for source in sources[:3]))
        parts.append(f"Добывается: {where}.")

    recipes = kb.recipes_for(thing)
    if recipes:
        recipe = recipes[0]
        ingredients = []
        for ingredient in recipe.inputs:
            if not ingredient.alternatives:
                continue
            name = kb.label(ingredient.alternatives[0]) or ingredient.alternatives[0]
            if ingredient.is_choice:
                name += " (или аналог)"
            ingredients.append(f"{name} ×{ingredient.quantity}")
        station = STATION_RU.get(recipe.station, recipe.station)
        text = f"Изготавливается {station}: " + ", ".join(ingredients)
        if recipe.produces_count > 1:
            text += f" (выход {recipe.produces_count} шт.)"
        parts.append(text + ".")
    return " ".join(parts) or None


@router.get("/craft")
async def search_craft(query: Optional[str] = None) -> Dict[str, Any]:
    """Рецепт предмета: состав по базе знаний и, если известна, раскладка сетки."""
    from kb.graph import KnowledgeBase
    from resolver import AMBIGUOUS, resolve

    if not query or not query.strip():
        return {"status": "error", "message": "Пустой запрос."}
    query = query.strip()

    resolution = resolve(query, action="give_item")
    if resolution.status == AMBIGUOUS:
        names = ", ".join(candidate.label for candidate in resolution.candidates)
        return {"status": "error", "message": f"Уточни, какой именно: {names}."}
    if not resolution.ok:
        return {"status": "error", "message": f"«{query}» в игре нет."}

    kb = KnowledgeBase.shared()
    label = kb.label(resolution.thing) or resolution.thing
    description = _describe(kb, resolution.thing)
    if description is None:
        return {"status": "error",
                "message": f"Для «{label}» в данных игры нет ни рецепта, ни источника добычи."}

    grid = _grid_for(label)
    logger.info(f"Recipe for {query!r}: {resolution.thing}, сетка {'есть' if grid else 'нет'}")
    return {"status": "success", "source": "kb", "item_id": resolution.thing,
            "recipe": {"name": label, "description": description, "grid": grid}}
