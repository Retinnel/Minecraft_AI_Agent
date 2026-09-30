"""Общий доступ к языковой модели для оценочных конфигураций.

Конфигурации A и C отличаются промптом и тем, что делается с ответом, но обращаются к модели
одинаково, поэтому обращение вынесено сюда.

Отдельная забота — отличать отказ системы от сбоя канала связи. Бесплатные провайдеры
периодически отвечают ошибкой или пустым ответом, и если считать это отказом, измеряется не
качество системы, а качество сети. Поэтому запрос повторяется, а неустранённый сбой
помечается явно и в статистику решений не попадает.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from api.groq_client import GroqClient, _loads_jsonish_object

ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


class ModelUnavailable(RuntimeError):
    """Сбой канала связи с моделью, а не решение системы."""


_client: Optional[GroqClient] = None


def client() -> GroqClient:
    global _client
    if _client is None:
        from api.llm_config_manager import get_llm_config
        _client = GroqClient(get_llm_config())
    return _client


def reset() -> None:
    """Сбрасывает клиента: нужно после смены модели в пределах одного процесса."""
    global _client
    _client = None


def ask_json(prompt: str, utterance: str) -> Dict[str, Any]:
    """Ответ модели, разобранный как объект JSON.

    Повторяет запрос при сбое; исчерпав попытки, поднимает ModelUnavailable.
    """
    active = client()
    if not active.is_available():
        raise ModelUnavailable(
            "Языковая модель недоступна: проверь ключ в .env и настройки llm_config.json"
        )

    params = active._build_api_params()
    params["messages"] = [{"role": "user", "content": prompt.replace("{utterance}", utterance)}]
    # Ответ конфигураций — короткий JSON; штатного предела в 100 токенов на него не хватает.
    params["max_tokens"] = max(params.get("max_tokens", 100), 200)

    last = ""
    for attempt in range(ATTEMPTS):
        try:
            completion = active._create_completion(params)
            choices = getattr(completion, "choices", None)
            if not choices:
                raise ValueError("ответ без вариантов")
            return _loads_jsonish_object(choices[0].message.content or "") or {}
        except Exception as error:
            last = f"{type(error).__name__}: {error}"
            if attempt + 1 < ATTEMPTS:
                time.sleep(BACKOFF_SECONDS * (attempt + 1))
    raise ModelUnavailable(last)
