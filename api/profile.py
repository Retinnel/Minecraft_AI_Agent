"""Профиль игрока: что известно о том, как человек играет, — поверх миров и игр.

Память мира (`data/worlds/<мир>/`) отвечает на вопрос «что есть в этом мире». Профиль
отвечает на другой: «какой это игрок». Он один на все миры и все игры: наблюдения из
Minecraft и из Terraria складываются в одни и те же счётчики, потому что записаны в словах,
не зависящих от игры, — «просил готовое», «спрашивал, как сделать», «погиб от падения»,
«цель — инструмент».

Устроен так же, как остальная система: наблюдения — это исходы конвейера и события игры
(то, что система решила и увидела, а не то, что модель подумала об игроке), черты выводятся
из счётчиков правилами с порогом свидетельств, и каждая черта называет, на чём основана.
Модель получает готовые черты одной строкой и только для тона и совета в свободном
разговоре; на допустимость действий профиль не влияет.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

router = APIRouter(prefix="/api/profile", tags=["profile"])

PROFILE_PATH = Path(__file__).resolve().parent.parent / "data" / "profile.json"

# Черта не называется, пока под ней меньше свидетельств: одна реплика — не привычка.
MIN_EVIDENCE = 5
# Доля, с которой склонность считается выраженной.
STRONG_SHARE = 0.6

# Виды реплик в словах, не зависящих от игры.
ASK_HOW = "ask_how"          # спросил, как сделать: план
ASK_WORLD = "ask_world"      # вопрос о мире: из чего, откуда, что выпадает, что это
ASK_WEB = "ask_web"          # вопрос, ушедший в поиск: тактика, механики
WANT_READY = "want_ready"    # попросил выдать готовое
DELEGATE = "delegate"        # попросил сделать за него
SET_GOAL = "set_goal"        # цель, задание, кампания
REMEMBER = "remember"        # попросил запомнить
TALK = "talk"                # разговор без просьбы

ACTION_KINDS = {
    "plan_goal": ASK_HOW,
    "ask_uses": ASK_WORLD, "ask_sources": ASK_WORLD, "ask_drops": ASK_WORLD,
    "ask_available": ASK_WORLD, "ask_about": ASK_WORLD, "show_progress": ASK_WORLD,
    "search_web": ASK_WEB,
    "give_item": WANT_READY, "summon_entity": WANT_READY,
    "execute_plan": DELEGATE,
    "create_quest": SET_GOAL, "start_campaign": SET_GOAL, "propose_goal": SET_GOAL,
    "remember": REMEMBER,
}

# К чему относится цель реплики. Определяется по онтологии игры (инструмент) и по
# родовому слову в идентификаторе — оно одно и то же в разных играх.
TOOLS, WEAPONS, ARMOR = "tools", "weapons", "armor"
CATEGORY_RU = {TOOLS: "инструменты", WEAPONS: "оружие", ARMOR: "броня"}
CATEGORY_WORDS = {
    WEAPONS: ("sword", "bow", "crossbow", "trident", "spear", "arrow"),
    ARMOR: ("helmet", "chestplate", "leggings", "boots", "shield", "armor"),
    TOOLS: ("pickaxe", "axe", "shovel", "hoe", "hammer"),
}

_lock = threading.Lock()
_state: Optional[Dict[str, Any]] = None
_last_death_key: Optional[str] = None


def _empty() -> Dict[str, Any]:
    return {"turns": 0, "kinds": {}, "clarified": 0, "refused": 0, "categories": {},
            "deaths": 0, "threats": {}, "games": {}}


def _enabled() -> bool:
    """Профиль пишется только при работающем сервере с выбранным миром: тесты и прогоны
    оценочного контура его не трогают (то же правило, что у состояния по мирам)."""
    from api import worlds
    return worlds.active()


def load() -> Dict[str, Any]:
    global _state
    if _state is None:
        try:
            _state = {**_empty(), **json.loads(PROFILE_PATH.read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError):
            _state = _empty()
    return _state


def _save() -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(_state, ensure_ascii=False, indent=1), encoding="utf-8")


def _bump(table: Dict[str, int], key: str) -> None:
    table[key] = int(table.get(key, 0)) + 1


def category(target: Optional[str], kb: Optional[Any] = None) -> Optional[str]:
    """Род цели: инструмент, оружие или броня; None — прочее."""
    if not target:
        return None
    if kb is not None:
        try:
            if kb.tool_profile(target) is not None:
                return TOOLS
        except Exception:
            pass
    name = target.rsplit(":", 1)[-1]
    for kind, words in CATEGORY_WORDS.items():
        # «broadsword» — тоже меч; короткие слова («bow», «axe») только отдельным словом,
        # иначе «rainbow» стал бы оружием.
        if any(name == word or name.endswith("_" + word)
               or (len(word) >= 5 and name.endswith(word)) for word in words):
            return kind
    return None


def observe(state: Dict[str, Any], game: str, action: Optional[str], outcome: str,
            target_category: Optional[str] = None) -> None:
    """Одна реплика: что игрок хотел и чем кончилось. Чистая функция над состоянием."""
    state["turns"] += 1
    played = state["games"].setdefault(game, {"turns": 0, "kinds": {}})
    played["turns"] += 1
    kind = ACTION_KINDS.get(action or "", TALK) if outcome != "none" else TALK
    _bump(state["kinds"], kind)
    _bump(played["kinds"], kind)
    if outcome == "clarify":
        state["clarified"] += 1
    if outcome == "reject":
        state["refused"] += 1
    if target_category:
        _bump(state["categories"], target_category)


def observe_turn(game: str, turn: Any, kb: Optional[Any] = None) -> None:
    """Исход конвейера — в профиль. Вызывается после каждой реплики чата."""
    if not _enabled():
        return
    with _lock:
        observe(load(), game or "Minecraft", getattr(turn, "action", None),
                getattr(turn, "outcome", "none"), category(getattr(turn, "target", None), kb))
        _save()


def observe_death(game: str, threat: str, key: Optional[str] = None) -> None:
    """Гибель и класс угрозы, к которому её отнёс разбор смерти. `key` — примета события:
    повторный разбор той же смерти второй раз не считается."""
    global _last_death_key
    if not _enabled() or not threat:
        return
    with _lock:
        if key is not None and key == _last_death_key:
            return
        _last_death_key = key
        state = load()
        state["deaths"] += 1
        _bump(state["threats"], threat)
        _save()


# --- черты ---

def _share(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def traits(state: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Черты игрока с основанием. Выводятся правилами из счётчиков; модель не участвует."""
    state = state if state is not None else load()
    kinds = state["kinds"]
    found: List[Dict[str, Any]] = []

    def add(key: str, text: str, basis: str, evidence: int) -> None:
        found.append({"key": key, "text": text, "basis": basis, "evidence": evidence})

    own = kinds.get(ASK_HOW, 0)
    ready = kinds.get(WANT_READY, 0) + kinds.get(DELEGATE, 0)
    if own + ready >= MIN_EVIDENCE:
        share = _share(own, own + ready)
        if share >= STRONG_SHARE:
            add("self_reliant", "предпочитает делать сам: чаще спрашивает, как получить, чем просит готовое",
                f"планов — {own}, просьб выдать или сделать за него — {ready}", own + ready)
        elif share <= 1 - STRONG_SHARE:
            add("wants_ready", "предпочитает получать готовое: чаще просит выдать или сделать, чем спрашивает как",
                f"просьб выдать или сделать за него — {ready}, планов — {own}", own + ready)

    asked = kinds.get(ASK_WORLD, 0) + kinds.get(ASK_WEB, 0)
    requests = state["turns"] - kinds.get(TALK, 0)
    if requests >= MIN_EVIDENCE and _share(asked, requests) >= 0.4:
        add("curious", "любознателен: много вопросов об устройстве мира и тактике",
            f"вопросов — {asked} из {requests} реплик по делу", requests)

    if requests >= MIN_EVIDENCE and _share(state["clarified"], requests) >= 0.25:
        add("generic_names", "называет предметы родовым словом — варианты стоит предлагать сразу",
            f"уточнений понадобилось — {state['clarified']} из {requests}", requests)

    goals = kinds.get(SET_GOAL, 0)
    if goals >= MIN_EVIDENCE:
        add("goal_driven", "играет от цели: берёт задания и кампании",
            f"целей и заданий — {goals}", goals)

    categories = state["categories"]
    total = sum(categories.values())
    if total >= MIN_EVIDENCE:
        top, count = max(categories.items(), key=lambda pair: pair[1])
        if _share(count, total) >= 0.5:
            add("focus_" + top, f"главный интерес — {CATEGORY_RU.get(top, top)}",
                f"{count} из {total} целей с определённым родом", total)

    if state["deaths"] >= 3:
        threat, count = max(state["threats"].items(), key=lambda pair: pair[1])
        if _share(count, state["deaths"]) >= 0.5:
            add("dies_of", f"чаще всего гибнет от одной угрозы — «{threat}»: о ней стоит напоминать заранее",
                f"{count} из {state['deaths']} смертей", state["deaths"])

    played = {game: data["turns"] for game, data in state["games"].items() if data["turns"]}
    if len(played) > 1:
        listed = ", ".join(f"{game} — {count}" for game, count in
                           sorted(played.items(), key=lambda pair: -pair[1]))
        add("games", "играет в несколько игр", f"реплик по играм: {listed}", sum(played.values()))
    return found


