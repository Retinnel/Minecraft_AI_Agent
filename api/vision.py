"""HTTP-слой распознавания экрана: выбор окна, запуск наблюдателя, состояние, ручной снимок.

Наблюдатель работает в процессе сервера, который запущен на той же машине, что и игра.
Страница `/chat` выбирает окно игры из списка, запускает и останавливает съёмку и
показывает, что ассистент сейчас видит. Ручная загрузка снимка нужна для проверки без игры.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError

from vision import capture, watcher as watchers
from vision.scene import VisionUnavailable
from vision.watcher import AnticheatRunning

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vision", tags=["vision"])

# Окна, которые заведомо не игра: в списке выбора они только мешают.
HIDDEN_WINDOWS = ("Program Manager", "Microsoft Text Input Application", "Settings")


def _watcher() -> watchers.ScreenWatcher:
    watcher = watchers.shared()
    if watcher.on_game is None:
        watcher.on_game = _follow_game
    return watcher


def _follow_game(game: str) -> None:
    """Игра, которую несколько кадров подряд назвали одинаково, становится игрой сессии."""
    from api.advice import MINECRAFT_NAMES, _assistant_session
    from orchestrator.session import DEFAULT_GAME

    session = _assistant_session()
    name = DEFAULT_GAME if MINECRAFT_NAMES.search(game) else game
    if name.lower() != session.game.lower() and session.set_game(name):
        logger.info(f"[Vision] Игра определена по экрану: {name}")


@router.get("/windows")
async def list_windows() -> Dict[str, Any]:
    titles = [window.title for window in capture.list_windows()
              if window.title not in HIDDEN_WINDOWS]
    return {"supported": capture.IS_WINDOWS, "windows": sorted(set(titles), key=str.lower)}


@router.post("/start")
async def start(payload: Dict[str, Any]) -> Dict[str, Any]:
    window = str(payload.get("window") or "").strip()
    if not window:
        raise HTTPException(status_code=400, detail="Не выбрано окно игры.")
    watcher = _watcher()
    if not watcher.model.available:
        raise HTTPException(status_code=503, detail="Нет ключа DEEPSEEK_API: модель зрения недоступна.")
    try:
        watcher.start(window, interval=float(payload.get("interval") or watchers.DEFAULT_INTERVAL),
                      mode=str(payload.get("mode") or watchers.CONTINUOUS),
                      follow_game=bool(payload.get("follow_game", True)),
                      save=bool(payload.get("save", False)))
    except AnticheatRunning as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return watcher.snapshot()


@router.post("/stop")
async def stop() -> Dict[str, Any]:
    watcher = _watcher()
    watcher.stop()
    return watcher.snapshot()


@router.get("/state")
async def state() -> Dict[str, Any]:
    return _watcher().snapshot()


@router.get("/frame.jpg")
async def frame() -> Response:
    data = _watcher().frame_jpeg()
    if data is None:
        raise HTTPException(status_code=404, detail="Кадров ещё нет.")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/frame")
async def upload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Снимок из файла: data URL или base64 в поле image. Разбирается сразу."""
    raw = str(payload.get("image") or "")
    if "," in raw and raw.startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        image = Image.open(io.BytesIO(base64.b64decode(raw, validate=True)))
        image.load()
    except (binascii.Error, ValueError, UnidentifiedImageError, OSError):
        raise HTTPException(status_code=400, detail="Не удалось прочитать изображение.")
    try:
        description = _watcher().upload(image)
    except VisionUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error))
    return {"facts": description.facts.to_dict(), "elapsed_ms": round(description.elapsed_ms),
            "usage": description.usage}
