@echo off
REM Start the FastAPI server with uvicorn

if not exist logs mkdir logs
set LOGFILE=%~dp0logs\server.log

echo Activating virtual environment...
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
) else if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
) else (
    echo [ERROR] Virtual environment not found.
    echo Run: python -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo Starting FastAPI server...
echo Server will run on http://127.0.0.1:8000
echo Press Ctrl+C to stop
echo Logs: %LOGFILE%
echo.

echo [%date% %time%] ========== Server starting ========== >> "%LOGFILE%"
python -m uvicorn main:app --host 127.0.0.1 --port 8000

pause
