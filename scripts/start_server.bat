@echo off
chcp 65001 >nul
REM Запуск сервера ассистента. Скрипт можно запускать откуда угодно:
REM он сам переходит в корень проекта.

cd /d "%~dp0.."

if not exist logs mkdir logs
set LOGFILE=%CD%\logs\server.log

if not exist .venv\Scripts\python.exe (
    echo [ОШИБКА] Не найдено окружение .venv.
    echo Создать: python -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo Сервер: http://127.0.0.1:8000   (остановка — Ctrl+C)
echo Журнал: %LOGFILE%
echo.

echo [%date% %time%] ========== Server starting ========== >> "%LOGFILE%"
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000

pause
