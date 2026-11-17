"""Озвучка: выбор голоса, движки, кэш и поведение без синтеза.

Ни локальный синтез (VoxCPM2 на видеокарте), ни облачный (ElevenLabs) в тестах не
вызываются: обращения к ним подменены.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import voice


class PickVoiceTests(unittest.TestCase):
    def test_plain_reply_is_spoken_by_the_base_voice(self):
        self.assertEqual(voice.pick_voice(None, "chat", "Держи хлеб."),
                         ("assistant", "Держи хлеб."))

    def test_signed_reply_is_spoken_by_its_persona_without_the_signature(self):
        chosen, spoken = voice.pick_voice(None, "chat", "Кузнец Торвальд: Добудь три булыжника.")
        self.assertEqual(chosen, "smith")
        self.assertEqual(spoken, "Добудь три булыжника.")

    def test_persona_without_a_voice_of_its_own_speaks_in_the_base_voice(self):
        chosen, spoken = voice.pick_voice(None, "chat", "Старейшина Марта: Этап пройден.")
        self.assertEqual(chosen, "assistant")
        self.assertEqual(spoken, "Этап пройден.")

    def test_campaign_reply_takes_the_persona_of_the_running_campaign(self):
        with patch("api.campaign.get_active_campaign", return_value={"persona": "smith"}):
            self.assertEqual(voice.pick_voice(None, "campaign", "Этап пройден.")[0], "smith")
        with patch("api.campaign.get_active_campaign", return_value={"persona": "elder"}):
            self.assertEqual(voice.pick_voice(None, "campaign", "Этап пройден.")[0], "assistant")
        with patch("api.campaign.get_active_campaign", return_value=None):
            self.assertEqual(voice.pick_voice(None, "campaign", "Этап пройден.")[0], "assistant")

    def test_explicit_voice_wins_and_unknown_voice_is_ignored(self):
        self.assertEqual(voice.pick_voice("smith", "chat", "В путь.")[0], "smith")
        self.assertEqual(voice.pick_voice("no-such-voice", "chat", "В путь.")[0], "assistant")

    def test_base_voice_exists(self):
        self.assertIn("assistant", voice.known_voices())


class SpeakEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(voice, "CACHE_DIR", Path(self.temp.name)),
            # По умолчанию облачный движок выключен: ключа в тестах нет и быть не должно.
            patch.dict(os.environ, {"VOICE_ENGINES": "voxcpm,elevenlabs",
                                    "ELEVENLABS_API_KEY": ""}),
        ]
        for item in self.patches:
            item.start()
        app = FastAPI()
        app.include_router(voice.router)
        self.client = TestClient(app)

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def test_without_any_engine_the_answer_is_503(self):
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
        self.assertEqual(first.headers["x-voice-engine"], "voxcpm")
        self.assertEqual(first.headers["content-type"], "audio/wav")
        self.assertEqual(second.content, b"RIFF-fake-wav")
        self.assertEqual(calls, [("smith", "Добудь булыжник.")])

    def test_cached_line_is_served_when_the_engine_is_down(self):
        with patch.object(voice, "_synthesize", return_value=b"RIFF-fake-wav"):
            self.client.post("/api/voice/speak", json={"text": "Рядом крипер."})
        with patch.object(voice, "_synthesize", side_effect=OSError("нет процесса")):
            again = self.client.post("/api/voice/speak", json={"text": "Рядом крипер."})
        self.assertEqual(again.status_code, 200)

    def test_cloud_engine_takes_over_when_the_local_one_is_down(self):
        with patch.object(voice, "_synthesize", side_effect=OSError("нет процесса")), \
                patch.object(voice, "_synthesize_elevenlabs", return_value=b"ID3-fake-mp3"):
            response = self.client.post("/api/voice/speak", json={"text": "Привет"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-voice-engine"], "elevenlabs")
        self.assertEqual(response.headers["content-type"], "audio/mpeg")

    def test_engine_order_comes_from_the_environment(self):
        with patch.dict(os.environ, {"VOICE_ENGINES": "elevenlabs, voxcpm, nonsense"}):
            self.assertEqual(voice.engines(), ["elevenlabs", "voxcpm"])
            with patch.object(voice, "_synthesize", return_value=b"RIFF-fake-wav"), \
                    patch.object(voice, "_synthesize_elevenlabs", return_value=b"ID3-fake-mp3"):
                response = self.client.post("/api/voice/speak", json={"text": "Привет"})
        self.assertEqual(response.headers["x-voice-engine"], "elevenlabs")

    def test_cloud_engine_without_key_or_voice_id_makes_no_request(self):
        with patch("urllib.request.urlopen") as network:
            with self.assertRaises(OSError):
                voice._synthesize_elevenlabs("assistant", "Привет")
            with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}):
                # Идентификатор голоса в voices.json не заполнен.
                with self.assertRaises(OSError):
                    voice._synthesize_elevenlabs("assistant", "Привет")
        network.assert_not_called()

    def test_cloud_request_carries_only_the_reply_text(self):
        sent = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"ID3-fake-mp3"

        def fake_urlopen(request, timeout=None):
            sent["url"] = request.full_url
            sent["body"] = request.data.decode("utf-8")
            sent["key"] = request.get_header("Xi-api-key")
            return FakeResponse()

        config = {"voices": {"assistant": {"elevenlabs_voice_id": "voice123"}}}
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), \
                patch.object(voice, "voices_config", return_value=config), \
                patch("urllib.request.urlopen", side_effect=fake_urlopen):
            audio = voice._synthesize_elevenlabs("smith", "Добудь булыжник.")
        self.assertEqual(audio, b"ID3-fake-mp3")
        # У кузнеца своего идентификатора нет — взят идентификатор базового голоса.
        self.assertIn("/text-to-speech/voice123", sent["url"])
        self.assertEqual(sent["key"], "test-key")
        import json
        payload = json.loads(sent["body"])
        self.assertEqual(payload, {"text": "Добудь булыжник.", "model_id": "eleven_flash_v2_5",
                                   "language_code": "ru"})

    def test_empty_text_is_rejected(self):
        self.assertEqual(self.client.post("/api/voice/speak", json={"text": "  "}).status_code, 400)
        self.assertEqual(self.client.post("/api/voice/speak",
                                          json={"text": "Кузнец Торвальд:"}).status_code, 400)

    def test_long_text_is_cut(self):
        self.assertEqual(len(voice.clean("слово " * 200)), voice.MAX_TEXT)

    def test_status_reports_missing_engines_and_lists_voices(self):
        with patch.object(voice, "_server_alive", return_value=False):
            status = self.client.get("/api/voice/status").json()
        self.assertFalse(status["available"])
        self.assertEqual(status["ready"], [])
        keys = [entry["key"] for entry in status["voices"]]
        self.assertIn("assistant", keys)
        self.assertEqual(next(e["label"] for e in status["voices"] if e["key"] == "assistant"),
                         "базовый")


if __name__ == "__main__":
    unittest.main()
