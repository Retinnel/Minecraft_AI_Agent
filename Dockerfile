# Образ сервера ассистента.
#
# База знаний в образе не собирается: для этого нужен jar клиента Minecraft. Её собирают на
# хосте (kb.fetch_locale, kb.extract, kb.build) до сборки образа — data/kb/game.ttl попадает
# в образ вместе с data/. Ключи провайдеров передаются при запуске: --env-file .env.
#
#   docker build -t game-assistant .
#   docker run --env-file .env -p 8000:8000 game-assistant

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY api api
COPY models models
COPY orchestrator orchestrator
COPY resolver resolver
COPY solver solver
COPY search search
COPY kb kb
COPY eval eval
COPY static static
COPY frontend frontend
COPY data data
COPY main.py settings.json llm_config.json llm_prompts.json ./

RUN mkdir -p logs && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
