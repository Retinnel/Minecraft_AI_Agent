"""Состояние кампании поверх заданий.

Кампания — последовательность этапов, доказанных решателем (`solver.campaign`). Каждый
этап по очереди становится активным заданием `api.challenges`: прогресс считается из
инвентаря теми же правилами, награда выдаётся тем же путём и той же очередью. Модуль
добавляет только то, чего у одиночного задания нет: порядок этапов, переход к следующему
после награды и события для проактивного канала — завершение этапа и новое поручение
объявляются без обращения игрока.

Тексты персонажа здесь не сочиняются: у кампании и этапов есть поля для реплик, которые
заполняет рассказчик; пока их нет, используются шаблоны из тех же фактов.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api import challenges
from api.player_state import get_player_state

router = APIRouter(prefix="/api/campaign", tags=["campaign"])

CAMPAIGN_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "campaign_state.json"
MAX_HISTORY = 20
MAX_EVENTS = 50
SOURCE = "campaign"

ACTIVE = "active"
PENDING = "pending"
COMPLETED = "completed"
CANCELLED = "cancelled"

# События для проактивного канала.
EVENT_STAGE_STARTED = "stage_started"
EVENT_STAGE_COMPLETED = "stage_completed"
EVENT_CAMPAIGN_COMPLETED = "campaign_completed"
EVENT_CAMPAIGN_CANCELLED = "campaign_cancelled"

_campaign_state: Dict[str, Any] = {"active": None, "history": [], "events": [], "updated_at": None}
_builder_instance = None


class CampaignStartRequest(BaseModel):
    goal: str
    count: int = Field(default=1, ge=1)
    mode: str = "creative"
    persona: Optional[str] = None
    title: Optional[str] = None


# --- решатель -----------------------------------------------------------------------------


def _builder():
    """Единственный построитель на процесс: предварительный расчёт стоимостей занимает секунду."""
    global _builder_instance
    if _builder_instance is None:
        from kb.graph import KnowledgeBase
        from solver.campaign import CampaignBuilder
        _builder_instance = CampaignBuilder(KnowledgeBase.shared())
    return _builder_instance


def _kb():
    return _builder().kb


def _label(thing: Optional[str]) -> Optional[str]:
    if not thing:
        return None
    return _kb().label(thing) or thing


def inventory_counts(player_state: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """Инвентарь из последнего снимка, который прислал мод."""
    counts = ((player_state or get_player_state()).get("inventory") or {}).get("counts") or {}
    result: Dict[str, int] = {}
    for item_id, count in counts.items():
        try:
            result[str(item_id)] = int(count)
        except (TypeError, ValueError):
            continue
    return result


def propose_goals(inventory: Optional[Dict[str, int]] = None, mode: str = "creative") -> List[Dict[str, Any]]:
    """Цели, до которых из текущего состояния доказанно ведёт кампания."""
    stock = inventory if inventory is not None else inventory_counts()
    return [
        {"item": candidate.item, "label": _label(candidate.item), "kind": candidate.kind,
         "stages": candidate.stages, "effort": candidate.effort}
        for candidate in _builder().propose_goals(stock, mode)
    ]


# --- сериализация -------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state() -> Dict[str, Any]:
    return {"active": None, "history": [], "events": [], "updated_at": None}


def save_campaign_state() -> None:
    CAMPAIGN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CAMPAIGN_STATE_PATH.write_text(json.dumps(_campaign_state, ensure_ascii=False, indent=2),
                                   encoding="utf-8")


def load_campaign_state() -> None:
    global _campaign_state
    _campaign_state = _default_state()
    if not CAMPAIGN_STATE_PATH.exists():
        return
    try:
        payload = json.loads(CAMPAIGN_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    _campaign_state = {
        "active": payload.get("active") if isinstance(payload.get("active"), dict) else None,
        "history": payload.get("history") if isinstance(payload.get("history"), list) else [],
        "events": payload.get("events") if isinstance(payload.get("events"), list) else [],
        "updated_at": payload.get("updated_at"),
    }


def reset_campaign_state(*, persist: bool = False) -> None:
    global _campaign_state
    _campaign_state = _default_state()
    if persist:
        save_campaign_state()


def _touch(stamp: Optional[str] = None) -> str:
    stamp = stamp or _now_iso()
    _campaign_state["updated_at"] = stamp
    active = _campaign_state.get("active")
    if isinstance(active, dict):
        active["updated_at"] = stamp
    return stamp


def _serialize_stage(stage, total: int) -> Dict[str, Any]:
    from solver.render import render_summary, render_tree

    kb = _kb()
    return {
        "index": stage.index,
        "total": total,
        "target": stage.target,
        "label": _label(stage.target),
        "count": stage.count,
        "unlocks": stage.unlocks,
        "for_item": stage.for_item,
        "for_label": _label(stage.for_item),
        "effort": stage.effort,
        "source": stage.source,
        "source_label": _label(stage.source),
        "reward": stage.reward,
        "reward_label": _label(stage.reward),
        "reward_count": stage.reward_count,
        "reward_options": [
            {"item": option.item, "label": _label(option.item), "count": option.count}
            for option in stage.reward_options
        ],
        "plan_tree": render_tree(stage.plan, kb),
        "plan_summary": render_summary(stage.plan, kb),
        "explanation": list(stage.explanation),
        "status": PENDING,
        "progress_count": 0,
        "challenge_id": None,
        "started_at": None,
        "completed_at": None,
        # Реплики персонажа: поручение и реплика на завершение. Заполняет рассказчик.
        "texts": {"brief": None, "done": None},
    }


def stage_reason(stage: Dict[str, Any]) -> str:
    """Зачем этап нужен — факт из онтологии, который персонаж пересказывает игроку."""
    unlocks = stage.get("unlocks")
    for_label = stage.get("for_label")
    if unlocks == "tool" and for_label:
        return f"Открывает добычу: {for_label}."
    if unlocks == "station" and for_label:
        return f"Открывает изготовление: {for_label}."
    if unlocks == "material" and for_label:
        return f"Понадобится для: {for_label}."
    return "Цель кампании."


def stage_brief(stage: Dict[str, Any]) -> str:
    """Шаблон поручения, если рассказчик его не написал."""
    text = stage.get("texts", {}).get("brief")
    if text:
        return text
    return (f"Этап {stage['index']} из {stage['total']}: собери {stage['count']} × "
            f"{stage['label']}. {stage_reason(stage)}")


def stage_done(stage: Dict[str, Any]) -> str:
    """Шаблон реплики на завершение этапа, если рассказчик её не написал."""
    text = stage.get("texts", {}).get("done")
    if text:
        return text
    reward = (f" Награда — {stage['reward_label']} ×{stage['reward_count']}."
              if stage.get("reward") else "")
    return f"Этап {stage['index']} из {stage['total']} пройден: {stage['label']}.{reward}"


def campaign_outro(campaign: Dict[str, Any]) -> str:
    text = campaign.get("texts", {}).get("outro")
    if text:
        return text
    return f"Кампания «{campaign['title']}» пройдена: {campaign['label']} в руках."


# --- доступ -------------------------------------------------------------------------------


def get_campaign_state() -> Dict[str, Any]:
    return deepcopy(_campaign_state)


def get_active_campaign() -> Optional[Dict[str, Any]]:
    active = _campaign_state.get("active")
    return deepcopy(active) if isinstance(active, dict) else None


def current_stage(campaign: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    campaign = campaign if campaign is not None else _campaign_state.get("active")
    if not isinstance(campaign, dict) or not campaign.get("current"):
        return None
    index = int(campaign["current"])
    stages = campaign.get("stages") or []
    return stages[index - 1] if 0 < index <= len(stages) else None


def consume_campaign_events() -> List[Dict[str, Any]]:
    """Забирает накопленные события: проактивный канал объявляет их игроку по одному разу."""
    events = _campaign_state.get("events") or []
    if not events:
        return []
    taken = deepcopy(events)
    _campaign_state["events"] = []
    _touch()
    save_campaign_state()
    return taken


def _push_event(kind: str, campaign: Dict[str, Any], stage: Optional[Dict[str, Any]] = None,
                text: Optional[str] = None) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "type": kind,
        "campaign_id": campaign.get("id"),
        "title": campaign.get("title"),
        "persona": campaign.get("persona"),
        "at": _now_iso(),
        "text": text,
    }
    if stage is not None:
        event.update({
            "stage": stage["index"], "total": stage["total"], "target": stage["target"],
            "label": stage["label"], "count": stage["count"],
            "reward": stage.get("reward"), "reward_label": stage.get("reward_label"),
            "reward_count": stage.get("reward_count"),
        })
    events = _campaign_state.setdefault("events", [])
    events.append(event)
    if len(events) > MAX_EVENTS:
        del events[:-MAX_EVENTS]
    return event


def set_campaign_text(key: str, text: Optional[str]) -> bool:
    active = _campaign_state.get("active")
    if not isinstance(active, dict) or key not in ("intro", "outro"):
        return False
    active.setdefault("texts", {})[key] = text
    _touch()
    save_campaign_state()
    return True


def set_stage_text(index: int, key: str, text: Optional[str]) -> bool:
    active = _campaign_state.get("active")
    if not isinstance(active, dict) or key not in ("brief", "done"):
        return False
    stages = active.get("stages") or []
    if not 0 < index <= len(stages):
        return False
    stages[index - 1].setdefault("texts", {})[key] = text
    _touch()
    save_campaign_state()
    return True


def get_campaign_prompt_context() -> str:
    """Сводка кампании одной-двумя строками: для окна диалога и промптов."""
    campaign = refresh_campaign_progress().get("active")
    if not isinstance(campaign, dict):
        return "Кампании нет."
    stage = current_stage(campaign)
    lines = [f"Кампания «{campaign['title']}»: цель {campaign['label']} ×{campaign['count']}, "
             f"этапов {len(campaign['stages'])}, персонаж: {campaign.get('persona') or 'не выбран'}."]
    if stage is not None:
        lines.append(f"Текущий этап {stage['index']}: {stage['label']} ×{stage['count']}, "
                     f"прогресс {stage.get('progress_count', 0)}/{stage['count']}, "
                     f"награда {stage.get('reward_label')} ×{stage.get('reward_count')}.")
    done = [s["label"] for s in campaign["stages"] if s.get("status") == COMPLETED]
    if done:
        lines.append("Пройдено: " + ", ".join(done) + ".")
    return "\n".join(lines)


# --- жизненный цикл -----------------------------------------------------------------------


def start_campaign(goal: str, count: int = 1, *, mode: str = "creative",
                   inventory: Optional[Dict[str, int]] = None,
                   persona: Optional[str] = None, title: Optional[str] = None,
                   source: str = "chat") -> Dict[str, Any]:
    """Строит кампанию решателем и запускает первый этап.

    Отказы явные: уже идёт кампания, занято одиночным заданием, цель недостижима, этапу не
    нашлось награды. Молча ничего не подменяется.
    """
    refresh_campaign_progress()
    active = _campaign_state.get("active")
    if isinstance(active, dict):
        return {"ok": False, "error_type": "active_exists",
                "error": f"Кампания «{active['title']}» ещё идёт. Сначала заверши или отмени её.",
                "campaign": deepcopy(active)}

    foreign = challenges.get_active_challenge()
    if foreign is not None:
        return {"ok": False, "error_type": "challenge_active",
                "error": "Сейчас идёт одиночное задание. Заверши или отмени его, и начнём кампанию."}

    stock = inventory if inventory is not None else inventory_counts()
    campaign = _builder().build(goal, count, stock, mode)
    if not campaign.feasible:
        return {"ok": False, "error_type": "infeasible",
                "error": campaign.reason or "Кампанию к этой цели не построить.",
                "explanation": list(campaign.explanation)}

    total = len(campaign.stages)
    stages = [_serialize_stage(stage, total) for stage in campaign.stages]
    rewardless = [stage for stage in stages if not stage.get("reward")]
    if rewardless:
        return {"ok": False, "error_type": "no_reward",
                "error": f"Для этапа «{rewardless[0]['label']}» не нашлось допустимой награды.",
                "explanation": list(campaign.explanation)}

    created_at = _now_iso()
    label = _label(goal)
    record: Dict[str, Any] = {
        "id": f"campaign-{uuid4().hex[:10]}",
        "goal": goal,
        "label": label,
        "count": count,
        "title": title or f"Кампания: {label}",
        "persona": persona,
        "mode": mode,
        "status": ACTIVE,
        "stages": stages,
        "current": None,
        "effort": campaign.effort,
        "explanation": list(campaign.explanation),
        "texts": {"intro": None, "outro": None},
        "source": source,
        "created_at": created_at,
        "completed_at": None,
        "updated_at": created_at,
    }
    _campaign_state["active"] = record

    started = _start_stage(record, 1)
    if not started.get("ok"):
        _campaign_state["active"] = None
        return {"ok": False, "error_type": "challenge",
                "error": started.get("error") or "Не удалось запустить первый этап."}

    # Первый этап мог закрыться сразу — например, игрок уже держит нужное; тогда кампания
    # продвигается дальше ещё до первой реплики.
    _advance(record)
    _touch(created_at)
    save_campaign_state()
    return {"ok": True, "campaign": get_active_campaign() or deepcopy(record),
            "summary": f"Кампания «{record['title']}»: этапов {total}."}


def _start_stage(campaign: Dict[str, Any], index: int) -> Dict[str, Any]:
    stage = campaign["stages"][index - 1]
    result = challenges.create_challenge(
        goal_type="collect",
        goal_target_id=stage["target"],
        goal_count=stage["count"],
        reward_item_id=stage["reward"],
        reward_count=stage["reward_count"],
        title=f"{campaign['title']} — этап {index} из {stage['total']}",
        description=stage_brief(stage),
        source=SOURCE,
    )
    if not result.get("ok"):
        return result
    challenge = result.get("challenge") or {}
    stage["status"] = ACTIVE
    stage["challenge_id"] = challenge.get("id")
    stage["started_at"] = _now_iso()
    stage["progress_count"] = int(challenge.get("progress_count") or 0)
    campaign["current"] = index
    _push_event(EVENT_STAGE_STARTED, campaign, stage, stage_brief(stage))
    return result


def refresh_campaign_progress(player_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Сверяет текущий этап с активным заданием и продвигает кампанию.

    Задание завершено и награждено — этап закрыт, следующий запущен; последнее — кампания
    пройдена. Задание отменено в обход кампании — кампания тоже отменяется: половинчатого
    состояния быть не должно.
    """
    campaign = _campaign_state.get("active")
    if not isinstance(campaign, dict):
        return get_campaign_state()

    challenges.refresh_challenge_progress(player_state)
    changed = _advance(campaign, player_state)
    if changed:
        _touch()
        save_campaign_state()
    return get_campaign_state()


