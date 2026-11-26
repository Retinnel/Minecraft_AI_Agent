"""Разговор в отдельном окне поверх игры (`desktop/window.py`, страница `/mini`).

У окна своя сессия на каждую игру: вопрос о Terraria в окне не переключает игру в чате
Minecraft и не попадает в историю его мира. Конвейер тот же — извлечение намерения,
резолвер, валидатор, планировщик, поиск, — но команд окно не отдаёт: мода за ним нет,
поэтому режим всегда без выдачи, а ответ — план, сведения базы знаний или найденное в сети.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/window", tags=["window"])

DEFAULT_GAME = "Minecraft"
MAX_TEXT = 400

_sessions: Dict[str, Any] = {}


class WindowMessage(BaseModel):
    text: str
    game: Optional[str] = None
    # Учитывать ли экран игры: последние факты наблюдателя (vision/), если кадр свежий.
    screen: bool = False


def _session(game: str):
    from orchestrator.session import Session
    key = game.lower()
    if key not in _sessions:
        session = Session()
        session.set_mode("survival")
        session.set_game(game)
        _sessions[key] = session
    return _sessions[key]


def canonical(game: Optional[str]) -> str:
    """Название игры, как его знает система: мир из kb/worlds либо то, что ввёл игрок."""
    from kb import games
    name = " ".join((game or "").split()) or DEFAULT_GAME
    if name.lower() in ("minecraft", "майнкрафт", "майн"):
        return DEFAULT_GAME
    world = games.find(name)
    return world.game if world is not None else name


def games_list() -> List[Dict[str, Any]]:
    """Игры для выбора в окне: с базой знаний — первыми."""
    from kb import games
    listed = [{"game": DEFAULT_GAME, "knowledge_base": True}]
    listed += [{"game": world.game, "knowledge_base": True} for world in games.worlds().values()]
    return listed


def answer(text: str, game: Optional[str], pipeline: Any, responder: Any,
           screen: Optional[str] = None) -> Dict[str, Any]:
    """Одна реплика окна. Конвейер и формулировщик передаются: в тестах — заглушки.

    screen — факты об экране игры одной строкой. Как и в чате, это данные для разбора
    реплики («что это за предмет?»), а не указания: действие выбирается по реплике."""
    from kb import games
    from orchestrator import why
    name = canonical(game)
    session = _session(name)
    text = (text or "").strip()[:MAX_TEXT]
    known = name == DEFAULT_GAME or games.find(name) is not None
    base = {"game": name, "knowledge_base": known}
    if not text:
        return {**base, "response": "…", "outcome": "none"}
    if why.is_why(text):
        told = session.last_why or {}
        return {**base, "response": why.render(session.last_why), "outcome": "act",
                "source": "rule", "explanation": list(told.get("trace") or []),
                "plan_alternatives": told.get("alternatives")}
    turn = pipeline.handle(text, mode="survival", inventory={}, pending=session.pending,
                           context=session.last_exchange, game=name, screen=screen or None,
                           last_target=session.last_target)
    reply = responder.reply(turn, text, summary=session.summary(), window=session.window())
    session.record(text, turn, reply.text)
    web = getattr(turn, "web", None)
    return {**base, "response": reply.text, "outcome": turn.outcome, "action": turn.action,
            "source": reply.source, "plan_tree": turn.plan_tree,
            "plan_summary": turn.plan_summary, "plan_alternatives": turn.plan_alternatives,
            "explanation": ([f"по снимку экрана: {screen}"] if screen else [])
            + list(turn.explanation or []),
            "screen": screen,
            "options": [option.label for option in turn.options],
            "sources": [{"site": hit.site, "url": hit.url} for hit in getattr(web, "hits", [])]}


@router.get("/games")
async def get_games() -> Dict[str, Any]:
    return {"games": games_list()}


@router.post("/chat")
async def window_chat(message: WindowMessage) -> Dict[str, Any]:
    from api import advice
    from orchestrator.llm import ModelUnavailable
    seen = None
    if message.screen:
        # Кадр снимается, только пока окно игры на переднем плане; здесь берётся последний
        # свежий — игрок переключился в окно ассистента, чтобы спросить.
        facts = await asyncio.to_thread(advice._screen_facts)
        seen = facts.as_prompt() if facts is not None else None
    try:
        return await asyncio.to_thread(answer, message.text, message.game,
                                       advice._assistant_pipeline(), advice._assistant_responder(),
                                       seen)
    except ModelUnavailable as error:
        return {"game": canonical(message.game), "response": f"Модель недоступна: {error}",
                "outcome": "error"}


@router.post("/reset")
async def window_reset(message: WindowMessage) -> Dict[str, Any]:
    """Начать разговор об игре заново."""
    _sessions.pop(canonical(message.game).lower(), None)
    return {"ok": True}
