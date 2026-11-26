"""Окно ассистента поверх игры: небольшое, всегда сверху, со своим разговором.

Чат Minecraft живёт в моде. Для игры без мода спрашивать через него нелогично, поэтому у
ассистента есть отдельное окно: игра выбирается в нём самом, разговор об этой игре свой и
на чат Minecraft не влияет (`api/window.py`, страница `/mini`).

Запуск (сервер должен работать):
    .venv/Scripts/python.exe -m desktop.window
    .venv/Scripts/python.exe -m desktop.window --game Terraria

Окно рисует pywebview (`pip install pywebview`). Без него страница открывается в окне
браузера без рамок — работает так же, но поверх игры не держится.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

DEFAULT_URL = "http://127.0.0.1:8000"
WIDTH, HEIGHT = 400, 620


def page(base: str, game: str = "") -> str:
    url = base.rstrip("/") + "/mini"
    return url + ("?game=" + urllib.parse.quote(game) if game else "")


def server_alive(base: str) -> bool:
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/api/window/games", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def open_fallback(url: str) -> None:
    """Без pywebview: окно браузера в режиме приложения, иначе обычная вкладка."""
    for browser in ("msedge", "chrome"):
        path = shutil.which(browser)
        if path:
            subprocess.Popen([path, f"--app={url}", f"--window-size={WIDTH},{HEIGHT}"])
            return
    webbrowser.open(url)


def hide_from_capture(window) -> None:
    """Убирает окно ассистента из снимков экрана (Windows 10 2004 и новее).

    Окно висит поверх игры и попадало в кадр, который разбирает модель зрения: закрывало
    часть сцены и читалось как надпись «Ассистент». Игрок его видит по-прежнему.
    """
    try:
        import ctypes
        handle = int(window.native.Handle.ToInt64())
        ctypes.windll.user32.SetWindowDisplayAffinity(handle, 0x11)   # WDA_EXCLUDEFROMCAPTURE
    except Exception:
        pass   # не Windows или старая сборка: окно просто остаётся в кадре


def main() -> int:
    parser = argparse.ArgumentParser(description="Окно ассистента поверх игры.")
    parser.add_argument("--url", default=DEFAULT_URL, help="адрес сервера ассистента")
    parser.add_argument("--game", default="", help="игра, с которой начать разговор")
    parser.add_argument("--no-top", action="store_true", help="не держать окно поверх остальных")
    args = parser.parse_args()

    if not server_alive(args.url):
        print(f"Сервер ассистента не отвечает по адресу {args.url}. "
              f"Запусти его: scripts/start_server.bat", file=sys.stderr)
        return 1
    url = page(args.url, args.game)
    try:
        import webview
    except ImportError:
        open_fallback(url)
        return 0
    window = webview.create_window("Игровой ассистент", url, width=WIDTH, height=HEIGHT,
                                   on_top=not args.no_top, min_size=(320, 360))
    window.events.shown += lambda: hide_from_capture(window)
    # Не приватный режим: разрешение на микрофон, выданное один раз, запоминается.
    webview.start(private_mode=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
