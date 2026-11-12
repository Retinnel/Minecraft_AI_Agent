"""Озвучка ответов ассистента.

Сам синтез живёт в отдельном процессе (`voice/server.py`, модель VoxCPM2 на видеокарте).
Этот модуль — прослойка основного сервера: выбирает голос, хранит кэш озвученных реплик и
ходит в процесс синтеза. Если процесс не запущен, отвечает 503, и страница чата переходит
на встроенный синтезатор браузера: голос — удобство, а не звено конвейера.

Голос выбирается по тому, кто говорит: реплики кампании — голосом её персонажа, остальные —
голосом ассистента. Озвучивается готовый текст ответа; на решение он не влияет.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/voice", tags=["voice"])

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "voice_cache"
VOICES_PATH = ROOT / "voice" / "voices.json"
VOICE_SERVER_URL = os.environ.get("VOICE_SERVER_URL", "http://127.0.0.1:8001")

DEFAULT_VOICE = "assistant"
# Длинный текст в речь не превращается: строка чата и так ограничена, а синтез идёт
# примерно со скоростью речи.
MAX_TEXT = 400
SYNTHESIS_TIMEOUT = 90.0
STATUS_TIMEOUT = 0.6


class SpeakRequest(BaseModel):
    text: str
    voice: Optional[str] = None      # ключ голоса; без него выбирается по сессии
    source: Optional[str] = None     # поле source ответа: кто его сформулировал


def known_voices() -> list:
    try:
        return sorted((json.loads(VOICES_PATH.read_text(encoding="utf-8")).get("voices") or {}))
    except (OSError, ValueError):
        return [DEFAULT_VOICE]


def persona_names() -> dict:
    """Имя персонажа → ключ пресета: реплики кампании подписаны именем говорящего."""
    try:
        from orchestrator.narrator import load_personas
        return {str(spec.get("name")): key for key, spec in load_personas().items()
                if spec.get("name")}
    except Exception:
        return {}


def pick_voice(voice: Optional[str], source: Optional[str], text: str = ""):
    """Чьим голосом говорить и что именно произносить.

    Явно названный голос берётся как есть. Реплика, подписанная именем персонажа
    («Кузнец Торвальд: …»), звучит его голосом, а подпись вслух не читается. Прочие реплики
    кампании — голосом персонажа идущей кампании, всё остальное — голосом ассистента.
    """
    voices = known_voices()
    for name, key in persona_names().items():
        prefix = f"{name}:"
        if text.startswith(prefix):
            spoken = text[len(prefix):].strip()
            return (voice if voice in voices else key if key in voices else DEFAULT_VOICE), spoken
    if voice in voices:
        return voice, text
    if str(source or "").startswith("campaign"):
        from api import campaign as campaigns
        persona = (campaigns.get_active_campaign() or {}).get("persona")
        if persona in voices:
            return persona, text
    return DEFAULT_VOICE, text


def clean(text: str) -> str:
    text = " ".join((text or "").split())
    return text[:MAX_TEXT]


def cache_path(voice: str, text: str) -> Path:
    digest = hashlib.sha1(f"{voice}\n{text}".encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / f"{voice}-{digest}.wav"


def _synthesize(voice: str, text: str) -> bytes:
    """Обращение к процессу синтеза; бросает OSError, если он недоступен."""
    body = json.dumps({"text": text, "voice": voice}).encode("utf-8")
    request = urllib.request.Request(
        VOICE_SERVER_URL + "/speak", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=SYNTHESIS_TIMEOUT) as response:
        return response.read()


def _server_alive() -> bool:
    try:
        with urllib.request.urlopen(VOICE_SERVER_URL + "/health", timeout=STATUS_TIMEOUT) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def speak_bytes(voice: str, text: str) -> Optional[bytes]:
    """Озвученная реплика: из кэша либо от процесса синтеза; None, если синтез недоступен."""
    path = cache_path(voice, text)
    if path.exists():
        return path.read_bytes()
    try:
        audio = _synthesize(voice, text)
    except (OSError, urllib.error.URLError, ValueError):
        return None
    if audio:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
    return audio or None


@router.get("/status")
async def voice_status():
    alive = await asyncio.to_thread(_server_alive)
    cached = len(list(CACHE_DIR.glob("*.wav"))) if CACHE_DIR.exists() else 0
    return {"available": alive, "engine": "VoxCPM2" if alive else None,
            "voices": known_voices(), "cached": cached}


@router.post("/speak")
async def voice_speak(request: SpeakRequest):
    text = clean(request.text)
    if not text:
        return JSONResponse(status_code=400, content={"error": "пустой текст"})
    voice, text = pick_voice(request.voice, request.source, text)
    if not text:
        return JSONResponse(status_code=400, content={"error": "пустой текст"})
    audio = await asyncio.to_thread(speak_bytes, voice, text)
    if audio is None:
        return JSONResponse(status_code=503, content={
            "error": "синтез речи не запущен", "voice": voice})
    return Response(content=audio, media_type="audio/wav", headers={"X-Voice": voice})
