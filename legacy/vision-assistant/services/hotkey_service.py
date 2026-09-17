from pynput import keyboard

from backend.core.container import container


class HotkeyService:
    """
    Фоновый сервис глобальных горячих клавиш.
    Реализует Push-to-Talk (PTT): запись начинается при нажатии,
    останавливается и обрабатывается при отпускании клавиши.
    """

    def __init__(self):
        self.ptt_key = keyboard.Key.ctrl_l
        self.toggle_visible_key = keyboard.Key.f2
        self._is_pressed = False  # Защита от повторных событий при удержании клавиши
        self.listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )

    def _on_press(self, key) -> None:
        try:
            # 1. Обработка F2 (Глобально)
            if key == self.toggle_visible_key:
                container.overlay_visible = not container.overlay_visible
                print(f"[HOTKEY] Overlay visibility: {container.overlay_visible}")
                return

            # 2. Обработка PTT
            if key != self.ptt_key or self._is_pressed:
                return
            if not container.is_active:
                return
            self._is_pressed = True
            container.voice_listener.start_recording()
            container.set_overlay_status("🔴 Recording...")
        except Exception as e:
            print(f"[ERROR] Hotkey press error: {e}")

    def _on_release(self, key) -> None:
        if key != self.ptt_key:
            return
        if not container.is_active:
            return
        self._is_pressed = False
        try:
            container.voice_listener.stop_recording()
            container.set_overlay_status("⏳ Processing...")
        except Exception as e:
            print(f"[ERROR] PTT release error: {e}")

    def start(self) -> None:
        if not self.listener.running:
            self.listener.start()
            print("[INFO] Hotkey Service (PTT) started.")

    def stop(self) -> None:
        if self.listener.running:
            self.listener.stop()
            print("[INFO] Hotkey Service (PTT) stopped.")


hotkey_service = HotkeyService()

