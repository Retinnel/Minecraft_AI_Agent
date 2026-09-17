import sys

import requests
from PyQt6 import QtWidgets, QtCore, QtGui


class GlassWidget(QtWidgets.QFrame):
    """Базовый виджет с эффектом Glassmorphism."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            GlassWidget {
                background-color: rgba(30, 30, 30, 160);
                border: 1px solid rgba(255, 255, 255, 40);
                border-radius: 15px;
            }
            QLabel {
                color: #e0e0e0;
                font-family: 'Segoe UI', sans-serif;
                background: transparent;
            }
        """)


class OverlayWindow(QtWidgets.QWidget):
    """
    Полноэкранный премиальный оверлей.
    Включается/выключается глобально через F2.
    При показе становится активным окном (кликабельным).
    """

    _response_signal = QtCore.pyqtSignal(str)
    _status_signal = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Game Assistant")
        # Убираем WindowTransparentForInput, чтобы окно было кликабельным по умолчанию
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        
        # Принудительно на весь экран
        self.showFullScreen()
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

        self._last_response = ""
        self._setup_ui()

        self._response_signal.connect(self._update_response_label)
        self._status_signal.connect(self._update_status_label)

        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.timeout.connect(self._poll_backend)
        self._poll_timer.start(500)

    def _setup_ui(self) -> None:
        layout = QtWidgets.QGridLayout(self)
        layout.setContentsMargins(60, 60, 60, 60)
        layout.setSpacing(30)

        # --- Центральный блок управления ---
        center_panel = GlassWidget(self)
        center_panel.setFixedSize(320, 200)
        center_layout = QtWidgets.QVBoxLayout(center_panel)

        self._start_button = QtWidgets.QPushButton("START SYSTEM")
        self._start_button.setFixedSize(240, 60)
        self._start_button.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
        self._start_button.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6366f1, stop:1 #a855f7);
                color: white;
                border-radius: 12px;
                font-weight: 800;
                font-size: 15px;
                letter-spacing: 1px;
                border: 1px solid rgba(255,255,255,0.1);
            }
            QPushButton:hover { 
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4f46e5, stop:1 #9333ea);
                border: 1px solid rgba(255,255,255,0.3);
            }
        """)
        self._start_button.clicked.connect(self._on_start_clicked)
        center_layout.addWidget(self._start_button, alignment=QtCore.Qt.AlignmentFlag.AlignCenter)

        self._status_label = QtWidgets.QLabel("SYSTEM READY")
        self._status_label.setStyleSheet("color: #94a3b8; font-weight: 600; font-size: 12px;")
        center_layout.addWidget(self._status_label, alignment=QtCore.Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(center_panel, 1, 1, QtCore.Qt.AlignmentFlag.AlignCenter)

        # --- Блок ответов (справа) ---
        response_container = GlassWidget(self)
        response_container.setFixedWidth(420)
        resp_layout = QtWidgets.QVBoxLayout(response_container)
        
        resp_title = QtWidgets.QLabel("AI INTELLIGENCE")
        resp_title.setStyleSheet("color: #818cf8; font-weight: 900; font-size: 14px; letter-spacing: 2px;")
        resp_layout.addWidget(resp_title)

        self._response_label = QtWidgets.QLabel("Waiting for command...")
        self._response_label.setWordWrap(True)
        self._response_label.setStyleSheet("font-size: 16px; color: #f8fafc; line-height: 1.6;")
        self._response_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        resp_layout.addWidget(self._response_label)
        
        layout.addWidget(response_container, 1, 2)

        # --- Панель горячих клавиш (внизу) ---
        self._hotkey_panel = QtWidgets.QWidget()
        hotkey_layout = QtWidgets.QHBoxLayout(self._hotkey_panel)
        hotkey_layout.setSpacing(15)

        hotkeys = [
            ("L_CTRL", "Push to Talk"),
            ("F2", "Toggle Assistant")
        ]

        for key, desc in hotkeys:
            badge = QtWidgets.QWidget()
            badge.setStyleSheet("background: rgba(15, 23, 42, 0.6); border-radius: 8px; border: 1px solid rgba(255,255,255,0.05);")
            b_layout = QtWidgets.QHBoxLayout(badge)
            b_layout.setContentsMargins(10, 5, 10, 5)
            
            k_lbl = QtWidgets.QLabel(key)
            k_lbl.setStyleSheet("color: #6366f1; font-weight: 900; font-size: 11px;")
            d_lbl = QtWidgets.QLabel(desc)
            d_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: 600;")
            
            b_layout.addWidget(k_lbl)
            b_layout.addWidget(d_lbl)
            hotkey_layout.addWidget(badge)

        layout.addWidget(self._hotkey_panel, 2, 0, 1, 3, QtCore.Qt.AlignmentFlag.AlignCenter)

        # Сетка
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(2, 0)
        layout.setRowStretch(0, 1)
        layout.setRowStretch(1, 0)
        layout.setRowStretch(2, 0)

    def _on_start_clicked(self):
        try:
            r = requests.post("http://127.0.0.1:8000/api/start", timeout=1.0)
            if r.status_code == 200:
                self._start_button.setText("ACTIVE")
                self._start_button.setEnabled(False)
                self._start_button.setStyleSheet("background: rgba(34, 197, 94, 0.2); color: #22c55e; border: 1px solid #22c55e; border-radius: 12px; font-weight: 800;")
                self._status_label.setText("LISTENING FOR INPUT")
        except:
            self._status_label.setText("OFFLINE")

    def _poll_backend(self) -> None:
        try:
            r = requests.get("http://127.0.0.1:8000/api/latest_response", timeout=0.3)
            if r.status_code == 200:
                answer = r.json().get("answer", "")
                if answer and answer != self._last_response:
                    self._last_response = answer
                    self._response_signal.emit(answer)

            r2 = requests.get("http://127.0.0.1:8000/api/status", timeout=0.3)
            if r2.status_code == 200:
                data = r2.json()
                self._status_signal.emit(data.get("status", "IDLE"))
                
                visible = data.get("visible", True)
                if visible != self.isVisible():
                    if visible:
                        self.showFullScreen()
                        self.activateWindow() # Фокус на окно при показе
                    else:
                        self.hide()
        except:
            pass

    @QtCore.pyqtSlot(str)
    def _update_response_label(self, text: str) -> None:
        self._response_label.setText(text)

    @QtCore.pyqtSlot(str)
    def _update_status_label(self, status: str) -> None:
        self._status_label.setText(status.upper())

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        # Локальные клавиши больше не нужны, так как работают глобальные хоткеи
        pass


def run() -> None:
    app = QtWidgets.QApplication(sys.argv)
    window = OverlayWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()

