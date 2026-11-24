"""Озвучка ответов ассистента.

Этот модуль — прослойка основного сервера: выбирает голос, хранит кэш озвученных реплик и
обращается к движку синтеза. Движков два, оба необязательные:

    voxcpm      локальный процесс `voice/server.py` (модель VoxCPM2 на видеокарте);
    elevenlabs  облачный синтез по ключу `ELEVENLABS_API_KEY`.

Порядок задаётся переменной `VOICE_ENGINES` (по умолчанию «voxcpm,elevenlabs»): берётся первый
движок, который ответил. Если не ответил ни один, точка отдаёт 503, и страница чата читает
ответ встроенным синтезатором браузера: голос — удобство, а не звено конвейера.

Голос выбирается по тому, кто говорит. Базовый голос — у ассистента; персонаж кампании
говорит своим, если он для него заведён в `voice/voices.json`. Озвучивается готовый текст
ответа; на решение он не влияет.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/voice", tags=["voice"])

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "voice_cache"
VOICES_PATH = ROOT / "voice" / "voices.json"
VOICE_SERVER_URL = os.environ.get("VOICE_SERVER_URL", "http://127.0.0.1:8001")
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/"

VOXCPM = "voxcpm"
ELEVENLABS = "elevenlabs"
DEFAULT_ENGINES = f"{VOXCPM},{ELEVENLABS}"
MEDIA = {VOXCPM: ("wav", "audio/wav"), ELEVENLABS: ("mp3", "audio/mpeg")}

DEFAULT_VOICE = "assistant"
# Длинный текст в речь не превращается: строка чата и так ограничена, а синтез идёт
# примерно со скоростью речи (и в облаке оплачивается посимвольно).
MAX_TEXT = 400
SYNTHESIS_TIMEOUT = 90.0
STATUS_TIMEOUT = 0.6


class SpeakRequest(BaseModel):
    text: str
    voice: Optional[str] = None      # ключ голоса; без него выбирается по говорящему
    source: Optional[str] = None     # поле source ответа: кто его сформулировал


# --- голоса ---

def voices_config() -> Dict[str, Any]:
    try:
        return json.loads(VOICES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"voices": {DEFAULT_VOICE: {}}}


def known_voices() -> List[str]:
    return sorted(voices_config().get("voices") or {DEFAULT_VOICE: {}})


def persona_names() -> dict:
    """Имя персонажа → ключ пресета: реплики кампании подписаны именем говорящего."""
    try:
        from orchestrator.narrator import load_personas
        return {str(spec.get("name")): key for key, spec in load_personas().items()
                if spec.get("name")}
    except Exception:
        return {}


def pick_voice(voice: Optional[str], source: Optional[str], text: str = "") -> Tuple[str, str]:
    """Чьим голосом говорить и что именно произносить.

    Явно названный голос берётся как есть. Реплика, подписанная именем персонажа
    («Кузнец Торвальд: …»), звучит его голосом, если такой заведён, а подпись вслух не
    читается. Прочие реплики кампании — голосом персонажа идущей кампании. Всё остальное,
    и персонажи без своего голоса, — базовым голосом ассистента.
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


# --- движки ---

def engines() -> List[str]:
    """Движки в порядке предпочтения — из переменной окружения VOICE_ENGINES."""
    listed = [name.strip().lower() for name in
              os.environ.get("VOICE_ENGINES", DEFAULT_ENGINES).split(",")]
    return [name for name in listed if name in MEDIA]


def _elevenlabs_key() -> Optional[str]:
    from api import keys
    return keys.get("ELEVENLABS_API_KEY")


def _elevenlabs_voice(voice: str) -> Optional[str]:
    """Идентификатор голоса ElevenLabs для нашего голоса; у базового — запасной для всех."""
    config = voices_config().get("voices") or {}
    own = (config.get(voice) or {}).get("elevenlabs_voice_id")
    return own or (config.get(DEFAULT_VOICE) or {}).get("elevenlabs_voice_id") or None


