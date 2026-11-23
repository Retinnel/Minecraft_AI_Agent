"""Распознавание экрана: снимок окна игры, разбор кадра моделью, факты для конвейера.

Модель зрения видит экран и называет, что на нём: сцену, предмет под курсором, реплику и
варианты диалога. Отвечает игроку по-прежнему конвейер: факты с экрана попадают в промпт
извлечения намерения, а дальше вопрос решают онтология или поиск в сети.
"""

from vision.scene import SceneFacts, VisionModel, VisionUnavailable
from vision.watcher import CONTINUOUS, ON_DEMAND, ScreenWatcher, shared

__all__ = ["CONTINUOUS", "ON_DEMAND", "SceneFacts", "ScreenWatcher", "VisionModel",
           "VisionUnavailable", "shared"]
