from backend.core.config import settings

# Системная инструкция для игрового ассистента
_SYSTEM_INSTRUCTION = (
    "Ты — мой ИИ-друг и помощник в играх. "
    "Ты анализируешь игровой экран, даешь советы, предлагаешь тактики, "
    "комментируешь происходящее, подбадриваешь и развлекаешь. "
    "Обращай внимание только на игру и ее составляющие. "
    "Отвечай не более 1000 символов, но по возможности как можно короче, как живой человек, без формальностей. "
    "Можешь использовать сленг и ненормативную лексику, если это уместно. "
    "Иностранные игровые термины пиши по-русски. Пример: Dota → Дота."
    "Не повторяйся без просьбы"
)


def ask_gemini(question: str, image_bytes: bytes = None, history: list = None) -> str:
    """
    Отправляет мультимодальный запрос в Gemini с учетом истории диалога.
    """
    try:
        import google.genai as genai
        from google.genai import types
    except ImportError:
        return "[gemini-unavailable] Пакет google-genai не установлен."

    if not settings.gemini_api_key:
        return "[gemini-no-key] Не найден API ключ."

    client = genai.Client(api_key=settings.gemini_api_key)
    
    # Формируем структуру сообщений
    contents = []
    
    # 1. Добавляем системную инструкцию
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=f"System Instruction: {_SYSTEM_INSTRUCTION}")]))
    contents.append(types.Content(role="model", parts=[types.Part.from_text(text="Понял! Я твой игровой ассистент. Жду команд.")]))

    # 2. Добавляем историю диалога (если есть)
    if history:
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])]))

    # 3. Текущий запрос
    current_parts = [types.Part.from_text(text=question)]
    if image_bytes:
        try:
            current_parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
        except Exception as err:
            return f"[gemini-error] Ошибка упаковки изображения: {err}"
    
    contents.append(types.Content(role="user", parts=current_parts))

    try:
        response = client.models.generate_content(
            model="gemini-flash-lite-latest",
            contents=contents,
        )
        return response.text
    except Exception as e:
        return f"[gemini-error] {e}"