def _synthesize(voice: str, text: str) -> bytes:
    """Локальный процесс синтеза; бросает OSError, если он недоступен."""
    body = json.dumps({"text": text, "voice": voice}).encode("utf-8")
    request = urllib.request.Request(
        VOICE_SERVER_URL + "/speak", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=SYNTHESIS_TIMEOUT) as response:
        return response.read()


def _synthesize_elevenlabs(voice: str, text: str) -> bytes:
    """Облачный синтез ElevenLabs; бросает OSError без ключа, голоса или связи.

    В облако уходит только текст готовой реплики ассистента — ни реплика игрока, ни
    состояние игры туда не передаются.
    """
    key, voice_id = _elevenlabs_key(), _elevenlabs_voice(voice)
    if not key or not voice_id:
        raise OSError("ElevenLabs не настроен: нужен ключ и идентификатор голоса")
    model = voices_config().get("elevenlabs_model") or "eleven_flash_v2_5"
    body = json.dumps({"text": text, "model_id": model, "language_code": "ru"}).encode("utf-8")
    request = urllib.request.Request(
        ELEVENLABS_URL + voice_id + "?output_format=mp3_44100_128", data=body,
        headers={"Content-Type": "application/json", "xi-api-key": key,
                 "Accept": "audio/mpeg"})
    with urllib.request.urlopen(request, timeout=SYNTHESIS_TIMEOUT) as response:
        return response.read()


SYNTHESIZERS = {VOXCPM: lambda voice, text: _synthesize(voice, text),
                ELEVENLABS: lambda voice, text: _synthesize_elevenlabs(voice, text)}


def _server_alive() -> bool:
    try:
        with urllib.request.urlopen(VOICE_SERVER_URL + "/health", timeout=STATUS_TIMEOUT) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _engine_ready(engine: str) -> bool:
    if engine == VOXCPM:
        return _server_alive()
    return bool(_elevenlabs_key() and _elevenlabs_voice(DEFAULT_VOICE))


# --- кэш и синтез ---

def cache_path(engine: str, voice: str, text: str) -> Path:
    digest = hashlib.sha1(f"{voice}\n{text}".encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / f"{voice}-{digest}.{MEDIA[engine][0]}"


def speak_bytes(voice: str, text: str) -> Optional[Tuple[bytes, str, str]]:
    """Озвученная реплика: (звук, тип содержимого, движок) либо None, если синтеза нет.

    Сначала кэш любого из движков: озвученная заранее реплика отдаётся и тогда, когда сам
    движок уже недоступен. Затем движки по порядку.
    """
    order = engines()
    for engine in order:
        path = cache_path(engine, voice, text)
        if path.exists():
            return path.read_bytes(), MEDIA[engine][1], engine
    for engine in order:
        try:
            audio = SYNTHESIZERS[engine](voice, text)
        except (OSError, urllib.error.URLError, ValueError):
            continue
        if audio:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path(engine, voice, text).write_bytes(audio)
            return audio, MEDIA[engine][1], engine
    return None


@router.get("/status")
async def voice_status():
    order = engines()
    ready = [engine for engine in order if await asyncio.to_thread(_engine_ready, engine)]
    cached = sum(1 for path in CACHE_DIR.glob("*.*") if path.suffix in (".wav", ".mp3")) \
        if CACHE_DIR.exists() else 0
    config = voices_config().get("voices") or {}
    return {"available": bool(ready), "engine": ready[0] if ready else None,
            "engines": order, "ready": ready,
            "voices": [{"key": key, "label": (config.get(key) or {}).get("label") or key}
                       for key in known_voices()],
            "cached": cached}


@router.post("/speak")
async def voice_speak(request: SpeakRequest):
    text = clean(request.text)
    if not text:
        return JSONResponse(status_code=400, content={"error": "пустой текст"})
    voice, text = pick_voice(request.voice, request.source, text)
    if not text:
        return JSONResponse(status_code=400, content={"error": "пустой текст"})
    result = await asyncio.to_thread(speak_bytes, voice, text)
    if result is None:
        return JSONResponse(status_code=503, content={
            "error": "синтез речи не запущен", "voice": voice})
    audio, media_type, engine = result
    return Response(content=audio, media_type=media_type,
                    headers={"X-Voice": voice, "X-Voice-Engine": engine})
