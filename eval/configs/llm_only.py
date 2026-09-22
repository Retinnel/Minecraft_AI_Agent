"""Конфигурация A — только языковая модель, без базы знаний.

Модель получает те же инструменты, но сама называет идентификатор предмета или существа.
Ни резолва по онтологии, ни проверки запретов, ни ограничений режима: что модель сказала,
то и выполняется.

Это не соломенное чучело, а честный вариант «просто прикрутить LLM» — ровно то, чем был
проект до переработки, и то, что предложил бы любой, кто спросит «зачем вам база знаний,
если есть языковая модель». Разница между этой конфигурацией и гибридом и есть предмет
курсовой работы.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv  # noqa: E402

# Оценочный контур запускается отдельно от сервера, который обычно и подхватывает .env.
load_dotenv()

from api.groq_client import GroqClient, _loads_jsonish_object  # noqa: E402

from eval.schema import Config, Prediction  # noqa: E402

KEY = "llm_only"
TITLE = "A — только языковая модель"
DESCRIPTION = "Модель сама называет идентификаторы; база знаний и проверки не участвуют."

PROMPT = """Ты — помощник в игре Minecraft 1.20.1. Разбери реплику игрока.

Верни ТОЛЬКО JSON без пояснений:
{
  "action": "give_item" | "remove_item" | "summon_entity" | "none",
  "id": "minecraft:идентификатор предмета или существа, либо null",
  "count": число
}

Правила:
- "none" — если это не просьба выдать, забрать или призвать.
- Идентификатор указывай точный, как в игре, с префиксом minecraft:.
- Если игрок не назвал количество, ставь 1.

Реплика игрока: "{utterance}"
"""

_client: Optional[GroqClient] = None


def _get_client() -> GroqClient:
    global _client
    if _client is None:
        from api.llm_config_manager import get_llm_config
        _client = GroqClient(get_llm_config())
    return _client


def _ask(utterance: str) -> Dict[str, Any]:
    client = _get_client()
    if not client.is_available():
        raise RuntimeError(
            "Языковая модель недоступна: проверь ключ в .env и настройки llm_config.json"
        )
    params = client._build_api_params()
    params["messages"] = [{"role": "user", "content": PROMPT.replace("{utterance}", utterance)}]
    # Этой конфигурации нужен запас на JSON: штатные 100 токенов обрезают ответ.
    params["max_tokens"] = max(params.get("max_tokens", 100), 200)
    completion = client._create_completion(params)
    text = completion.choices[0].message.content or ""
    return _loads_jsonish_object(text) or {}


def predict(utterance: str) -> Prediction:
    started = time.perf_counter()
    try:
        payload = _ask(utterance)
    except Exception as error:  # сеть, квота, разбор ответа
        return Prediction(outcome="reject", details={"error": f"{type(error).__name__}: {error}"},
                          latency_ms=(time.perf_counter() - started) * 1000)

    action = str(payload.get("action") or "none").strip()
    target = payload.get("id")
    target = str(target).strip() if isinstance(target, str) and target.strip() else None

    try:
        count = int(payload.get("count", 1))
    except (TypeError, ValueError):
        count = 1

    latency = (time.perf_counter() - started) * 1000

    if action == "none" or not target:
        return Prediction(outcome="none", latency_ms=latency, details={"raw": payload})

    # Ключевое отличие конфигурации: сказанное моделью принимается без проверки.
    command = (f"/summon {target} ~ ~ ~" if action == "summon_entity"
               else f"/give @s {target} {count}")
    return Prediction(
        outcome="act",
        intent=action,
        item_id=target if action != "summon_entity" else None,
        entity_id=target if action == "summon_entity" else None,
        count=count,
        command=command,
        latency_ms=latency,
        details={"raw": payload},
    )


CONFIG = Config(key=KEY, title=TITLE, description=DESCRIPTION, predict=predict)
