"""Миры: у каждого своя история разговора и своё состояние, и они переживают перезапуск.

До этого модуля всё, что ассистент знал о разговоре, жило в памяти процесса: режим,
персонаж, окно диалога, сводка фактами, последний план. Задания и кампания писались в
файл, но один на все миры. Перезапуск сервера или вход в другой мир — и ассистент ничего
не помнит либо помнит чужое.

Теперь у каждого мира папка в `data/worlds/`:

    meta.json             как мир называется и когда в нём были
    dialogue.jsonl        разговор: реплики игрока, ответы и сообщения по инициативе ассистента
    session.json          сессия диалога (режим, персонаж, окно, сводка) и последний план
    player.json           счётчики игрока: убийства, последняя гибель
    challenge_state.json  задания
    campaign_state.json   кампания

Мир называет мод при входе игрока: для одиночной игры — имя папки сохранения (оно уникально,
название мира — нет), для сетевой — адрес сервера. Страница `/chat` без игры работает в
мире «без игры». Игрок у сервера один, поэтому мир — состояние процесса, а не поле запроса.

Другая игра — отдельный мир. Заметки и факты с экрана о «Ведьмаке» не относятся ни к
одному сохранению Minecraft, поэтому при смене игры заводится мир `game:<название>` (без учёта регистра) со
своей папкой и своей памятью; к нему возвращаются, сколько бы раз игру ни меняли.

Пока мир не выбран (тесты, сторонний код), модуль ничего не пишет и путей не трогает.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/worlds", tags=["worlds"])

WORLDS_DIR = Path(__file__).resolve().parent.parent / "data" / "worlds"
CURRENT_FILE = "current.json"

DEFAULT_WORLD = "web"
DEFAULT_NAME = "Без игры"

# Мир другой игры называется по игре: «game:ведьмак 3».
GAME_PREFIX = "game:"
DEFAULT_GAME = "Minecraft"

# Сколько сообщений держать в памяти и отдавать клиентам; на диске история полная.
MAX_LOADED = 300
MAX_ID = 200
MAX_NAME = 80
MAX_TEXT = 4000

# Сообщения по инициативе ассистента, которые входят в историю. Дежурные реплики о
# состоянии («здоровье в норме») в неё не пишутся: они повторяются каждые двадцать секунд.
PROACTIVE_SOURCES = ("campaign", "postmortem", "forewarn")

_current: Optional[Dict[str, Any]] = None
_dialogue: List[Dict[str, Any]] = []
_last_plan: Dict[str, Any] = {}


# --- какой мир сейчас ---

def active() -> bool:
    return _current is not None


def current() -> Optional[Dict[str, Any]]:
    return dict(_current) if _current else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def folder_name(world_id: str) -> str:
    """Имя папки мира: читаемая часть и отпечаток идентификатора.

    Идентификатор приходит из запроса, поэтому в путь идёт только отфильтрованная часть;
    отпечаток не даёт двум разным мирам слиться в одну папку после фильтра.
    """
    readable = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", world_id).strip("_")[:40] or "world"
    return f"{readable}-{hashlib.sha1(world_id.encode('utf-8')).hexdigest()[:8]}"


def _dir(world_id: str) -> Path:
    return WORLDS_DIR / folder_name(world_id)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)


# --- вход в мир ---

def enter(world_id: str, name: Optional[str] = None) -> Dict[str, Any]:
    """Делает мир текущим: сохраняет прежний, поднимает состояние нового."""
    global _current
    world_id = " ".join(str(world_id or "").split())[:MAX_ID]
    if not world_id:
        raise ValueError("Не назван мир.")
    name = " ".join(str(name or "").split())[:MAX_NAME] or None

    if _current and _current["id"] == world_id:
        if name and name != _current["name"]:
            _current["name"] = name
        _touch_meta()
        return snapshot()

    save()
    directory = _dir(world_id)
    meta = _read(directory / "meta.json", {})
    _current = {
        "id": world_id,
        "name": name or meta.get("name") or world_id,
        "dir": str(directory),
        "created_at": meta.get("created_at") or _now(),
        "new": not (directory / "meta.json").exists(),
        "game": meta.get("game") or _game_of(world_id),
    }
    _load(directory)
    _touch_meta()
    _write(WORLDS_DIR / CURRENT_FILE, {"id": world_id, "name": _current["name"]})
    logger.info(f"[Worlds] Мир «{_current['name']}»: сообщений в истории {len(_dialogue)}")
    return snapshot()


def restore() -> None:
    """При запуске сервера возвращает мир, в котором работали в прошлый раз."""
    last = _read(WORLDS_DIR / CURRENT_FILE, {})
    try:
        enter(last.get("id") or DEFAULT_WORLD, last.get("name") or DEFAULT_NAME)
    except Exception as error:   # состояние прошлого запуска не должно мешать серверу подняться
        logger.warning(f"[Worlds] Прошлый мир не восстановлен: {error}")


def _touch_meta() -> None:
    if not _current:
        return
    meta = {"id": _current["id"], "name": _current["name"],
            "created_at": _current["created_at"], "last_seen": _now()}
    if _current.get("game"):
        meta["game"] = _current["game"]
    _write(Path(_current["dir"]) / "meta.json", meta)


# --- миры других игр ---

def _is_default_game(game: Optional[str]) -> bool:
    return not game or game.strip().lower() == DEFAULT_GAME.lower()


def _game_of(world_id: str) -> Optional[str]:
    """Игра мира, заведённого под другую игру; None для миров Minecraft."""
    if world_id.startswith(GAME_PREFIX):
        return world_id[len(GAME_PREFIX):].strip() or None
    return None


def game_world_id(game: str) -> str:
    """Идентификатор без учёта регистра: «ведьмак 3» и «Ведьмак 3» — одна игра."""
    return f"{GAME_PREFIX}{' '.join(str(game).split()).lower()}"[:MAX_ID]


def ensure_game_world(game: Optional[str]) -> Optional[Path]:
    """Папка мира другой игры; заводится при первой смене игры. Для Minecraft — None.

    Как и всё в модуле, без выбранного мира ничего не пишет.
    """
    if not _current or _is_default_game(game):
        return None
    world_id = game_world_id(game)
    directory = _dir(world_id)
    meta = _read(directory / "meta.json", None)
    now = _now()
    if not isinstance(meta, dict) or meta.get("id") != world_id:
        meta = {"id": world_id, "name": " ".join(str(game).split())[:MAX_NAME],
                "game": " ".join(str(game).split()), "created_at": now}
    meta["last_seen"] = now
    try:
        _write(directory / "meta.json", meta)
    except OSError as error:
        logger.warning(f"[Worlds] Мир игры «{game}» не заведён: {error}")
    return directory


def memory_dir(game: Optional[str] = None) -> Optional[Path]:
    """Где лежит память для игры, о которой сейчас разговор.

    Minecraft — папка текущего мира. Другая игра — папка её собственного мира, если только
    текущий мир и не есть мир этой игры. Без выбранного мира памяти на диске нет.
    """
    if not _current:
        return None
    own = _current.get("game")
    if _is_default_game(game) or (own and own.lower() == str(game).strip().lower()):
        return Path(_current["dir"])
    return ensure_game_world(game)


def _load(directory: Path) -> None:
    """Поднимает состояние мира во все места, где оно живёт в процессе."""
    global _dialogue, _last_plan
    from api import advice, campaign, challenges, player_state
    from orchestrator import session as sessions

    stored = _read(directory / "session.json", {})
    sessions.replace(sessions.Session.from_dict(stored.get("session") or {}))
    _last_plan = stored.get("plan") if isinstance(stored.get("plan"), dict) else {}

    _dialogue = []
    try:
        lines = (directory / "dialogue.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines[-MAX_LOADED:]:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("text"):
            _dialogue.append(message)

    challenges.CHALLENGE_STATE_PATH = directory / "challenge_state.json"
    challenges.load_challenge_state()
    campaign.CAMPAIGN_STATE_PATH = directory / "campaign_state.json"
    campaign.load_campaign_state()

    player_state.reset_player_state()
    player_state.restore_progress(_read(directory / "player.json", {}))

    # Мир другой игры: разговор в нём идёт об этой игре, даже если сессия ещё не сохранялась.
    game = (_current or {}).get("game")
    if game and sessions.shared().is_default_game:
        sessions.shared().set_game(game)

    # То, что относилось к прежнему миру и в новом было бы ложью: недавние ответы,
    # предупреждение об угрозе, отметки обработанных событий.
    advice._conversation_history.clear()
    advice._response_history.clear()
    advice._last_rp_response = None
    advice._last_forewarning = None
    advice._processed_log_ids.clear()


# --- запись ---

def save() -> None:
    """Сохраняет состояние текущего мира. Без выбранного мира ничего не делает."""
    if not _current:
        return
    from api import campaign, challenges, player_state
    from orchestrator import session as sessions

    directory = Path(_current["dir"])
    try:
        _write(directory / "session.json",
               {"session": sessions.shared().to_dict(), "plan": _last_plan, "saved_at": _now()})
        _write(directory / "player.json", player_state.progress_snapshot())
        challenges.save_challenge_state()
        campaign.save_campaign_state()
    except OSError as error:
        logger.warning(f"[Worlds] Состояние мира не сохранено: {error}")


def save_player() -> None:
    """Счётчики игрока после убийства или гибели: от них зависит прогресс заданий."""
    if not _current:
        return
    from api import player_state
    try:
        _write(Path(_current["dir"]) / "player.json", player_state.progress_snapshot())
    except OSError as error:
        logger.warning(f"[Worlds] Счётчики игрока не сохранены: {error}")


def log_message(role: str, text: str, **details: Any) -> None:
    """Дописывает сообщение в историю мира: в память и в конец файла."""
    text = (text or "").strip()
    if not _current or role not in ("user", "assistant") or not text:
        return
    message = {"time": _now(), "role": role, "text": text[:MAX_TEXT]}
    message.update({key: value for key, value in details.items() if value not in (None, "", [])})
    _dialogue.append(message)
    del _dialogue[:-MAX_LOADED]
    try:
        path = Path(_current["dir"]) / "dialogue.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(message, ensure_ascii=False) + "\n")
    except OSError as error:
        logger.warning(f"[Worlds] Сообщение не записано в историю: {error}")


def record_exchange(player_message: Optional[str], result: Dict[str, Any]) -> None:
    """Обмен репликами в чате: вопрос, ответ, план и сессия после него."""
    global _last_plan
    if not _current:
        return
    if player_message:
        log_message("user", player_message)
    log_message("assistant", result.get("response") or result.get("error") or "",
                outcome=result.get("outcome"), source=result.get("source"),
                command=result.get("command"))
    if result.get("plan_tree"):
        _last_plan = {
            "tree": result.get("plan_tree"), "summary": result.get("plan_summary"),
            "alternatives": result.get("plan_alternatives"),
            "explanation": list(result.get("explanation") or []), "time": _now(),
        }
    save()


def record_proactive(response: Dict[str, Any]) -> None:
    """Сообщение по инициативе ассистента: поручение кампании, предупреждение, разбор гибели."""
    if _current and response.get("source") in PROACTIVE_SOURCES:
        log_message("assistant", response.get("response") or "", source=response.get("source"),
                    proactive=True)
        save()


# --- чтение ---

def history(limit: int = 50) -> List[Dict[str, Any]]:
    return list(_dialogue[-max(1, min(int(limit), MAX_LOADED)):])


def snapshot(limit: int = 6) -> Dict[str, Any]:
    """Что клиенту нужно, чтобы продолжить с того места, где остановились."""
    from orchestrator import session as sessions

    session = sessions.shared()
    return {
        "world": {"id": _current["id"], "name": _current["name"], "new": _current.get("new", False)}
        if _current else None,
        "messages": len(_dialogue),
        "history": history(limit),
        "plan": dict(_last_plan),
        "assistant_mode": session.mode,
        "persona": session.persona,
        "game": session.game,
    }


def listing() -> List[Dict[str, Any]]:
    """Все миры, в которых были: последний посещённый — первым."""
    found = []
    if WORLDS_DIR.exists():
        for directory in WORLDS_DIR.iterdir():
            meta = _read(directory / "meta.json", None) if directory.is_dir() else None
            if isinstance(meta, dict) and meta.get("id"):
                found.append({"id": meta["id"], "name": meta.get("name") or meta["id"],
                              "last_seen": meta.get("last_seen"),
                              "current": bool(_current and _current["id"] == meta["id"])})
    return sorted(found, key=lambda world: world.get("last_seen") or "", reverse=True)


def forget(world_id: str) -> bool:
    """Удаляет сохранённое состояние мира. Текущий мир забыть нельзя: в нём идёт работа."""
    import shutil

    world_id = " ".join(str(world_id or "").split())[:MAX_ID]
    if not world_id or (_current and _current["id"] == world_id):
        return False
    directory = _dir(world_id)
    meta = _read(directory / "meta.json", None)
    # Папка должна быть папкой именно этого мира: удаляется только то, что создал модуль.
    if not isinstance(meta, dict) or meta.get("id") != world_id:
        return False
    shutil.rmtree(directory, ignore_errors=True)
    import memory
    memory.reset()
    return True


# --- HTTP ---

@router.get("/")
async def list_worlds() -> Dict[str, Any]:
    return {"current": snapshot(0)["world"], "worlds": listing()}


@router.post("/enter")
async def enter_world(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Мод вызывает при входе игрока в мир, страница чата — при выборе мира."""
    try:
        return enter(str(payload.get("id") or ""), payload.get("name"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/forget")
async def forget_world(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Забыть мир: нужен, например, съёмке страниц для записки, которая работает в
    собственном мире и убирает его за собой."""
    return {"ok": forget(str(payload.get("id") or ""))}


@router.get("/history")
async def world_history(limit: int = 50) -> Dict[str, Any]:
    state = snapshot(limit)
    return {"world": state["world"], "messages": state["messages"], "history": state["history"],
            "plan": state["plan"]}
