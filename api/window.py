"""Разговор в отдельном окне поверх игры (`desktop/window.py`, страница `/mini`).

У окна своя сессия на каждую игру: вопрос о Terraria в окне не переключает игру в чате
Minecraft и не попадает в историю его мира. Конвейер тот же — извлечение намерения,
резолвер, валидатор, планировщик, поиск, — но команд окно не отдаёт: мода за ним нет,
поэтому режим всегда без выдачи, а ответ — план, сведения базы знаний или найденное в сети.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/window", tags=["window"])

DEFAULT_GAME = "Minecraft"
# Манера речи окна: напарник, а не справочник. Меняет слова, не содержание — правила
# формулирования («только по фактам», «не выдумывай») остаются выше в тех же подсказках.
STYLE = ("Манера речи: ты — напарник, который играет рядом с игроком. Говори на «ты», живо и "
         "по-человечески, как в голосовом чате: можно короткую реакцию на ситуацию и совет, что "
         "сделать следующим шагом; если уместно — один встречный вопрос. Без канцелярита, без "
         "перечислений через точку с запятой и без слов «согласно», «в найденных описаниях», "
         "«источники». Учитывай, о чём говорили раньше. Игрок называет вещи по-русски, а "
         "справка может быть на английском — это одни и те же вещи: не говори, что названного "
         "игроком в игре нет, из-за разницы в языке. Факты не выдумывай и не опускай.")

# Вопрос о том, что видно на экране: «куда ставить», «где тут», «что это». На такие отвечает
# модель зрения по самому кадру — в тексте фактов сцены расположения нет.
LOOK_WORDS = re.compile(r"(?<![а-яё])(куда|где|сюда|туда|тут|здесь|на экране|передо мной|видишь|"
                        r"покажи|это|этот|эта|эту|эти|этим|вот)(?![а-яё])", re.IGNORECASE)
MAX_TEXT = 400
# Журнал разговоров окна: по нему разбираются неудачные ответы. В репозиторий не идёт.
LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "window" / "dialogue.jsonl"

_sessions: Dict[str, Any] = {}


class WindowMessage(BaseModel):
    text: str = ""
    game: Optional[str] = None
    # Диалог окна: у каждого своя история и своя сессия, они хранятся на диске.
    dialog: Optional[str] = None
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


# --- диалоги: история и сессия каждого разговора на диске ---

DIALOGS_DIR = LOG_PATH.parent / "dialogs"
MAX_TITLE = 40
SHOWN = ("plan_tree", "plan_alternatives", "explanation", "options", "sources")

_dialog_sessions: Dict[str, Any] = {}


def _dialog_path(dialog_id: str) -> Optional[Path]:
    """Путь файла диалога; идентификатор — только буквы и цифры, чужой путь не пройдёт."""
    return DIALOGS_DIR / f"{dialog_id}.json" if re.fullmatch(r"[a-z0-9]{6,32}", dialog_id or "") else None


def load_dialog(dialog_id: str) -> Optional[Dict[str, Any]]:
    path = _dialog_path(dialog_id)
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_dialog(dialog: Dict[str, Any]) -> None:
    path = _dialog_path(dialog["id"])
    if path is None:
        return
    dialog["updated"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dialog, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def create_dialog(game: Optional[str]) -> Dict[str, Any]:
    import uuid
    dialog = {"id": uuid.uuid4().hex[:12], "title": "", "game": canonical(game),
              "messages": [], "session": {}}
    save_dialog(dialog)
    return dialog


def list_dialogs() -> List[Dict[str, Any]]:
    """Диалоги от недавних к старым: название, игра, число реплик."""
    found = []
    for path in DIALOGS_DIR.glob("*.json") if DIALOGS_DIR.exists() else []:
        dialog = load_dialog(path.stem)
        if dialog:
            found.append({"id": dialog["id"], "title": dialog.get("title") or "новый диалог",
                          "game": dialog.get("game"), "updated": dialog.get("updated"),
                          "messages": len(dialog.get("messages") or [])})
    return sorted(found, key=lambda entry: entry.get("updated") or "", reverse=True)


def delete_dialog(dialog_id: str) -> bool:
    path = _dialog_path(dialog_id)
    _dialog_sessions.pop(dialog_id, None)
    if path is None or not path.exists():
        return False
    path.unlink()
    return True


def dialog_session(dialog: Dict[str, Any]):
    """Сессия диалога: из памяти, иначе восстановленная из сохранённого."""
    from orchestrator.session import Session
    if dialog["id"] not in _dialog_sessions:
        session = Session.from_dict(dialog.get("session") or {})
        session.set_mode("survival")
        session.set_game(dialog.get("game") or DEFAULT_GAME)
        _dialog_sessions[dialog["id"]] = session
    return _dialog_sessions[dialog["id"]]


def remember(dialog: Dict[str, Any], text: str, result: Dict[str, Any], session: Any) -> None:
    """Обмен репликами — в историю диалога; название — по первой реплике игрока."""
    dialog["messages"].append({"role": "user", "text": text})
    dialog["messages"].append({"role": "assistant", "text": result.get("response") or "",
                               **{key: result[key] for key in SHOWN if result.get(key)}})
    dialog["game"] = result.get("game") or dialog.get("game")
    if not dialog.get("title"):
        dialog["title"] = " ".join(text.split())[:MAX_TITLE]
    dialog["session"] = session.to_dict()
    save_dialog(dialog)


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
           screen: Optional[str] = None, session: Optional[Any] = None) -> Dict[str, Any]:
    """Одна реплика окна. Конвейер и формулировщик передаются: в тестах — заглушки.

    screen — факты об экране игры одной строкой. Как и в чате, это данные для разбора
    реплики («что это за предмет?»), а не указания: действие выбирается по реплике."""
    from kb import games
    from orchestrator import why
    name = canonical(game)
    # Без диалога (тесты, прямой вызов) — сессия на игру в памяти, как было.
    session = session if session is not None else _session(name)
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
    # Источники показывает само окно — ссылками под ответом, — поэтому в тексте их нет.
    reply = responder.reply(turn, text, summary=session.summary(), window=session.window(),
                            style=STYLE, cite=False)
    session.record(text, turn, reply.text)
    web = getattr(turn, "web", None)
    return {**base, "response": reply.text, "outcome": turn.outcome, "action": turn.action,
            "source": reply.source, "plan_tree": turn.plan_tree,
            "plan_summary": turn.plan_summary, "plan_alternatives": turn.plan_alternatives,
            "explanation": ([f"по снимку экрана: {screen}"] if screen else [])
            + list(turn.explanation or []),
            "screen": screen,
            "stages": {key: value for key, value in (turn.stages or {}).items() if key != "screen"},
            "options": [option.label for option in turn.options],
            "sources": [{"site": hit.site, "url": hit.url} for hit in getattr(web, "hits", [])]}


@router.get("/games")
async def get_games() -> Dict[str, Any]:
    return {"games": games_list()}


def look(result: Dict[str, Any], text: str, watcher: Any) -> Dict[str, Any]:
    """Дополняет ответ взглядом на кадр, когда вопрос — о видимом на экране.

    План и сведения базы знаний кадром не заменяются: по кадру отвечает только то, что
    иначе пошло бы в поиск или осталось без ответа. Найденное в сети и сведения игры идут
    модели зрения справкой; источники к ответу, как и раньше, дописывает система.
    """
    if not LOOK_WORDS.search(text or "") or result.get("action") not in (None, "search_web"):
        return result
    context = "\n".join(part for part in (result.get("screen"), result.get("response")) if part)
    seen = watcher.look(text, game=result.get("game") or "", context=context, style=STYLE)
    if not seen:
        return result
    result["response"] = seen
    result["source"] = "vision"
    result["outcome"] = "act"
    result["explanation"] = ["ответ по кадру: вопрос о видимом на экране"] + list(
        result.get("explanation") or [])
    return result


def log(entry: Dict[str, Any]) -> None:
    """Реплика, ответ и след конвейера — строкой в журнал; сбой записи разговору не мешает."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as target:
            target.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **entry},
                                    ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


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
    dialog = (load_dialog(message.dialog) or create_dialog(message.game)) if message.dialog else None
    session = dialog_session(dialog) if dialog else None
    if dialog and session.game.lower() != canonical(message.game).lower():
        session.set_game(canonical(message.game))      # игру в диалоге можно сменить
    try:
        result = await asyncio.to_thread(answer, message.text, message.game,
                                         advice._assistant_pipeline(),
                                         advice._assistant_responder(), seen, session)
        if message.screen:
            from vision import shared
            await asyncio.to_thread(look, result, message.text, shared())
        if dialog:
            remember(dialog, message.text, result, session)
            result["dialog"] = dialog["id"]
        log({"text": message.text, "typed_game": message.game, "screen_asked": message.screen,
             **{key: result.get(key) for key in ("game", "outcome", "action", "response",
                                                 "screen", "explanation", "stages")}})
        result.pop("stages", None)
        return result
    except ModelUnavailable as error:
        return {"game": canonical(message.game), "response": f"Модель недоступна: {error}",
                "outcome": "error"}


@router.get("/dialogs")
async def get_dialogs() -> Dict[str, Any]:
    return {"dialogs": list_dialogs()}


@router.post("/dialogs")
async def new_dialog(message: WindowMessage) -> Dict[str, Any]:
    return create_dialog(message.game)


@router.get("/dialogs/{dialog_id}")
async def get_dialog(dialog_id: str) -> Dict[str, Any]:
    dialog = load_dialog(dialog_id)
    if dialog is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="нет такого диалога")
    return {key: dialog.get(key) for key in ("id", "title", "game", "messages", "updated")}


@router.delete("/dialogs/{dialog_id}")
async def remove_dialog(dialog_id: str) -> Dict[str, Any]:
    return {"ok": delete_dialog(dialog_id)}


@router.post("/reset")
async def window_reset(message: WindowMessage) -> Dict[str, Any]:
    """Начать разговор об игре заново."""
    _sessions.pop(canonical(message.game).lower(), None)
    return {"ok": True}
