"""Доступ к языковой модели для конвейера.

Модель вызывается в двух местах конвейера — на входе, чтобы извлечь намерение, и на выходе,
чтобы сформулировать ответ, — и оба вызова устроены одинаково, поэтому обращение вынесено
сюда. Им же пользуется оценочный контур: конфигурации прогоняются через те же вызовы, что
работают на сервере.

Отдельная забота — отличать отказ системы от сбоя канала связи. Провайдеры периодически
отвечают ошибкой или пустым ответом. Если считать это отказом, игрок получает «нельзя» там,
где система ничего не решала, а оценка измеряет качество сети. Поэтому запрос повторяется,
а неустранённый сбой поднимается отдельным исключением.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from api.groq_client import GroqClient, _loads_jsonish_object

ATTEMPTS = 3
BACKOFF_SECONDS = 2.0

# Разбору реплики нужен короткий JSON, формулированию — пара предложений. Но рассуждающие
# модели (gpt-oss и подобные) тратят часть бюджета на рассуждение до ответа: при пределе в
# 200 токенов 148 уходило на рассуждение, и JSON обрывался посреди поля. Обрезанный ответ не
# разбирался и молча становился «не запросом действия» — ошибка канала выглядела как ошибка
# понимания. Поэтому предел взят с запасом, а обрезка обрабатывается явно.
JSON_MAX_TOKENS = 600
TEXT_MAX_TOKENS = 500

# Во сколько раз увеличить бюджет, если ответ всё же обрезан.
TRUNCATION_GROWTH = 2


class ModelUnavailable(RuntimeError):
    """Сбой канала связи с моделью, а не решение системы."""


_client: Optional[GroqClient] = None
_client_model: Optional[str] = None


def client() -> GroqClient:
    """Клиент для текущей модели из конфигурации.

    Администратор может сменить модель на ходу, поэтому клиент пересоздаётся, когда
    выбранная модель перестаёт совпадать с той, под которую он был создан.
    """
    global _client, _client_model
    from api.llm_config_manager import get_llm_config

    config = get_llm_config()
    if _client is None or _client_model != config.model_type:
        _client = GroqClient(config)
        _client_model = config.model_type
    return _client


def reset() -> None:
    """Сбрасывает клиента: нужно после смены модели в пределах одного процесса."""
    global _client, _client_model
    _client = None
    _client_model = None


def available() -> bool:
    try:
        return client().is_available()
    except Exception:
        return False


def _complete(messages: List[Dict[str, str]], max_tokens: int) -> str:
    active = client()
    if not active.is_available():
        raise ModelUnavailable(
            "Языковая модель недоступна: проверь ключ в .env и настройки llm_config.json"
        )

    params = active._build_api_params()
    params["messages"] = messages
    params["max_tokens"] = max(int(params.get("max_tokens") or 0), max_tokens)

    last = ""
    for attempt in range(ATTEMPTS):
        try:
            completion = active._create_completion(params)
            choices = getattr(completion, "choices", None)
            if not choices:
                raise ValueError("ответ без вариантов")
            choice = choices[0]
            if getattr(choice, "finish_reason", None) == "length":
                # Ответ оборван пределом токенов. Отдавать его дальше нельзя: обрывок JSON
                # не разберётся и будет принят за «ничего не просили». Повтор — сразу и с
                # бо́льшим бюджетом: сеть здесь ни при чём, ждать незачем.
                last = f"ответ обрезан на {params['max_tokens']} токенах"
                params["max_tokens"] = int(params["max_tokens"]) * TRUNCATION_GROWTH
                continue
            return choice.message.content or ""
        except Exception as error:
            last = f"{type(error).__name__}: {error}"
            if attempt + 1 < ATTEMPTS:
                time.sleep(BACKOFF_SECONDS * (attempt + 1))
    raise ModelUnavailable(last)


def ask_json(prompt: str, utterance: str) -> Dict[str, Any]:
    """Ответ модели, разобранный как объект JSON."""
    text = _complete([{"role": "user", "content": prompt.replace("{utterance}", utterance)}],
                     JSON_MAX_TOKENS)
    return _loads_jsonish_object(text) or {}


def ask_text(messages: List[Dict[str, str]]) -> str:
    """Свободный текст: формулирование ответа игроку."""
    return _complete(messages, TEXT_MAX_TOKENS).strip()
