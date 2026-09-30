"""Фиксация конфигурации языковой модели на время прогона.

Руководство требует, чтобы условия эксперимента были воспроизводимы. Штатные настройки
сервера для этого не годятся: температура 0.7 делает два прогона одного набора разными,
а сотни токенов ответа обрезают JSON.

Профиль применяется к загруженной конфигурации в памяти и файл настроек не трогает —
рабочие настройки пользователя прогон менять не должен.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Детерминированный разбор: одна и та же реплика должна давать один и тот же ответ.
EVAL_TEMPERATURE = 0.0
# Ответ конфигураций — JSON из трёх полей; штатного предела в 100 токенов на него не хватает.
EVAL_MAX_TOKENS = 200


def apply(model_type: Optional[str] = None, temperature: float = EVAL_TEMPERATURE) -> Dict[str, Any]:
    """Возвращает описание применённого профиля — оно идёт в отчёт о прогоне."""
    from api.llm_config_manager import get_llm_config

    config = get_llm_config()
    if model_type:
        if model_type not in config.models:
            raise ValueError(
                f"Неизвестная модель {model_type!r}. Доступны: {', '.join(config.models)}"
            )
        config.model_type = model_type

    model = config.models[config.model_type]
    parameters = model.parameters if isinstance(model.parameters, dict) else {}
    parameters["temperature"] = temperature
    parameters["max_tokens"] = max(int(parameters.get("max_tokens") or 0), EVAL_MAX_TOKENS)
    model.parameters = parameters

    return {
        "ключ": config.model_type,
        "модель": model.name,
        "провайдер": model.provider,
        "температура": temperature,
        "max_tokens": parameters["max_tokens"],
    }
