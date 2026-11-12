"""Озвучка: выбор голоса, кэш и поведение без процесса синтеза.

Сам синтез (VoxCPM2 на видеокарте) в тестах не запускается: обращение к нему подменено.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import voice


class PickVoiceTests(unittest.TestCase):
    def test_plain_reply_is_spoken_by_the_assistant(self):
        self.assertEqual(voice.pick_voice(None, "chat", "Держи хлеб."),
                         ("assistant", "Держи хлеб."))

    def test_signed_reply_is_spoken_by_its_persona_without_the_signature(self):
        chosen, spoken = voice.pick_voice(None, "chat", "Кузнец Торвальд: Добудь три булыжника.")
        self.assertEqual(chosen, "smith")
        self.assertEqual(spoken, "Добудь три булыжника.")

    def test_campaign_reply_takes_the_persona_of_the_running_campaign(self):
        with patch("api.campaign.get_active_campaign", return_value={"persona": "elder"}):
            self.assertEqual(voice.pick_voice(None, "campaign", "Этап пройден.")[0], "elder")
        with patch("api.campaign.get_active_campaign", return_value=None):
            self.assertEqual(voice.pick_voice(None, "campaign", "Этап пройден.")[0], "assistant")

    def test_explicit_voice_wins_and_unknown_voice_is_ignored(self):
        self.assertEqual(voice.pick_voice("cartographer", "chat", "В путь.")[0], "cartographer")
        self.assertEqual(voice.pick_voice("no-such-voice", "chat", "В путь.")[0], "assistant")

    def test_every_persona_has_a_voice(self):
        voices = voice.known_voices()
        self.assertIn("assistant", voices)
        for key in voice.persona_names().values():
            self.assertIn(key, voices)


class SpeakEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = patch.object(voice, "CACHE_DIR", Path(self.temp.name))
        self.cache.start()
        app = FastAPI()
        app.include_router(voice.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.cache.stop()
        self.temp.cleanup()

    def test_without_the_synthesis_process_the_answer_is_503(self):
        with patch.object(voice, "_synthesize", side_effect=OSError("нет процесса")):
            response = self.client.post("/api/voice/speak", json={"text": "Привет"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["voice"], "assistant")

    def test_synthesized_line_is_cached_and_not_synthesized_twice(self):
        calls = []

        def fake(chosen, text):
            calls.append((chosen, text))
            return b"RIFF-fake-wav"

        with patch.object(voice, "_synthesize", side_effect=fake):
            first = self.client.post("/api/voice/speak",
                                     json={"text": "Кузнец Торвальд:  Добудь   булыжник."})
            second = self.client.post("/api/voice/speak",
                                      json={"text": "Кузнец Торвальд: Добудь булыжник."})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["x-voice"], "smith")
        self.assertEqual(first.content, b"RIFF-fake-wav")
        self.assertEqual(second.content, b"RIFF-fake-wav")
        self.assertEqual(calls, [("smith", "Добудь булыжник.")])

    def test_cached_line_is_served_when_the_process_is_down(self):
        with patch.object(voice, "_synthesize", return_value=b"RIFF-fake-wav"):
            self.client.post("/api/voice/speak", json={"text": "Рядом крипер."})
        with patch.object(voice, "_synthesize", side_effect=OSError("нет процесса")):
            again = self.client.post("/api/voice/speak", json={"text": "Рядом крипер."})
        self.assertEqual(again.status_code, 200)

    def test_empty_text_is_rejected(self):
        self.assertEqual(self.client.post("/api/voice/speak", json={"text": "  "}).status_code, 400)
        self.assertEqual(self.client.post("/api/voice/speak",
                                          json={"text": "Кузнец Торвальд:"}).status_code, 400)

    def test_long_text_is_cut(self):
        self.assertEqual(len(voice.clean("слово " * 200)), voice.MAX_TEXT)

    def test_status_reports_a_missing_process(self):
        with patch.object(voice, "_server_alive", return_value=False):
            status = self.client.get("/api/voice/status").json()
        self.assertFalse(status["available"])
        self.assertIn("assistant", status["voices"])


if __name__ == "__main__":
    unittest.main()
