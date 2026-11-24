"""Общая настройка тестов.

Тесты не читают `.env` разработчика: ключи провайдеров в них либо заданы явно, либо
подменены. Иначе тест «без ключа провайдер недоступен» проходил бы только на машине без
ключей.
"""

from pathlib import Path

from api import keys

keys.ENV_PATH = Path(__file__).resolve().parent / "no-such-file.env"