def _advance(campaign: Dict[str, Any], player_state: Optional[Dict[str, Any]] = None) -> bool:
    """Продвигает кампанию, пока текущий этап оказывается закрытым. Возвращает, было ли что менять.

    Следующий этап может быть закрыт уже в момент запуска — игрок успел сделать и кирку,
    пока делал верстак, — поэтому после запуска прогресс сверяется заново.
    """
    changed = False
    while True:
        stage = current_stage(campaign)
        if stage is None or stage.get("status") != ACTIVE:
            return changed

        active = challenges.get_active_challenge()
        if active is not None and active.get("id") == stage.get("challenge_id"):
            progress = int(active.get("progress_count") or 0)
            if progress != stage.get("progress_count"):
                stage["progress_count"] = progress
                changed = True
            return changed

        record = _challenge_record(stage.get("challenge_id"))
        if record is None or record.get("status") == CANCELLED:
            _finish(campaign, CANCELLED, "задание этапа отменено в обход кампании")
            return True

        if record.get("status") != "rewarded":
            return changed

        changed = True
        stamp = _now_iso()
        stage["status"] = COMPLETED
        stage["completed_at"] = stamp
        stage["progress_count"] = int(record.get("progress_count") or stage["count"])
        done_text = stage_done(stage)
        challenges.update_pending_reward_response(stage["challenge_id"], done_text)
        _push_event(EVENT_STAGE_COMPLETED, campaign, stage, done_text)

        next_index = stage["index"] + 1
        if next_index > len(campaign["stages"]):
            _finish(campaign, COMPLETED)
            return True
        started = _start_stage(campaign, next_index)
        if not started.get("ok"):
            _finish(campaign, CANCELLED, started.get("error") or "не удалось запустить следующий этап")
            return True
        challenges.refresh_challenge_progress(player_state)


