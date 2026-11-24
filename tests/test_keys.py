"""Ключи провайдеров: файл `.env` проекта важнее окружения процесса.

Закрепляет найденную 2026-11-24 ошибку: в переменных среды Windows лежал
`DEEPSEEK_API_KEY`, в `.env` проекта — другой ключ под именем `DEEPSEEK_API`, и сервер
работал на ключе из среды.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import keys
from api.groq_client import DEEPSEEK_KEY_NAMES, GroqClient, key_note
from tests.test_groq_client import _FakeClient, _build_config


class KeySourceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_file = Path(self.temp_dir.name) / ".env"
        self.path_patch = patch.object(keys, "ENV_PATH", self.env_file)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    # Окружение в тестах очищается: при полном прогоне в нём уже лежит .env разработчика,
    # подмешанный при импорте серверных модулей.

    def write(self, text):
        self.env_file.write_text(text, encoding="utf-8")

    def test_file_wins_over_the_environment_even_under_another_name(self):
        self.write("DEEPSEEK_API=key-from-file\n")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "key-from-system"}, clear=True):
            key, source = keys.lookup(*DEEPSEEK_KEY_NAMES)
            self.assertEqual(key, "key-from-file")
            self.assertEqual(source, ".env: DEEPSEEK_API")
            self.assertEqual(keys.shadowed(*DEEPSEEK_KEY_NAMES), ["DEEPSEEK_API_KEY"])

    def test_file_wins_under_the_same_name_too(self):
        self.write("DEEPSEEK_API_KEY=key-from-file\n")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "key-from-system"}, clear=True):
            self.assertEqual(keys.get(*DEEPSEEK_KEY_NAMES), "key-from-file")

    def test_environment_is_used_when_the_file_has_no_key(self):
        self.write("GROQ_API_KEY=other\n")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "key-from-system"}, clear=True):
            key, source = keys.lookup(*DEEPSEEK_KEY_NAMES)
            self.assertEqual(key, "key-from-system")
            self.assertEqual(source, "окружение: DEEPSEEK_API_KEY")
            self.assertEqual(keys.shadowed(*DEEPSEEK_KEY_NAMES), [])

    def test_no_file_and_no_environment_means_no_key(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(keys.lookup(*DEEPSEEK_KEY_NAMES), (None, None))
            self.assertFalse(keys.describe(*DEEPSEEK_KEY_NAMES)["present"])

    def test_changed_file_is_picked_up_without_a_restart(self):
        self.write("DEEPSEEK_API=first\n")
        self.assertEqual(keys.get("DEEPSEEK_API"), "first")
        self.write("DEEPSEEK_API=second\n")
        self.assertEqual(keys.get("DEEPSEEK_API"), "second")

    def test_client_is_built_with_the_key_from_the_file(self):
        self.write("DEEPSEEK_API=key-from-file\n")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "key-from-system"}, clear=True), \
                patch("api.groq_client.OllamaClient", return_value=_FakeClient("ok")) as factory:
            client = GroqClient(llm_config=_build_config(provider="deepseek"))
        self.assertEqual(client.provider_api_key, "key-from-file")
        self.assertEqual(factory.call_args.kwargs["api_key"], "key-from-file")

    def test_startup_note_never_prints_the_key(self):
        self.write("DEEPSEEK_API=key-from-file\n")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "key-from-system"}, clear=True):
            note = key_note(*DEEPSEEK_KEY_NAMES)
        self.assertIn(".env: DEEPSEEK_API", note)
        self.assertIn("DEEPSEEK_API_KEY", note)          # предупреждение о ключе из окружения
        self.assertNotIn("key-from-file", note)
        self.assertNotIn("key-from-system", note)
        self.assertIn(keys.fingerprint("key-from-file"), note)


if __name__ == "__main__":
    unittest.main()
