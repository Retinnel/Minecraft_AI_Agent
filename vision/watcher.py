"""Наблюдатель за экраном: снимок раз в несколько секунд, разбор только изменившихся кадров.

Два режима:

- «постоянный» — каждый изменившийся кадр сразу разбирается моделью; к вопросу игрока
  факты уже готовы, ответ не ждёт. В игре с движущейся камерой это 600–900 кадров в час,
  около 0,15–0,25 $ по тарифу DeepSeek;
- «по вопросу» — кадры только снимаются и сравниваются, модель вызывается, когда игрок
  спрашивает, а последний разобранный кадр устарел. Фоновых расходов нет, ответ на вопрос
  задерживается на 2–3 с.

Свежесть ограничена: кадр старше FRESH_SECONDS в разговор не попадает, иначе ассистент
отвечал бы про экран, который игрок давно закрыл. Модель зрения вызывается не чаще одного
раза одновременно: фоновый разбор и вопрос игрока ждут друг друга.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from PIL import Image

from vision import frames
from vision.capture import WindowCapture
from vision.scene import Description, SceneFacts, VisionModel, VisionUnavailable

CONTINUOUS = "continuous"
ON_DEMAND = "on_demand"
MODES = (CONTINUOUS, ON_DEMAND)

DEFAULT_INTERVAL = 4.0
MIN_INTERVAL = 2.0
MAX_INTERVAL = 30.0

# Сколько секунд снятый кадр считается текущим экраном.
FRESH_SECONDS = 30.0
# Загруженный вручную снимок — для проверки без игры — живёт дольше.
UPLOAD_SECONDS = 600.0

# Сколько разборов подряд должны назвать одну игру, чтобы ассистент переключился на неё.
GAME_VOTES = 2

# Куда складываются разобранные кадры, если съёмка запущена с сохранением: из них
# собирается набор для замера качества распознавания (eval.vision_probe). Вне git.
FRAMES_DIR = Path(__file__).resolve().parents[1] / "data" / "vision_frames"

BLACK_WARNING = ("кадр чёрный: игра, похоже, в эксклюзивном полноэкранном режиме — "
                 "включите оконный режим без рамки")


@dataclass
class Frame:
    image: Image.Image
    signature: Image.Image
    taken_at: float
    ttl: float
    title: Optional[str] = None


class ScreenWatcher:
    def __init__(self, model: Optional[VisionModel] = None, *,
                 capture_factory: Callable[[str], Any] = WindowCapture,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.model = model or VisionModel()
        self._capture_factory = capture_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._describe_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._capture = None
        self.on_game: Optional[Callable[[str], None]] = None
        self.reset()

    # --- состояние ---

    def reset(self) -> None:
        with self._lock:
            self.window: Optional[str] = None
            self.interval = DEFAULT_INTERVAL
            self.mode = CONTINUOUS
            self.follow_game = True
            self.save_dir: Optional[Path] = None
            self.counters: Counter = Counter()
            self.skipped: Counter = Counter()
            self.last_skip: Optional[str] = None
            self.warning: Optional[str] = None
            self.error: Optional[str] = None
            self.frame: Optional[Frame] = None
            self.facts: Optional[SceneFacts] = None
            self.facts_signature: Optional[Image.Image] = None
            self.facts_at: Optional[float] = None
            self.last_elapsed_ms: Optional[float] = None
            self._game_streak: tuple = (None, 0)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- запуск и остановка ---

    def start(self, window: str, *, interval: float = DEFAULT_INTERVAL, mode: str = CONTINUOUS,
              follow_game: bool = True, save: bool = False) -> None:
        if mode not in MODES:
            raise ValueError(f"режим должен быть одним из: {', '.join(MODES)}")
        self.stop()
        self.reset()
        with self._lock:
            self.window = window
            self.interval = min(max(float(interval), MIN_INTERVAL), MAX_INTERVAL)
            self.mode = mode
            self.follow_game = follow_game
            self.save_dir = FRAMES_DIR / datetime.now().strftime("%Y-%m-%d") if save else None
        self._capture = self._capture_factory(window)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="screen-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:   # наблюдатель не должен падать от одного кадра
                with self._lock:
                    self.error = f"{type(error).__name__}: {error}"
                    self.counters["failed"] += 1
            if self._stop.wait(self.interval):
                break

    # --- один шаг ---

    def tick(self) -> None:
        """Снять кадр; если экран изменился и режим постоянный — разобрать его."""
        grab = self._capture.grab()
        if grab.image is None:
            with self._lock:
                self.skipped[grab.skipped] += 1
                self.last_skip = grab.skipped
            return
        if frames.is_black(grab.image):
            with self._lock:
                self.skipped["black"] += 1
                self.last_skip = self.warning = BLACK_WARNING
            return
        frame = Frame(grab.image, frames.signature(grab.image), self._clock(), FRESH_SECONDS,
                      grab.title)
        with self._lock:
            self.frame = frame
            self.counters["captured"] += 1
            self.last_skip = self.warning = None
            fresh = frames.changed(frame.signature, self.facts_signature)
            if not fresh:
                self.counters["unchanged"] += 1
        if fresh and self.mode == CONTINUOUS:
            self._describe(frame)

    def upload(self, image: Image.Image) -> Description:
        """Снимок, присланный вручную, — проверка без игры. Разбирается сразу."""
        frame = Frame(image.convert("RGB"), frames.signature(image), self._clock(), UPLOAD_SECONDS)
        with self._lock:
            self.frame = frame
            self.counters["uploaded"] += 1
        return self._describe(frame)

    def _describe(self, frame: Frame) -> Description:
        with self._describe_lock:
            with self._lock:
                if self.facts is not None and not frames.changed(frame.signature,
                                                                 self.facts_signature):
                    # Пока ждали очереди, такой же экран уже разобрал другой поток.
                    return Description(self.facts, 0.0, {})
            try:
                description = self.model.describe(frame.image, hint=frame.title)
            except VisionUnavailable as error:
                with self._lock:
                    self.error = str(error)
                    self.counters["failed"] += 1
                raise
            with self._lock:
                self.facts = description.facts
                self.facts_signature = frame.signature
                self.facts_at = self._clock()
                self.last_elapsed_ms = description.elapsed_ms
                self.counters["described"] += 1
                self.error = None
                game = self._vote(description.facts.game)
                save_dir = self.save_dir if frame.ttl == FRESH_SECONDS else None
            if save_dir is not None:
                self._save(save_dir, frame, description)
            if game and self.on_game is not None:
                self.on_game(game)
            return description

    @staticmethod
    def _save(directory: Path, frame: Frame, description: Description) -> None:
        """Кадр и то, что модель на нём увидела, — заготовка для разметки и замера."""
        directory.mkdir(parents=True, exist_ok=True)
        stem = datetime.now().strftime("%H%M%S-%f")[:-3]
        frame.image.save(directory / f"{stem}.jpg", quality=90)
        (directory / f"{stem}.json").write_text(json.dumps(
            {"window": frame.title, "facts": description.facts.to_dict(),
             "elapsed_ms": round(description.elapsed_ms), "usage": description.usage},
            ensure_ascii=False, indent=2), encoding="utf-8")

    def _vote(self, game: Optional[str]) -> Optional[str]:
        """Игру меняем, только если подряд несколько кадров назвали одну и ту же."""
        if not self.follow_game or not game:
            self._game_streak = (None, 0)
            return None
        name, count = self._game_streak
        count = count + 1 if name and name.lower() == game.lower() else 1
        self._game_streak = (game, count)
        return game if count == GAME_VOTES else None

    # --- для разговора ---

    def context(self) -> Optional[SceneFacts]:
        """Факты о текущем экране для вопроса игрока либо None, если экрана нет.

        Если последний разобранный кадр устарел или экран с тех пор изменился, кадр
        разбирается сейчас — в режиме «по вопросу» это обычный путь.
        """
        with self._lock:
            frame = self.frame
            if frame is None or self._clock() - frame.taken_at > frame.ttl:
                return None
            up_to_date = (self.facts is not None
                          and not frames.changed(frame.signature, self.facts_signature))
            if up_to_date:
                return self.facts
        try:
            return self._describe(frame).facts
        except VisionUnavailable:
            return None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = self._clock()
            frame_age = None if self.frame is None else round(now - self.frame.taken_at, 1)
            return {
                "running": self.running,
                "window": self.window,
                "interval": self.interval,
                "mode": self.mode,
                "follow_game": self.follow_game,
                "saving": None if self.save_dir is None else str(self.save_dir),
                "counters": dict(self.counters),
                "skipped": dict(self.skipped),
                "last_skip": self.last_skip,
                "warning": self.warning,
                "error": self.error,
                "frame_age": frame_age,
                "facts": None if self.facts is None else self.facts.to_dict(),
                "facts_age": None if self.facts_at is None else round(now - self.facts_at, 1),
                "elapsed_ms": None if self.last_elapsed_ms is None else round(self.last_elapsed_ms),
                "model": self.model.model,
            }

    def frame_jpeg(self, width: int = 640) -> Optional[bytes]:
        with self._lock:
            frame = self.frame
        return None if frame is None else frames.encode(frame.image, width)


_shared: Optional[ScreenWatcher] = None


def shared() -> ScreenWatcher:
    global _shared
    if _shared is None:
        _shared = ScreenWatcher()
    return _shared
