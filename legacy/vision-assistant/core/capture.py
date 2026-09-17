import threading
import time
from collections import deque

try:
    import dxcam
except ImportError:
    dxcam = None

try:
    import mss
    import mss.tools
except ImportError:
    mss = None

import numpy as np


class ScreenCapture:
    def __init__(self, monitor=1, fps=15, buffer_seconds=60):
        self.monitor = monitor
        self.fps = fps
        self.latest_frame = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self.capture = None
        self.frame_buffer = deque(maxlen=buffer_seconds)  # По одному кадру в секунду
        self.last_buffer_time = 0

        if dxcam is not None:
            try:
                self.capture = dxcam.create(region=None, output_color="BGR")
            except Exception:
                self.capture = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        while not self._stop_event.is_set():
            frame = None
            if self.capture is not None:
                try:
                    frame = self.capture.get_latest_frame()
                except Exception:
                    frame = None
            if frame is None and mss is not None:
                try:
                    with mss.mss() as sct:
                        monitor = sct.monitors[self.monitor] if self.monitor < len(sct.monitors) else sct.monitors[1]
                        sct_img = sct.grab(monitor)
                        frame = np.array(sct_img)
                        frame = frame[:, :, :3]  # RGBA -> RGB
                except Exception:
                    frame = None
            if frame is None:
                frame = np.zeros((720, 1280, 3), dtype=np.uint8)

            with self._lock:
                self.latest_frame = frame
                # Добавляем в буфер раз в секунду
                current_time = time.time()
                if current_time - self.last_buffer_time >= 1.0:
                    self.frame_buffer.append(frame.copy())
                    self.last_buffer_time = current_time

            time.sleep(1 / max(1, self.fps))

    def get_frame(self):
        with self._lock:
            if self.latest_frame is None:
                return None
            return self.latest_frame.copy()

    def get_frame_buffer(self):
        with self._lock:
            return list(self.frame_buffer)
