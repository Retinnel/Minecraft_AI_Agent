import asyncio

from fastapi import APIRouter, WebSocket
from fastapi.encoders import jsonable_encoder
from starlette.websockets import WebSocketDisconnect
from typing import List
from api.catalog_labels import replace_resource_ids_with_labels
from models.log_entry import LogEntry
from api.challenges import refresh_challenge_progress
from api.player_state import apply_log_to_player_state

router = APIRouter(prefix="/api/logs", tags=["logs"])

logs_db: List[LogEntry] = []  # Временное хранилище (заменить на Redis/Postgres)

@router.get("/", response_model=List[LogEntry])
async def get_logs(limit: int = 100, level: str = None):
    filtered = logs_db
    if level:
        filtered = [log for log in filtered if log.level == level]
    return filtered[-limit:]

@router.websocket("/ws")
async def logs_websocket(websocket: WebSocket):
    await websocket.accept()
    try:
        last_sent = 0
        while True:
            # Отправка новых логов в реальном времени
            while last_sent < len(logs_db):
                await websocket.send_json(jsonable_encoder(logs_db[last_sent]))
                last_sent += 1

            await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        return

@router.post("/")
async def add_log(entry: LogEntry):
    if not entry.message:
        if entry.event_type:
            if entry.event_data:
                entry.message = f"{entry.event_type}: {entry.event_data}"
            else:
                entry.message = entry.event_type
        else:
            entry.message = "(no message)"
    entry.message = replace_resource_ids_with_labels(entry.message)
    logs_db.append(entry)
    apply_log_to_player_state(entry)
    refresh_challenge_progress()
    if entry.event_type in ("mob_kill", "death"):
        # Счётчики убийств и последняя гибель из игры не восстанавливаются.
        from api import worlds
        worlds.save_player()
    if len(logs_db) > 10000:  # Ограничение
        logs_db.pop(0)
    return {"status": "added"}
