# LiveKit — step-by-step setup (Windows)

Follow these in order. Steps 1–5 get the real-time streaming pipeline
(Saaras v3 → Sarvam-105B → Bulbul v3) running and let you TALK TO IT TODAY via
the terminal. Step 6 is the browser integration (still to be built).

---

## Step 1 — Create a free LiveKit Cloud project & get keys
1. Go to https://cloud.livekit.io and sign up (free).
2. Create a project (any name, pick the closest region — e.g. Asia/India).
3. Open the project → **Settings → Keys** → create/copy these three values:
   - Project URL, looks like `wss://your-project-xxxx.livekit.cloud`
   - API Key, looks like `APIxxxxxxxx`
   - API Secret, a long random string

## Step 2 — Put the keys in your `.env`
Open `gov-services-ai\.env` and fill the three lines (they're currently empty):
```
LIVEKIT_URL=wss://your-project-xxxx.livekit.cloud
LIVEKIT_API_KEY=APIxxxxxxxx
LIVEKIT_API_SECRET=your-long-secret
```
Make sure `SARVAM_API_KEY=` is also set (it already is).

## Step 3 — Install the voice-worker dependencies
Open a terminal in the `gov-services-ai` folder and activate the venv, then install:
```
.venv\Scripts\activate
pip install -r requirements-voice.txt
```
(This installs `livekit-agents[sarvam,silero]` — only needed on the machine that
runs the worker. The base app doesn't need it.)

## Step 4 — Verify the keys are picked up
Start the backend (`run.bat`) and open `http://127.0.0.1:8000/api/health` in a
browser. You should now see:
```
"livekit_mode": "live"
```
If it still says `"mock"`, the three LIVEKIT_ values aren't being read — re-check
`.env` (no quotes needed, no spaces around `=`) and restart.

## Step 5 — Run the agent worker (this is the streaming brain)
The worker is a SEPARATE process from the backend. Open a **second** terminal in
`gov-services-ai`, activate the venv, and run:

**Talk to it right now, in the terminal (fastest test):**
```
.venv\Scripts\activate
python -m backend.livekit_agent_worker console
```
Speak into your mic — you'll hear the agent reply using the full streaming
pipeline. This is the quickest way to feel the low latency and confirm
Saaras v3 + Sarvam-105B + Bulbul v3 are all working.

**To serve real calls (for the app), run it in dev mode instead:**
```
python -m backend.livekit_agent_worker dev
```
Leave this running. It waits for callers to join rooms and embodies the right
department agent automatically.

So in normal use you have **two terminals**: one running `run.bat` (the backend
+ web app), one running `python -m backend.livekit_agent_worker dev` (the voice
brain).

## Step 6 — Browser join (DONE — now built into the simulator)
The web simulator now joins the LiveKit room automatically. When the backend
reports `livekit_mode: live` and you tap 📞, the browser:
- loads the `livekit-client` SDK,
- connects to the room with the URL + token from the backend,
- publishes your microphone, and
- plays the agent's streamed audio (Bulbul v3) in real time.

The Mute button mutes your real mic; End Call disconnects the room. If the SDK
can't load (offline) or LiveKit isn't configured, it silently falls back to the
mock press-to-talk path — nothing breaks.

### To use it
1. Make sure the **agent worker is running** (Step 5, `dev` mode) — without it,
   you'll connect to an empty room and hear nothing.
2. Restart the backend (`run.bat`) so it reads your LiveKit keys.
3. Hard-refresh the simulator (Ctrl+F5), open a department, tap 📞, and allow
   the mic. You should hear the agent greet you within ~1 second and be able to
   interrupt it (barge-in).

If the call status shows "SDK missing" or "Connection failed", see
Troubleshooting below.

---

## Troubleshooting
- **`/api/health` shows `livekit_mode: mock`** → keys missing/typo in `.env`, or
  backend not restarted.
- **Worker prints "dependencies are NOT installed"** → run step 3 inside the
  activated venv.
- **Worker prints "LiveKit credentials missing"** → step 2 values aren't set in
  the environment the worker sees (run it from the `gov-services-ai` folder so it reads
  the same `.env`).
- **Robotic/slow or wrong language** → tuning knobs are in
  `STREAMING_VOICE_SETUP.md` (`VOICE_LLM_MODEL`, VAD, endpointing, turn
  detector).

## Models in use (already configured)
- STT: `saaras:v3` (auto-detects the spoken language)
- LLM: `sarvam-105b` (override with `VOICE_LLM_MODEL`; streams tokens)
- TTS: `bulbul:v3` (per-department voice)
