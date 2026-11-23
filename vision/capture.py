"""Захват окна игры: только снимок уже выведенного на экран изображения.

Модуль устроен так, чтобы не давать античиту (BattlEye, Easy Anti-Cheat) поводов: они
следят за программами, которые вмешиваются в процесс игры. Поэтому:

- снимается изображение окна, уже выведенное на экран, тем же способом, что у программ
  записи экрана (GDI через mss). В процесс игры ничего не встраивается, его память не
  читается, отрисовка не перехватывается;
- в модуле нет ни одного вызова ввода: ассистент ничего не нажимает и не двигает мышь;
- снимается только клиентская область окна игры и только тогда, когда это окно на
  переднем плане. Иначе в кадр попали бы другие программы пользователя, а кадр уходит
  модели в облако;
- игра должна идти в окне или в окне без рамки: в эксклюзивном полноэкранном режиме
  захват видит чёрный кадр, и наблюдатель сообщает об этом.

Решение о том, разрешает ли такую программу издатель конкретной игры, остаётся за
пользователем: античит её не видит, но правила игры могут запрещать любые сторонние
программы.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import List, Optional

from PIL import Image

IS_WINDOWS = sys.platform == "win32"

# Окна меньше этого — служебные или свёрнутые; снимать их бессмысленно.
MIN_SIDE = 64

# Причины пропуска кадра: показываются на странице, чтобы было видно, почему ассистент «не
# видит» игру.
NO_WINDOW = "окно игры не найдено"
BACKGROUND = "окно игры не на переднем плане"
MINIMIZED = "окно игры свёрнуто"
UNSUPPORTED = "захват окна работает только в Windows"


@dataclass(frozen=True)
class Window:
    handle: int
    title: str


@dataclass
class Grab:
    """Итог попытки снять кадр: изображение либо причина, по которой его нет."""

    image: Optional[Image.Image] = None
    skipped: Optional[str] = None
    title: Optional[str] = None       # заголовок окна — подсказка модели, что за игра


_dpi_aware = False


def _make_dpi_aware() -> None:
    """Координаты окна в физических пикселях: иначе при масштабе 125 % кадр съезжает."""
    global _dpi_aware
    if _dpi_aware or not IS_WINDOWS:
        return
    _dpi_aware = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def list_windows() -> List[Window]:
    """Видимые окна верхнего уровня с заголовком — из них пользователь выбирает игру."""
    if not IS_WINDOWS:
        return []
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: List[Window] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(handle, _param):
        if user32.IsWindowVisible(handle) and not user32.IsIconic(handle):
            length = user32.GetWindowTextLengthW(handle)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(handle, buffer, length + 1)
                title = buffer.value.strip()
                if title:
                    found.append(Window(int(handle), title))
        return True

    user32.EnumWindows(callback_type(collect), 0)
    return found


def find_window(title_part: str) -> Optional[Window]:
    needle = (title_part or "").strip().lower()
    if not needle:
        return None
    for window in list_windows():
        if needle in window.title.lower():
            return window
    return None


class WindowCapture:
    """Снимок клиентской области окна, найденного по части заголовка."""

    def __init__(self, title_part: str, *, foreground_only: bool = True) -> None:
        self.title_part = title_part
        self.foreground_only = foreground_only

    def grab(self) -> Grab:
        if not IS_WINDOWS:
            return Grab(skipped=UNSUPPORTED)
        from ctypes import wintypes

        import mss

        _make_dpi_aware()
        user32 = ctypes.windll.user32
        window = find_window(self.title_part)
        if window is None:
            return Grab(skipped=NO_WINDOW)
        handle = wintypes.HWND(window.handle)
        if user32.IsIconic(handle):
            return Grab(skipped=MINIMIZED)
        if self.foreground_only and int(user32.GetForegroundWindow() or 0) != window.handle:
            return Grab(skipped=BACKGROUND)

        rect = wintypes.RECT()
        if not user32.GetClientRect(handle, ctypes.byref(rect)):
            return Grab(skipped=NO_WINDOW)
        origin = wintypes.POINT(0, 0)
        user32.ClientToScreen(handle, ctypes.byref(origin))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < MIN_SIDE or height < MIN_SIDE:
            return Grab(skipped=MINIMIZED)

        with mss.mss() as screen:
            shot = screen.grab({"left": origin.x, "top": origin.y, "width": width, "height": height})
        return Grab(image=Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"),
                    title=window.title)