def preferences(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Черты в виде, пригодном для решателя: что из профиля влияет на вывод.

    interest — главный род целей игрока (инструменты, оружие, броня): по нему решатель
    кампании упорядочивает достижимые цели. deadly — угроза, от которой игрок гибнет чаще
    всего, и основание: предупреждение о ней повторяется чаще и называет это основание.
    Пусто, пока свидетельств мало: без порога решатель подстраивался бы под случайность.
    """
    found: Dict[str, Any] = {}
    for trait in traits(state):
        if trait["key"].startswith("focus_"):
            found["interest"] = trait["key"][len("focus_"):]
            found["interest_basis"] = trait["basis"]
        elif trait["key"] == "dies_of":
            source = state if state is not None else load()
            found["deadly"] = max(source["threats"].items(), key=lambda pair: pair[1])[0]
            found["deadly_basis"] = trait["basis"]
    return found


def current_preferences() -> Dict[str, Any]:
    """Предпочтения для решателя на сервере; в тестах и прогонах — пусто."""
    if not _enabled():
        return {}
    with _lock:
        return preferences(load())


def as_prompt(state: Optional[Dict[str, Any]] = None) -> str:
    """Черты одной строкой для свободного разговора; пусто, пока свидетельств мало."""
    lines = [trait["text"] for trait in traits(state) if trait["key"] != "games"]
    return ("Об игроке (по наблюдениям во всех играх): " + "; ".join(lines) + ".") if lines else ""


def prompt_line() -> str:
    """Строка для разговора на сервере; в тестах и прогонах — пусто."""
    if not _enabled():
        return ""
    with _lock:
        return as_prompt(load())


def reset() -> None:
    global _state, _last_death_key
    with _lock:
        _state = _empty()
        _last_death_key = None
        if _enabled():
            _save()


@router.get("/")
async def get_profile() -> Dict[str, Any]:
    with _lock:
        state = load()
        return {"turns": state["turns"], "deaths": state["deaths"],
                "games": {game: data["turns"] for game, data in state["games"].items()},
                "traits": traits(state), "min_evidence": MIN_EVIDENCE,
                "prompt": as_prompt(state)}


@router.post("/forget")
async def forget_profile() -> Dict[str, Any]:
    """Забыть всё, что накоплено об игроке."""
    reset()
    return {"ok": True}
