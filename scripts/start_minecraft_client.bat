@echo off
chcp 65001 >nul
REM Запуск dev-клиента Minecraft с модом (нужен JDK 17). Скрипт можно запускать
REM откуда угодно: он сам переходит в корень проекта.

cd /d "%~dp0.."

if not exist logs mkdir logs
set LOGFILE=%CD%\logs\minecraft_client.log

echo Запуск клиента Minecraft, это займёт пару минут...
echo Журнал: %LOGFILE%
echo.

echo [%date% %time%] ========== Minecraft client starting ========== >> "%LOGFILE%"

cd mod\minecraft-mod
powershell -Command ".\gradlew.bat runClient 2>&1 | Tee-Object -Append -FilePath '%LOGFILE%'"

pause
