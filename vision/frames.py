"""Обработка кадров без модели: изменился ли экран, не чёрный ли кадр, сжатие для отправки.

Модель разбирает только изменившиеся кадры. Сравнение идёт по уменьшенной серой копии: так
оно занимает доли миллисекунды и не реагирует на шум сжатия и мерцание. В диалоге, меню и
инвентаре экран стоит на месте, и каждый такой экран разбирается один раз.
"""

from __future__ import annotations

import io

from PIL import Image, ImageChops, ImageStat

SIGNATURE_SIZE = (32, 18)

# Средняя разница яркости (0–255) уменьшенных кадров, начиная с которой экран считается
# изменившимся. Открытое окно инвентаря или новая реплика диалога дают десятки единиц,
# покачивание камеры на месте — единицы.
CHANGE_THRESHOLD = 6.0

# Ярче этого в эксклюзивном полноэкранном режиме не бывает ни один пиксель: захват видит
# только чёрный прямоугольник. Тёмная сцена игры даёт хотя бы интерфейс и блики.
BLACK_MAX = 4

# Ширина кадра для модели. Замер 2026-11-23 на синтетических кадрах: при 640 пикселях модель
# узнала 9 предметов из 9, при 1280 — 8 из 9, но мелкий шрифт настоящих игр может требовать
# большего; изображение в 1280 пикселей стоит около 550 входных токенов.
MAX_WIDTH = 1280
JPEG_QUALITY = 85


def signature(image: Image.Image) -> Image.Image:
    return image.convert("L").resize(SIGNATURE_SIZE, Image.BILINEAR)


def difference(first: Image.Image, second: Image.Image) -> float:
    return ImageStat.Stat(ImageChops.difference(first, second)).mean[0]


def changed(current: Image.Image, previous: Image.Image | None,
            threshold: float = CHANGE_THRESHOLD) -> bool:
    return previous is None or difference(current, previous) >= threshold


def is_black(image: Image.Image) -> bool:
    low, high = image.convert("L").resize((64, 36)).getextrema()
    return high <= BLACK_MAX


def encode(image: Image.Image, max_width: int = MAX_WIDTH) -> bytes:
    """JPEG для отправки модели: не шире max_width, пропорции сохранены."""
    if image.width > max_width:
        height = round(image.height * max_width / image.width)
        image = image.resize((max_width, height), Image.LANCZOS)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()
