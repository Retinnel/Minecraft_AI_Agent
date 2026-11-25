from fastapi import APIRouter
from models.log_entry import Settings
import json
from pathlib import Path

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Загрузка из файла или дефолт
settings_file = "settings.json"
_settings_path = Path(settings_file)

try:
    if _settings_path.exists():
        current_settings = Settings(**json.loads(_settings_path.read_text(encoding="utf-8")))
    else:
        current_settings = Settings()
except Exception:
    current_settings = Settings()

@router.get("/", response_model=Settings)
async def get_settings():
    return current_settings

@router.put("/", response_model=Settings)
async def update_settings(settings: Settings):
    global current_settings
    # Страница настроек раздела памяти не знает: без него сохраняется прежний.
    if settings.memory is None:
        settings.memory = current_settings.memory
    if settings.autopilot is None:
        settings.autopilot = current_settings.autopilot
    current_settings = settings
    # Сохранение в файл
    payload = settings.dict() if hasattr(settings, "dict") else settings.model_dump()
    _settings_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return current_settings
