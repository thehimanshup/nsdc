@echo off
REM ============================================================
REM  STEP 1 of 2 - run this on a machine WITH internet access.
REM  It downloads every dependency into a local "wheelhouse"
REM  folder so the app can be installed on an offline PC.
REM
REM  IMPORTANT: run this on the SAME OS + SAME Python version
REM  (e.g. Windows + Python 3.11) as the target PC, because some
REM  packages (pydantic, cryptography, uvicorn) ship compiled
REM  wheels that are specific to the OS and Python version.
REM ============================================================
setlocal
cd /d "%~dp0"

echo Python version on this machine:
python --version

echo.
echo Downloading dependencies into .\wheelhouse ...
python -m pip download -r requirements.txt -d wheelhouse
if errorlevel 1 (
    echo ERROR: download failed. Check your internet connection / proxy.
    pause
    exit /b 1
)

REM Also stage pip/setuptools/wheel so the offline PC can bootstrap if needed.
python -m pip download pip setuptools wheel -d wheelhouse

echo.
echo ============================================================
echo  DONE. Now copy the ENTIRE gov-services-ai folder (it must include
echo  the new "wheelhouse" folder) to the offline PC, and run
echo  run_offline.bat there.
echo ============================================================
pause
endlocal
