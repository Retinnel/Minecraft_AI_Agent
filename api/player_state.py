from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List

from api.catalog_labels import get_entity_display_name_ru, get_item_display_name_ru
from fastapi import APIRouter


router = APIRouter(prefix="/api/player-state", tags=["player-state"])

MAX_RECENT_KILLS = 50

_player_state: Dict[str, Any] = {
    "kill_counts": {},
    "recent_kills": [],
    "inventory": {
        "counts": {},
        "hotbar": [],
        "armor": [],
        "offhand": [],
    },
    "updated_at": None,
    "last_kill_at": None,
    "last_inventory_at": None,
    # Причина последней смерти и инвентарь на её момент: после смерти он пустеет, а для
    # разбора нужен именно тот, с которым игрок погиб.
    "last_death": None,
}


def reset_player_state() -> None:
    _player_state["kill_counts"] = {}
    _player_state["recent_kills"] = []
    _player_state["inventory"] = {
        "counts": {},
        "hotbar": [],
        "armor": [],
        "offhand": [],
    }
    _player_state["updated_at"] = None
    _player_state["last_kill_at"] = None
    _player_state["last_inventory_at"] = None
    _player_state["last_death"] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): val for key, val in value.items()}


def _normalize_entry_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("item_id") or entry.get("entity_id") or "").strip()
        count = entry.get("count")
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        normalized.append(
            {
                "slot": str(entry.get("slot") or "").strip() or None,
                "item_id": item_id or None,
                "display_name": str(entry.get("display_name") or "").strip() or get_item_display_name_ru(item_id) or None,
                "count": count,
            }
        )
    return normalized


def _localized_count_entries(counts: Dict[str, Any], *, kind: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for resource_id, raw_count in counts.items():
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue

        display_name = (
            get_entity_display_name_ru(resource_id)
            if kind == "entity"
            else get_item_display_name_ru(resource_id)
        ) or resource_id
        entries.append(
            {
                "id": resource_id,
                "display_name": display_name,
                "count": count,
            }
        )

    entries.sort(key=lambda item: (-int(item["count"]), str(item["display_name"])))
    return entries


def apply_log_to_player_state(entry: Any) -> None:
    event_type = getattr(entry, "event_type", None)
    event_data = getattr(entry, "event_data", None) or {}
    timestamp = getattr(entry, "timestamp", None)
    timestamp_text = timestamp.isoformat() if timestamp else _now_iso()

    if event_type == "mob_kill":
        entity_id = str(event_data.get("entity_id") or "").strip()
        entity_name = str(event_data.get("entity_name") or entity_id).strip()
        try:
            count_delta = int(event_data.get("count_delta") or 1)
        except (TypeError, ValueError):
            count_delta = 1
        if entity_id:
            current = int(_player_state["kill_counts"].get(entity_id, 0))
            _player_state["kill_counts"][entity_id] = current + max(1, count_delta)
            _player_state["recent_kills"].append(
                {
                    "entity_id": entity_id,
                    "entity_name": entity_name,
                    "count_delta": max(1, count_delta),
                    "timestamp": timestamp_text,
                }
            )
            if len(_player_state["recent_kills"]) > MAX_RECENT_KILLS:
                del _player_state["recent_kills"][:-MAX_RECENT_KILLS]
            _player_state["last_kill_at"] = timestamp_text
            _player_state["updated_at"] = timestamp_text

    if event_type == "death":
        _player_state["last_death"] = {
            "cause": str(event_data.get("cause") or "unknown").strip(),
            "inventory": deepcopy(_player_state.get("inventory") or {}),
            "timestamp": timestamp_text,
        }
        _player_state["updated_at"] = timestamp_text

    if event_type == "inventory_snapshot":
        inventory = {
            "counts": _normalize_mapping(event_data.get("counts")),
            "hotbar": _normalize_entry_list(event_data.get("hotbar")),
            "armor": _normalize_entry_list(event_data.get("armor")),
            "offhand": _normalize_entry_list(event_data.get("offhand")),
        }
        _player_state["inventory"] = inventory
        _player_state["last_inventory_at"] = timestamp_text
        _player_state["updated_at"] = timestamp_text


def get_player_state() -> Dict[str, Any]:
    snapshot = deepcopy(_player_state)
    snapshot["kill_total"] = sum(int(value) for value in snapshot["kill_counts"].values())
    snapshot["kill_count_entries"] = _localized_count_entries(snapshot.get("kill_counts") or {}, kind="entity")
    inventory = snapshot.get("inventory") or {}
    inventory["count_entries"] = _localized_count_entries(inventory.get("counts") or {}, kind="item")
    snapshot["inventory"] = inventory

    for entry in snapshot.get("recent_kills", []):
        entity_id = str(entry.get("entity_id") or "").strip()
        if entity_id and not entry.get("entity_name"):
            entry["entity_name"] = get_entity_display_name_ru(entity_id) or entity_id
    return snapshot


def _top_inventory_counts(counts: Dict[str, Any], limit: int = 8) -> List[str]:
    items = []
    for item_id, raw_count in counts.items():
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            items.append((item_id, count))
    items.sort(key=lambda item: (-item[1], item[0]))
    return [f"{get_item_display_name_ru(item_id) or item_id} x{count}" for item_id, count in items[: max(1, limit)]]


def _format_slot_entries(entries: List[Dict[str, Any]]) -> str:
    parts = []
    for entry in entries:
        item_id = entry.get("item_id")
        if not item_id:
            continue
        display_name = entry.get("display_name") or item_id
        count = entry.get("count") or 0
        slot = entry.get("slot")
        prefix = f"{slot}: " if slot else ""
        suffix = f" x{count}" if count else ""
        parts.append(f"{prefix}{display_name}{suffix}")
    return ", ".join(parts) if parts else "empty"


def get_player_state_prompt_context() -> str:
    state = get_player_state()
    lines = []

    kill_counts = state.get("kill_counts") or {}
    if kill_counts:
        top_kills = sorted(kill_counts.items(), key=lambda item: (-int(item[1]), item[0]))[:5]
        lines.append("Tracked kills:")
        lines.extend([f"- {get_entity_display_name_ru(entity_id) or entity_id}: {count}" for entity_id, count in top_kills])

    inventory = state.get("inventory") or {}
    counts = inventory.get("counts") or {}
    top_items = _top_inventory_counts(counts)
    if top_items:
        lines.append("Inventory summary:")
        lines.extend([f"- {item}" for item in top_items])

    hotbar_text = _format_slot_entries(inventory.get("hotbar") or [])
    armor_text = _format_slot_entries(inventory.get("armor") or [])
    offhand_text = _format_slot_entries(inventory.get("offhand") or [])
    if hotbar_text != "empty" or armor_text != "empty" or offhand_text != "empty":
        lines.append(f"Hotbar: {hotbar_text}")
        lines.append(f"Armor: {armor_text}")
        lines.append(f"Offhand: {offhand_text}")

    return "\n".join(lines) if lines else "No tracked player state."


@router.get("/")
async def get_player_state_endpoint():
    return get_player_state()


@router.get("/kills")
async def get_player_kills_endpoint():
    state = get_player_state()
    return {
        "kill_counts": state.get("kill_counts", {}),
        "recent_kills": state.get("recent_kills", []),
        "kill_total": state.get("kill_total", 0),
        "last_kill_at": state.get("last_kill_at"),
    }


@router.get("/inventory")
async def get_player_inventory_endpoint():
    state = get_player_state()
    return {
        "inventory": state.get("inventory", {}),
        "last_inventory_at": state.get("last_inventory_at"),
    }
