"""Сервер синтеза речи на VoxCPM2 — отдельный процесс в отдельном окружении.

Модели нужен Python 3.10–3.12 и torch с CUDA, основному серверу — ни то, ни другое, поэтому
синтез вынесен в свой процесс и своё окружение `.venv-voice`:

    py -3.10 -m venv .venv-voice
    .venv-voice/Scripts/python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
    .venv-voice/Scripts/python.exe -m pip install -r requirements-voice.txt
    .venv-voice/Scripts/python.exe -m uvicorn voice.server:app --host 127.0.0.1 --port 8001

Основной сервер обращается сюда через `api/voice.py` и работает без него: тогда страница
чата озвучивает ответы встроенным синтезатором браузера.

Голос персонажа задаётся описанием (`voice/voices.json`). По описанию один раз создаётся
образец, дальше реплики озвучиваются по образцу — тембр остаётся тем же.
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
VOICES_PATH = Path(__file__).resolve().parent / "voices.json"
REFERENCE_DIR = ROOT / "data" / "voice_cache" / "reference"
MODEL_ID = "openbmb/VoxCPM2"
DEFAULT_VOICE = "assistant"

app = FastAPI(title="Voice synthesis")

_model = None
_lock = threading.Lock()          # видеокарта одна: реплики синтезируются по очереди


def _config() -> Dict[str, Any]:
    return json.loads(VOICES_PATH.read_text(encoding="utf-8"))


def _load():
    global _model
    if _model is None:
        from voxcpm import VoxCPM
        _model = VoxCPM.from_pretrained(MODEL_ID, load_denoiser=False)
    return _model


def _reference(voice: str) -> Path:
    """Образец голоса: создаётся по описанию при первом обращении и переиспользуется."""
    config = _config()
    spec = (config.get("voices") or {}).get(voice)
    if spec is None:
        voice, spec = DEFAULT_VOICE, config["voices"][DEFAULT_VOICE]
    path = REFERENCE_DIR / f"{voice}.wav"
    if not path.exists():
        model = _load()
        text = f"({spec['description']}){config['sample_text']}"
        wav = model.generate(text=text, cfg_value=2.0, inference_timesteps=10,
                             seed=int(spec.get("seed", 42)))
        REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
        sf.write(path, wav, model.tts_model.sample_rate)
    return path


class SpeakRequest(BaseModel):
    text: str
    voice: Optional[str] = None


@app.get("/health")
def health():
    return {"ok": True, "loaded": _model is not None, "model": MODEL_ID,
            "voices": sorted((_config().get("voices") or {}).keys())}


@app.post("/speak")
def speak(request: SpeakRequest):
    text = " ".join((request.text or "").split())
    if not text:
        raise HTTPException(status_code=400, detail="пустой текст")
    with _lock:
        started = time.time()
        model = _load()
        reference = _reference(request.voice or DEFAULT_VOICE)
        wav = model.generate(text=text, reference_wav_path=str(reference),
                             cfg_value=2.0, inference_timesteps=10, seed=7)
        rate = model.tts_model.sample_rate
        buffer = io.BytesIO()
        sf.write(buffer, wav, rate, format="WAV", subtype="PCM_16")
        spent = time.time() - started
    return Response(content=buffer.getvalue(), media_type="audio/wav",
                    headers={"X-Audio-Seconds": f"{len(wav) / rate:.2f}",
                             "X-Synthesis-Seconds": f"{spent:.2f}"})
