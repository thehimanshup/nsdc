# =========================================================
# Windows Server install — runs the app as a service via NSSM.
# Prereqs:
#   - Python 3.10+ on PATH
#   - NSSM installed (https://nssm.cc) and on PATH
#   - Code deployed to C:\govapp\phase6e  with .env filled in (chmod-equivalent: restrict ACL)
# Run in an elevated PowerShell.
# =========================================================

$AppDir = "C:\govapp\phase6e"
$Python = "$AppDir\.venv\Scripts\python.exe"

# --- venv + deps (first run only) ---
if (-not (Test-Path "$AppDir\.venv")) {
    python -m venv "$AppDir\.venv"
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -r "$AppDir\requirements.txt"
    # Live voice only:  & $Python -m pip install -r "$AppDir\requirements-voice.txt"
}

# --- Main API + UI service (single worker) ---
nssm install GovApp $Python "-m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log"
nssm set GovApp AppDirectory $AppDir
nssm set GovApp AppStdout "$AppDir\logs\govapp.out.log"
nssm set GovApp AppStderr "$AppDir\logs\govapp.err.log"
nssm set GovApp Start SERVICE_AUTO_START
nssm set GovApp AppExit Default Restart

# --- LiveKit voice worker (only if live voice is in scope) ---
# nssm install GovAppVoice $Python "-m backend.livekit_agent_worker start"
# nssm set GovAppVoice AppDirectory $AppDir
# nssm set GovAppVoice AppStdout "$AppDir\logs\voice.out.log"
# nssm set GovAppVoice AppStderr "$AppDir\logs\voice.err.log"
# nssm set GovAppVoice Start SERVICE_AUTO_START

New-Item -ItemType Directory -Force "$AppDir\logs" | Out-Null
nssm start GovApp
# nssm start GovAppVoice

Write-Host "Installed. Verify with:  nssm status GovApp   and   curl http://127.0.0.1:8000/api/health"
