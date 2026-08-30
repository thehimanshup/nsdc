@echo off
REM ============================================================
REM  Sovereign AI Substrate PoC — one-click runner (Windows)
REM
REM  Usage:
REM    run_substrate.bat            (mock mode — no keys needed)
REM    run_substrate.bat sarvam     (live Sarvam composition + judge)
REM
REM  Does everything: venv, dependencies, corpus ingest, synthetic
REM  events, then starts the app and opens the demo console.
REM ============================================================
setlocal
cd /d "%~dp0"

REM Provider precedence: argument > .env LLM_PROVIDER > mock
set "PROVIDER=%~1"

echo.
echo  [1/5] Python virtual environment...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 (
        echo  ERROR: could not create venv. Is Python 3.10+ on PATH?
        pause & exit /b 1
    )
)
call ".venv\Scripts\activate.bat"

echo  [2/5] Dependencies...
python -c "import fastapi, uvicorn, pydantic" 2>nul
if errorlevel 1 (
    echo        installing from requirements.txt - first run takes a minute...
    python -m pip install --quiet --upgrade pip
    python -m pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo  ERROR: pip install failed. Check network/proxy and re-run.
        pause & exit /b 1
    )
)

echo  [3/5] Corpus index...
if not exist "data\manifests\CURRENT" (
    python -m backend.substrate.ingest
    if errorlevel 1 ( echo  ERROR: ingestion failed. & pause & exit /b 1 )
) else (
    echo        found - skipping. Delete data\manifests\CURRENT to force rebuild.
)

echo  [4/5] Synthetic event store...
if not exist "data\skilling_events.db" (
    python -m backend.substrate.events >nul
) else (
    echo        found - skipping.
)

set "SUBSTRATE_RAG=true"
set "APP_ENV=development"
set "AUTO_SEED_CORPORA=false"
if not "%PROVIDER%"=="" (
    set "LLM_PROVIDER=%PROVIDER%"
    echo  [5/5] Starting server - provider: %PROVIDER% ^(from argument^)...
) else (
    echo  [5/5] Starting server - provider from .env LLM_PROVIDER ^(fallback: mock^)...
)

echo.
echo  ------------------------------------------------------------
echo   Demo console : http://localhost:8000/substrate-demo
echo   Logins       : meena/learner-demo  rajesh/officer-demo
echo                  iyer/sme-demo       admin/admin-demo
echo   Demo script  : DEMO_SCRIPT.md      Stop server: Ctrl+C
echo  ------------------------------------------------------------
echo.

start "" /b cmd /c "timeout /t 4 >nul & start http://localhost:8000/substrate-demo"
python -m uvicorn backend.main:app --port 8000

endlocal