def _challenge_record(challenge_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not challenge_id:
        return None
    state = challenges.get_challenge_state()
    active = state.get("active")
    if isinstance(active, dict) and active.get("id") == challenge_id:
        return active
    for record in reversed(state.get("history") or []):
        if isinstance(record, dict) and record.get("id") == challenge_id:
            return record
    return None


def _finish(campaign: Dict[str, Any], status: str, reason: Optional[str] = None) -> None:
    stamp = _now_iso()
    campaign["status"] = status
    campaign["completed_at"] = stamp
    campaign["finish_reason"] = reason
    campaign["current"] = None
    campaign["updated_at"] = stamp
    if status == COMPLETED:
        _push_event(EVENT_CAMPAIGN_COMPLETED, campaign, None, campaign_outro(campaign))
    else:
        _push_event(EVENT_CAMPAIGN_CANCELLED, campaign, None, reason)
    history = _campaign_state.setdefault("history", [])
    history.append(campaign)
    if len(history) > MAX_HISTORY:
        del history[:-MAX_HISTORY]
    _campaign_state["active"] = None


def cancel_campaign(reason: Optional[str] = None) -> Dict[str, Any]:
    campaign = _campaign_state.get("active")
    if not isinstance(campaign, dict):
        return {"ok": False, "error_type": "not_found", "error": "Кампании сейчас нет."}

    stage = current_stage(campaign)
    if stage is not None and stage.get("challenge_id"):
        active = challenges.get_active_challenge()
        if active is not None and active.get("id") == stage["challenge_id"]:
            challenges.cancel_active_challenge(stage["challenge_id"])
        stage["status"] = CANCELLED

    _finish(campaign, CANCELLED, reason or "отменена игроком")
    _touch()
    save_campaign_state()
    return {"ok": True, "campaign": deepcopy(campaign),
            "summary": f"Кампания «{campaign['title']}» отменена."}


# --- HTTP ---------------------------------------------------------------------------------


@router.get("/")
async def get_campaign_endpoint():
    return refresh_campaign_progress()


@router.get("/active")
async def get_active_campaign_endpoint():
    return {"active": refresh_campaign_progress().get("active")}


@router.get("/candidates")
async def get_candidates_endpoint(mode: str = "creative"):
    return {"candidates": propose_goals(mode=mode)}


@router.post("/")
async def start_campaign_endpoint(payload: CampaignStartRequest):
    result = start_campaign(payload.goal, payload.count, mode=payload.mode,
                            persona=payload.persona, title=payload.title, source="api")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Не удалось начать кампанию.")
    return result


@router.post("/cancel")
async def cancel_campaign_endpoint():
    result = cancel_campaign()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Не удалось отменить кампанию.")
    return result


load_campaign_state()
