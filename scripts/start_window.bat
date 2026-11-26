@echo off
rem Окно ассистента поверх игры. Сервер должен быть запущен (scripts\start_server.bat).
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m desktop.window %*
