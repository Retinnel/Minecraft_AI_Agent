"""Распознавание речи для голоса в игре.

Мод по клавише записывает микрофон и присылает звук сюда; назад уходит текст, и дальше он
идёт тем же путём, что набранная реплика, — `/api/rp/chat`, конвейер, валидатор. Голос — ещё
один способ ввести реплику, а не ещё один путь к действию: распознанный текст не получает
прав, которых нет у набранного.

Движок — Speech to Text провайдера DeepSeek (`POST /v1/tools/asr`, тот же ключ, что у
модели): 16-битный WAV целиком в одном запросе, в ответ — текст. Реплика игрока — секунды,
поэтому запись длиннее предела не отправляется вовсе.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.request
import uuid
import wave
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api import keys
from api.groq_client import DEEPSEEK_KEY_NAMES

router = APIRouter(prefix="/api/voice", tags=["voice"])

ASR_URL = "https://api.deepseek.com/v1/tools/asr"
# Реплика игрока короткая; минута звука в 16 кГц моно — около двух мегабайт.
MAX_SECONDS = 30.0
MIN_SECONDS = 0.3
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 30.0


class SpeechError(Exception):
    """Запись не годится или движок не ответил; `status` — код ответа точки."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def describe(audio: bytes) -> float:
    """Длительность записи в секундах; заодно проверка, что это 16-битный WAV."""
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            if source.getsampwidth() != 2:
                raise SpeechError(400, "нужен 16-битный WAV")
            rate = source.getframerate()
            return source.getnframes() / rate if rate else 0.0
    except (wave.Error, EOFError):
        raise SpeechError(400, "это не WAV")


def _multipart(audio: bytes) -> Tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    head = (f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n").encode("ascii")
    return head + audio + f"\r\n--{boundary}--\r\n".encode("ascii"), boundary


def _post(audio: bytes, key: str) -> Dict[str, Any]:
    body, boundary = _multipart(audio)
    request = urllib.request.Request(ASR_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def transcribe(audio: bytes, post: Optional[Callable[[bytes, str], Dict[str, Any]]] = None,
               vocabulary: Optional[Any] = None) -> Dict[str, Any]:
    """Текст записи. `post` подменяется в тестах: к живому движку они не обращаются.

    vocabulary — словарь названий базы знаний: по нему распознанное возвращается к
    известным словам (`resolver/speech.py`); услышанное до правки остаётся в `heard`."""
    if not audio:
        raise SpeechError(400, "пустая запись")
    if len(audio) > MAX_BYTES:
        raise SpeechError(413, "запись слишком длинная")
    seconds = describe(audio)
    if seconds < MIN_SECONDS:
        raise SpeechError(400, "запись слишком короткая")
    if seconds > MAX_SECONDS:
        raise SpeechError(413, f"запись длиннее {int(MAX_SECONDS)} секунд")
    key = keys.get(*DEEPSEEK_KEY_NAMES)
    if not key:
        raise SpeechError(503, "распознавание речи не настроено: нет ключа DeepSeek")
    try:
        payload = (post or _post)(audio, key)
    except urllib.error.HTTPError as error:
        raise SpeechError(502, f"движок распознавания ответил {error.code}")
    except Exception as error:  # сеть, тайм-аут, разбор ответа
        raise SpeechError(502, f"движок распознавания недоступен: {type(error).__name__}")
    heard = str(payload.get("text") or "").strip()
    text = heard
    if vocabulary is not None and heard:
        from resolver.speech import repair
        text = repair(heard, vocabulary)
    return {"text": text, "heard": heard, "seconds": round(seconds, 2), "engine": "deepseek"}


def _vocabulary() -> Optional[Any]:
    """Словарь названий той базы знаний, по которой сейчас идёт разговор."""
    try:
        from api.advice import _assistant_pipeline
        return _assistant_pipeline().resolver.index.vocabulary
    except Exception:   # правка — удобство: без словаря текст уходит как услышан
        return None


@router.post("/transcribe")
async def transcribe_audio(request: Request):
    """Тело запроса — WAV как есть (`audio/wav`): моду так проще, чем собирать форму."""
    audio = await request.body()
    try:
        return await asyncio.to_thread(transcribe, audio, None, _vocabulary())
    except SpeechError as error:
        return JSONResponse(status_code=error.status, content={"detail": str(error)})
