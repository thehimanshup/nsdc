@echo off
REM ========================================================
REM Quick-start script for Windows.
REM .env loading is handled inside Python (backend/config.py)
REM via python-dotenv — no fragile batch parsing.
REM ========================================================
setlocal
cd /d "%~dp0"

REM --- Create / reuse virtualenv ---
if not exist .venv (
    echo Creating virtualenv...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: 'python -m venv .venv' failed. Is Python 3.10+ on PATH?
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: could not activate venv.
    pause
    exit /b 1
)

REM --- Install dependencies ---
echo Installing dependencies...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

REM --- Defaults if not set via .env or shell ---
if "%HOST%"=="" set HOST=127.0.0.1
if "%PORT%"=="" set PORT=8000

echo.
echo ============================================================
echo  Government Services Multi-Agent Backend (Phase 1)
echo  Open in browser:  http://%HOST%:%PORT%/
echo  Press Ctrl+C to stop.
echo ============================================================
echo.

python -m uvicorn backend.main:app --reload --host %HOST% --port %PORT%

endlocal
