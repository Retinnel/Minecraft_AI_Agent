from pydantic import BaseModel, Field
from typing import List, Optional


class GroqAdvice(BaseModel):
    """Response model for LLM-generated RP response."""

    response: str
    confidence: float
    level: str  # INFO, WARNING, CRITICAL
    threats: list = Field(default_factory=list)
    source: str = "groq"  # Indicates this came from Groq LLM
    fallback: bool = False  # True if using fallback response due to API error
    mode: str = "chat"  # chat | action
    execute: bool = False
    command: Optional[str] = None
    action_type: Optional[str] = None
    item_id: Optional[str] = None
    item_count: Optional[int] = None
    entity_id: Optional[str] = None
    entity_count: Optional[int] = None
    error: Optional[str] = None
    # Поля гибридного конвейера. Мод, собранный до их появления, их просто не читает.
    outcome: Optional[str] = None              # act | clarify | reject | none
    assistant_mode: Optional[str] = None       # creative | survival
    plan_tree: Optional[str] = None            # дерево плана для оверлея
    plan_summary: Optional[str] = None
    plan_alternatives: Optional[str] = None    # отвергнутые варианты и причины
    explanation: List[str] = Field(default_factory=list)  # цепочка применённых правил
    options: List[str] = Field(default_factory=list)      # варианты уточняющего вопроса
    campaign: Optional[dict] = None            # кампания: персонаж, этап, прогресс — для оверлея
    sources: List[dict] = Field(default_factory=list)     # откуда взят ответ, найденный в сети
    game: Optional[str] = None                 # игра, о которой идёт разговор
    screen: Optional[dict] = None              # что ассистент видел на экране при ответе
    memory: List[dict] = Field(default_factory=list)      # факты памяти, на которых построен ответ
    autopilot: Optional[dict] = None           # «Сделай сам»: {run_id, status, step, total}


class ChatMessage(BaseModel):
    """Chat message from player."""
    text: str
    # Сведения самой игры на момент реплики; их присылает мод. Точнее распознавания экрана:
    # игра знает, на какой блок смотрит игрок и что у него в руке.
    look_at: Optional[str] = None     # идентификатор блока или существа под прицелом
    look_kind: Optional[str] = None   # block | entity
    held: Optional[str] = None        # идентификатор предмета в руке
    # Для «Сделай сам»: одиночная игра не в LAN, есть ли в моде автопилот и Baritone.
    singleplayer: Optional[bool] = None
    autopilot_available: Optional[bool] = None
    baritone: Optional[bool] = None
