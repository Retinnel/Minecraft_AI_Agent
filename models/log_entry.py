from pydantic import BaseModel, Field
from typing import Any, Dict, Optional
from datetime import datetime, timezone

class LogEntry(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    level: str  # INFO, WARNING, ERROR
    message: Optional[str] = None
    event_type: Optional[str] = None
    event_data: Optional[Dict[str, Any]] = None
    player_health: Optional[float] = None
    threats_detected: Optional[list] = None
    advice_given: Optional[str] = None
    confidence: Optional[float] = None

class Settings(BaseModel):
    analysis_interval: int = 500  # ms
    threat_threshold: float = 0.7
    enable_voice: bool = False
    max_threats_display: int = 3
    # Раздел памяти мира: радиус и частота сканера, лимит фактов (memory/config.py).
    # Хранится здесь, чтобы сохранение настроек со страницы его не стирало.
    memory: Optional[Dict[str, Any]] = None
    # Раздел «Сделай сам»: включён ли, пределы, тайм-ауты (api/autopilot.py). По умолчанию выключен.
    autopilot: Optional[Dict[str, Any]] = None
    # Помощник — отдельный игрок (companion/): имя, адрес мира, доставка, пределы (api/companion.py).
    companion: Optional[Dict[str, Any]] = None
