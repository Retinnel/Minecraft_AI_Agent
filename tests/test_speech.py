"""Распознавание речи для голоса в игре: проверки записи и обращение к движку (заглушка)."""

import io
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from api import speech

MOD = Path(__file__).resolve().parent.parent / "mod" / "minecraft-mod" / "src" / "client" / "java"


def _wav(seconds: float, width: int = 2, rate: int = 16000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(b"\x00" * int(seconds * rate) * width)
    return out.getvalue()


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(speech.keys, "get", return_value="test-key")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_text_comes_from_the_engine(self):
        seen = {}

        def post(audio, key):
            seen["key"], seen["size"] = key, len(audio)
            return {"text": " дай меч ", "duration_ms": 1000}

        audio = _wav(1.0)
        result = speech.transcribe(audio, post=post)
        self.assertEqual(result["text"], "дай меч")
        self.assertEqual(result["seconds"], 1.0)
        self.assertEqual(seen, {"key": "test-key", "size": len(audio)})

    def test_bad_recordings_never_reach_the_engine(self):
        def post(audio, key):
            raise AssertionError("движок вызван")

        for audio, status in ((b"", 400), (b"not a wav", 400), (_wav(0.1), 400),
                              (_wav(1.0, width=1), 400), (_wav(31.0), 413)):
            with self.assertRaises(speech.SpeechError) as caught:
                speech.transcribe(audio, post=post)
            self.assertEqual(caught.exception.status, status)

    def test_engine_failure_is_a_gateway_error(self):
        def post(audio, key):
            raise TimeoutError()

        with self.assertRaises(speech.SpeechError) as caught:
            speech.transcribe(_wav(1.0), post=post)
        self.assertEqual(caught.exception.status, 502)

    def test_without_key_the_endpoint_says_so(self):
        with patch.object(speech.keys, "get", return_value=None):
            with self.assertRaises(speech.SpeechError) as caught:
                speech.transcribe(_wav(1.0), post=lambda audio, key: {"text": "x"})
        self.assertEqual(caught.exception.status, 503)

    def test_multipart_carries_the_file_field(self):
        body, boundary = speech._multipart(b"RIFFdata")
        self.assertIn(b'name="file"', body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))


class ModVoiceContractTests(unittest.TestCase):
    """Голос в моде — ввод реплики: текст уходит в чат ассистента, а не в команды игры."""

    def test_voice_goes_through_the_chat_path(self):
        source = (MOD / "com/example/assistant/VoiceInput.java").read_text(encoding="utf-8")
        self.assertIn("runtime.sendChatMessage(text)", source)
        for forbidden in ("sendCommand", "sendChat(", "connection.send"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()


class RepairTests(unittest.TestCase):
    """Правка распознанного по словарю онтологии: только к словам, которые система знает."""

    @classmethod
    def setUpClass(cls):
        from kb.graph import KnowledgeBase
        from resolver.resolve import Resolver
        cls.vocabulary = Resolver(KnowledgeBase.shared()).index.vocabulary

    def repair(self, text):
        from resolver.speech import repair
        return repair(text, self.vocabulary)

    def test_glued_words_are_split_into_known_ones(self):
        self.assertEqual(self.repair("Пустьделаеткаменнуюкирку."), "пусть делает каменную кирку.")
        self.assertEqual(self.repair("дайалмазныймеч"), "дай алмазный меч")

    def test_foreign_punctuation_is_removed(self):
        self.assertEqual(self.repair("начинай。"), "начинай")

    def test_unknown_long_words_are_left_alone(self):
        for text in ("Достопримечательность какая-то", "железнодорожный вокзал",
                     "действительно", "какпобедитьдракона"):
            self.assertEqual(self.repair(text), text)

    def test_real_words_are_not_replaced_by_similar_ones(self):
        self.assertEqual(self.repair("Как сделал печь?"), "Как сделал печь?")

    def test_transcribe_keeps_what_was_heard(self):
        with patch.object(speech.keys, "get", return_value="k"):
            result = speech.transcribe(_wav(1.0), post=lambda audio, key: {"text": "сделайверстак"},
                                       vocabulary=self.vocabulary)
        self.assertEqual((result["text"], result["heard"]), ("сделай верстак", "сделайверстак"))
