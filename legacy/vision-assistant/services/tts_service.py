import torch
import sounddevice as sd
import threading
from queue import Queue

from backend.core.config import settings


class TTSManager:
    """
    Менеджер синтеза речи на базе Silero TTS.
    Использует внутреннюю очередь для последовательного воспроизведения.
    """

    def __init__(self, model_id: str = None, speaker: str = None,
                 device: torch.device = None, sample_rate: int = None):
        self.device = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.speaker = speaker or settings.tts_speaker
        self.sample_rate = sample_rate or settings.tts_sample_rate
        self.model_id = model_id or settings.tts_model_id
        self.model = None
        self.speed = settings.tts_speed

        self._text_queue: Queue[str] = Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def _init_model(self) -> None:
        if self.model is None:
            print(f"[INFO] Инициализация Silero TTS на {self.device}...")
            self.model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-models",
                model="silero_tts",
                language="ru",
                speaker=self.model_id,
                trust_repo=True,
            )
            self.model.to(self.device)
            print("[SUCCESS] Silero TTS готов.")

    def say(self, text: str) -> None:
        """Добавляет текст в очередь на озвучку."""
        if text:
            self._text_queue.put(text)

    def _worker(self) -> None:
        """Фоновый поток: берёт текст из очереди и синтезирует речь."""
        self._init_model()
        import torch.nn.functional as F

        while True:
            text = self._text_queue.get()
            try:
                clean_text = text.replace("\n", " ").strip()
                if not clean_text:
                    continue

                # Используем SSML тег prosody для ускорения БЕЗ изменения питча (ТЗ п. 104)
                # Значение передается в процентах (например, 1.8 -> 180%)
                speed_pct = int(self.speed * 100)
                ssml_text = f"<speak><prosody rate='{speed_pct}%'>{clean_text}</prosody></speak>"

                audio = self.model.apply_tts(
                    text=ssml_text,
                    speaker=self.speaker,
                    sample_rate=self.sample_rate,
                )

                sd.play(audio.cpu().numpy(), self.sample_rate)
                sd.wait()
            except Exception as e:
                print(f"[TTS Worker Error] {e}")
            finally:
                self._text_queue.task_done()


tts_manager = TTSManager()


def speak(text: str) -> None:
    tts_manager.say(text)

