import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

from backend.services.llm_service import ask_gemini
from backend.services.tts_service import speak
from backend.services.memory_service import MemoryService


@dataclass
class UserRequest:
    text: str
    image_bytes: Optional[bytes] = None
    # field(default_factory=...) — правильный способ задать изменяемый default.
    # time.time() как default напрямую вычислялся ОДИН РАЗ при объявлении класса,
    # и все объекты получали одинаковый timestamp.
    timestamp: float = field(default_factory=time.time)


class RequestPipeline:
    """
    FIFO-конвейер обработки запросов (Producer-Consumer).
    Гарантирует порядок вывода ответов через asyncio.Queue.
    Оверлей и TTS запускаются параллельно согласно ТЗ.
    """

    def __init__(self, overlay_callback: Callable[[str], None], memory_service: MemoryService):
        self.queue: asyncio.Queue[UserRequest] = asyncio.Queue()
        self.overlay_callback = overlay_callback
        self.memory_service = memory_service
        self.is_running = False
        self._worker_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if not self.is_running:
            self.is_running = True
            self._worker_task = asyncio.create_task(self._worker())
            print("[INFO] Pipeline worker started.")

    def stop(self) -> None:
        self.is_running = False
        if self._worker_task:
            self._worker_task.cancel()
            print("[INFO] Pipeline worker stopped.")

    async def add_request(self, text: str, image_bytes: Optional[bytes] = None) -> None:
        request = UserRequest(text=text, image_bytes=image_bytes)
        await self.queue.put(request)

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()

        while self.is_running:
            try:
                request = await self.queue.get()

                # 1. Получаем последние сообщения из памяти для контекста (Dialogue Flow)
                history = self.memory_service.get_recent_history(limit=5)

                # 2. Запрос к Gemini (sync → async через executor)
                # Теперь передаем текст, картинку и историю как отдельные параметры
                answer = await loop.run_in_executor(
                    None, ask_gemini, request.text, request.image_bytes, history
                )

                if answer:
                    # 3. Сохраняем диалог в память
                    self.memory_service.add_message("user", request.text)
                    self.memory_service.add_message("assistant", answer)

                    # 4. Вывод в оверлей и TTS запускаются ПАРАЛЛЕЛЬНО (требование ТЗ)
                    await asyncio.gather(
                        loop.run_in_executor(None, self.overlay_callback, answer),
                        loop.run_in_executor(None, speak, answer),
                    )

                self.queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[ERROR] Pipeline error: {e}")
                await asyncio.sleep(1)
