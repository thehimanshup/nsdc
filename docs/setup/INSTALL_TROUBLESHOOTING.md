# Install error: "Connection to pypi.org timed out" / "No matching distribution found for fastapi"

This is **not** an app bug. `run.bat` tries to download dependencies (FastAPI,
uvicorn, …) from the Python package index (`pypi.org`), and on the new machine
pip **cannot reach the internet**. The line `from versions: none` means pip
couldn't fetch *any* package list — a pure connectivity/firewall/proxy problem.

Pick whichever matches the machine:

## A. The PC just needs internet
Connect it to a network that can reach `pypi.org`, then run `run.bat` again.
Quick test:  `ping pypi.org`  and in a browser open `https://pypi.org`.

## B. The PC is behind a corporate proxy
Tell pip about the proxy (ask IT for the host:port), then run `run.bat`:
```
set HTTPS_PROXY=http://YOUR_PROXY_HOST:PORT
set HTTP_PROXY=http://YOUR_PROXY_HOST:PORT
run.bat
```
If you also get an SSL/certificate error through the proxy, add trusted hosts:
```
python -m pip install -r requirements.txt ^
  --proxy http://YOUR_PROXY_HOST:PORT ^
  --trusted-host pypi.org --trusted-host files.pythonhosted.org ^
  --trusted-host pypi.python.org
```

## C. The PC is offline / air-gapped (recommended: offline bundle)
Use the two helper scripts shipped alongside this file:

1. On a machine **with** internet — and ideally the **same OS + same Python
   version** as the target PC — run:
   ```
   make_offline_bundle.bat
   ```
   This creates a `wheelhouse\` folder containing every dependency.

2. Copy the **entire `gov-services-ai` folder** (now including `wheelhouse\`) to the
   offline PC, and run:
   ```
   run_offline.bat
   ```
   It installs everything from `wheelhouse\` with no internet and starts the app.

### Why "same OS + same Python version"?
A few dependencies (pydantic, cryptography, uvicorn[standard]) ship **compiled**
wheels that are specific to the operating system and Python version. If the
download machine is Windows + Python 3.11, the target PC should also be
Windows + Python 3.11. Check with `python --version` on both. If they differ,
rebuild the bundle on a matching machine.

## Prerequisite on every machine
Python 3.10+ must be installed and on PATH (`python --version` should work).
Download from https://www.python.org/downloads/ (tick "Add Python to PATH").
