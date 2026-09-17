@echo off
REM Start Minecraft client with the mod (requires JDK 17)

if not exist logs mkdir logs
set LOGFILE=%~dp0logs\minecraft_client.log

echo Starting Minecraft client...
echo This may take a moment...
echo Logs: %LOGFILE%
echo.

echo [%date% %time%] ========== Minecraft client starting ========== >> "%LOGFILE%"

cd mod\minecraft-mod

echo Running Fabric dev client...
powershell -Command ".\gradlew.bat runClient 2>&1 | Tee-Object -Append -FilePath '%LOGFILE%'"

pause
