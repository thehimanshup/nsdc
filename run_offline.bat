@echo off
REM ============================================================
REM  STEP 2 of 2 - run this on the OFFLINE PC.
REM  Installs every dependency from the local .\wheelhouse folder
REM  (no internet needed), then starts the server.
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist wheelhouse (
    echo ERROR: no "wheelhouse" folder found next to this script.
    echo Run make_offline_bundle.bat on an internet-connected PC first,
    echo then copy the whole folder here.
    pause
    exit /b 1
)

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

REM --- Install dependencies from the local wheelhouse ONLY (no PyPI) ---
echo Installing dependencies from local wheelhouse (offline)...
python -m pip install --no-index --find-links wheelhouse -r requirements.txt
if errorlevel 1 (
    echo ERROR: offline install failed.
    echo Most likely the wheelhouse was built on a different OS or Python
    echo version than this PC. Rebuild make_offline_bundle.bat on a machine
    echo matching this one (same OS, same 'python --version').
    pause
    exit /b 1
)

if "%HOST%"=="" set HOST=127.0.0.1
if "%PORT%"=="" set PORT=8000

echo.
echo ============================================================
echo  Government Services Multi-Agent Backend
echo  Open in browser:  http://%HOST%:%PORT%/
echo  Press Ctrl+C to stop.
echo ============================================================
echo.

python -m uvicorn backend.main:app --reload --host %HOST% --port %PORT%
endlocal
