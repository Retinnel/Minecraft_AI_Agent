import asyncio
from typing import Optional

from backend.core.capture import ScreenCapture
from backend.core.vision import preprocess_frame
from backend.core.voice import VoiceListener
from backend.services.memory_service import MemoryService
from backend.services.pipeline import RequestPipeline


class ApplicationContainer:
    """
    Центральный IoC-контейнер приложения.
    Владеет жизненным циклом всех подсистем и координирует их взаимодействие.
    """

    def __init__(self):
        self.capture = ScreenCapture(fps=12)
        self.voice_listener = VoiceListener()
        self.memory_service = MemoryService()
        self.current_overlay_text: str = ""
        self.current_overlay_status: str = "IDLE"
        self.is_active = False
        self.overlay_visible = True
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None

        self.pipeline = RequestPipeline(
            overlay_callback=self._on_pipeline_response,
            memory_service=self.memory_service,
        )

        self.voice_listener.on_transcription = self._on_voice_transcription

    def _on_pipeline_response(self, text: str) -> None:
        """Вызывается pipeline'ом при получении ответа от Gemini."""
        self.current_overlay_text = text
        self.current_overlay_status = "IDLE"
        print(f"[PIPELINE] Ответ для оверлея: {text}")

    def _on_voice_transcription(self, text: str) -> None:
        """Вызывается VoiceListener после транскрибации речи."""
        print(f"🎤 Транскрибация: {text}")
        frame = self.capture.get_frame()
        image_bytes = preprocess_frame(frame) if frame is not None else None

        if self._main_loop is None:
            print("[ERROR] Main event loop не инициализирован в контейнере!")
            return

        asyncio.run_coroutine_threadsafe(
            self.pipeline.add_request(text, image_bytes),
            loop=self._main_loop,
        )

    def set_overlay_status(self, status: str) -> None:
        """Обновляет статусное сообщение для PTT-индикатора в оверлее."""
        self.current_overlay_status = status

    def start_all(self) -> None:
        # asyncio.get_running_loop() возбуждает RuntimeError, если loop не запущен.
        # В контексте FastAPI lifespan он всегда запущен — используем напрямую.
        self._main_loop = asyncio.get_running_loop()

        self.capture.start()
        self.voice_listener.start()
        self.pipeline.start()
        print("[INFO] Core services (Capture, Voice, Pipeline) started.")

    def stop_all(self) -> None:
        self.capture.stop()
        self.voice_listener.stop()
        self.pipeline.stop()
        print("[INFO] Core services stopped.")


container = ApplicationContainer()

