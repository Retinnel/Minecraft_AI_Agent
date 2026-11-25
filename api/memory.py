"""HTTP-слой памяти мира.

    GET    /api/memory/?kind=&q=        факты текущего мира (для игры разговора)
    POST   /api/memory/observations     пакет наблюдений от сканера мода
    POST   /api/memory/containers       содержимое хранилища
    POST   /api/memory/note             заметка или отметка места
    DELETE /api/memory/{id}             забыть факт
    GET    /api/memory/scan-list        какие блоки сканировать, радиус и частота

Наблюдения мода (сканер, хранилища) принимает `memory.adapters.minecraft`, его точки
подключены здесь же. Все значения — координаты, количества — приходят от мода или из
состояния игрока; модель к этому слою доступа не имеет.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException

import memory
from memory.model import KINDS, PLAYER
from memory.recall import describe, recall

router = APIRouter(prefix="/api/memory", tags=["memory"])

MAX_LISTED = 200


def _session_game() -> str:
    from orchestrator import session as sessions
    return sessions.shared().game


def label_of() -> Callable[[str], Optional[str]]:
    """Названия сущностей из базы знаний; без базы — идентификатор как есть."""
    try:
        from api.advice import _assistant_pipeline
        kb = _assistant_pipeline().kb
    except Exception:
        return lambda thing: None
    return lambda thing: kb.label(thing) if thing else None


def fact_view(fact, *, origin=None, names=None) -> Dict[str, Any]:
    view = fact.to_dict()
    view["description"] = describe(fact, origin=origin, label_of=names)
    return view


@router.get("/")
async def list_memory(kind: Optional[str] = None, q: Optional[str] = None,
                      limit: int = MAX_LISTED) -> Dict[str, Any]:
    from api.player_state import current_position

    game = _session_game()
    store = memory.shared(game)
    if kind and kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"Вид факта должен быть одним из: {', '.join(KINDS)}")
    origin = current_position()
    names = label_of()
    limit = max(1, min(int(limit), MAX_LISTED))
    if q:
        found = [match.fact for match in recall(store, q, kinds=[kind] if kind else None,
                                                position=origin, limit=limit, label_of=names)]
    else:
        found = sorted(store.facts(kind), key=lambda fact: fact.last_seen, reverse=True)[:limit]
    return {"game": game, "world": store.world, "total": len(store.facts()),
            "facts": [fact_view(fact, origin=origin, names=names) for fact in found]}


def _kb():
    try:
        from api.advice import _assistant_pipeline
        return _assistant_pipeline().kb
    except Exception:
        return None


def _minecraft_store():
    """Наблюдения мода относятся к Minecraft, о чём бы ни шёл разговор."""
    return memory.shared(memory.DEFAULT_GAME)


@router.get("/scan-list")
async def get_scan_list() -> Dict[str, Any]:
    from memory.adapters.minecraft import scan_list
    from memory.config import settings
    return scan_list(settings(), _kb())


@router.post("/observations")
async def post_observations(payload: Dict[str, Any]) -> Dict[str, Any]:
    from memory.adapters.minecraft import observations
    from memory.config import settings
    if not settings()["enabled"]:
        return {"ok": False, "disabled": True}
    store = _minecraft_store()
    result = observations(store, payload if isinstance(payload, dict) else {}, _kb())
    if result["added"] or result["updated"] or result["stale"]:
        store.save()
    return {"ok": True, **result}


@router.post("/containers")
async def post_container(payload: Dict[str, Any]) -> Dict[str, Any]:
    from memory.adapters.minecraft import container
    from memory.config import settings
    if not settings()["enabled"]:
        return {"ok": False, "disabled": True}
    store = _minecraft_store()
    fact = container(store, payload if isinstance(payload, dict) else {}, _kb())
    if fact is None:
        raise HTTPException(status_code=400, detail="Не хранилище или нет координат.")
    store.save()
    return {"ok": True, "fact": fact_view(fact, names=label_of())}


@router.post("/note")
async def add_note(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Заметка со страницы чата. `here: true` отмечает место по последней позиции игрока."""
    from api.player_state import current_position

    label = str(payload.get("label") or "").strip()
    text = str(payload.get("text") or "").strip() or None
    if not label and not text:
        raise HTTPException(status_code=400, detail="Нечего запомнить: нет ни названия, ни текста.")
    game = _session_game()
    position = current_position() if payload.get("here") else None
    if payload.get("here") and position is None:
        raise HTTPException(status_code=409, detail="Позиция игрока неизвестна: мод её ещё не присылал.")
    store = memory.shared(game)
    fact = store.note(label or text, text, position=position, source=PLAYER)
    store.save()
    return {"ok": True, "fact": fact_view(fact, origin=current_position(), names=label_of())}


@router.delete("/{fact_id}")
async def forget_fact(fact_id: str) -> Dict[str, Any]:
    store = memory.shared(_session_game())
    fact = store.forget(fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail="Такого факта нет или он уже забыт.")
    store.save()
    return {"ok": True, "id": fact.id}
