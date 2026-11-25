"""Упрощённая игра для проверок автопилота: исполняет задачи мода над инвентарём.

Не повторяет Minecraft целиком — только то, что нужно циклу «задача → отчёт → пересчёт»:
добыча кладёт предмет, крафт и плавка расходуют состав по рецепту из базы знаний,
станция ставится из инвентаря. Неверная задача (нет станции, нет состава) — ошибка теста.
"""

from typing import Dict, List, Optional


class GameError(AssertionError):
    pass


class SimGame:
    def __init__(self, kb, inventory: Optional[Dict[str, int]] = None,
                 chest: Optional[Dict[str, int]] = None) -> None:
        self.kb = kb
        self.inventory: Dict[str, int] = dict(inventory or {})
        self.chest: Dict[str, int] = dict(chest or {})
        self.stations: List[str] = []
        self.done: List[dict] = []

    def _take(self, item: str, count: int) -> None:
        if self.inventory.get(item, 0) < count:
            raise GameError(f"нет {item} ×{count} (есть {self.inventory.get(item, 0)})")
        self.inventory[item] -= count
        if not self.inventory[item]:
            del self.inventory[item]

    def _add(self, item: str, count: int) -> None:
        self.inventory[item] = self.inventory.get(item, 0) + count

    def _recipe(self, task: dict):
        for recipe in self.kb.recipes_for(task["item"]):
            if f"minecraft:{recipe.id}" == task["recipe_id"] or recipe.id == task["recipe_id"]:
                return recipe
        raise GameError(f"нет рецепта {task['recipe_id']}")

    def apply(self, task: dict) -> None:
        kind = task["kind"]
        if kind == "mine":
            if not task["blocks"]:
                raise GameError("добыча без блоков")
            self._add(task["item"], task["count"])
        elif kind == "place_station":
            self._take(task["item"], 1)
            self.stations.append(task["station"])
        elif kind == "take_from_container":
            if self.chest.get(task["item"], 0) < task["count"]:
                raise GameError(f"в сундуке нет {task['item']}")
            self.chest[task["item"]] -= task["count"]
            self._add(task["item"], task["count"])
        elif kind == "craft":
            if task["station"] != "inventory" and task["station"] not in self.stations:
                raise GameError(f"крафт без станции {task['station']}")
            recipe = self._recipe(task)
            for _ in range(task["runs"]):
                for ingredient in recipe.inputs:
                    choice = next((item for item in ingredient.alternatives
                                   if self.inventory.get(item, 0) >= ingredient.quantity), None)
                    if choice is None:
                        raise GameError(f"не хватает состава для {recipe.id}: {ingredient.alternatives[:3]}")
                    self._take(choice, ingredient.quantity)
                self._add(recipe.produces, recipe.produces_count)
        elif kind == "smelt":
            if "furnace" not in self.stations:
                raise GameError("плавка без печи")
            self._take(task["input"], task["count"])
            self._take(task["fuel"], task["fuel_count"])
            self._add(task["item"], task["count"])
        else:
            raise GameError(f"неизвестный вид задачи {kind}")
        self.done.append(task)
