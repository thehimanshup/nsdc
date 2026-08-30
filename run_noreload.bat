@echo off
REM ============================================================
REM  ALL-IN-ONE launcher for VOICE / LiveKit testing.
REM  - Sets up the venv + installs app and voice dependencies.
REM  - Starts the LiveKit agent worker in its OWN window.
REM  - Runs the backend in THIS window WITHOUT --reload.
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist .venv (
    echo Creating virtualenv...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
if errorlevel 1 goto venverr

echo Installing app dependencies...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto piperr

echo Checking voice pipeline dependencies...
python -c "import livekit.plugins.silero, livekit.plugins.sarvam" 1>nul 2>nul
if errorlevel 1 goto installvoice
goto havevoice

:installvoice
echo Installing voice dependencies - LiveKit + Sarvam + Silero ...
python -m pip install --disable-pip-version-check "livekit-agents[sarvam]~=1.5" livekit-plugins-silero
if errorlevel 1 goto voiceerr

:havevoice
if "%HOST%"=="" set HOST=127.0.0.1
if "%PORT%"=="" set PORT=8000

echo Starting LiveKit agent worker in a separate window...
start "Voice Agent Worker - LiveKit" cmd /k ".venv\Scripts\activate.bat && python -m backend.livekit_agent_worker dev"

timeout /t 3 /nobreak >nul

echo.
echo ============================================================
echo  Government Services Backend - stable, no auto-reload
echo  Open:  http://%HOST%:%PORT%/
echo  A second window is running the Voice Agent Worker.
echo  Press Ctrl+C here to stop the backend.
echo ============================================================
echo.
python -m uvicorn backend.main:app --host %HOST% --port %PORT%
goto end

:venverr
echo ERROR: could not create or activate the virtualenv. Is Python 3.10+ on PATH?
pause
exit /b 1

:piperr
echo ERROR: app dependency install failed.
pause
exit /b 1

:voiceerr
echo ERROR: voice dependency install failed. See the messages above.
pause
exit /b 1

:end
endlocal
