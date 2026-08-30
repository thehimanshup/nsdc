# LiveKit Voice Calls — Setup Guide

This guide takes you from zero to "I tapped the call button and had a real-time voice conversation with a Sarvam-powered government agent." Time required: about 15 minutes.

Phase 4 has two voice-call modes:

- **MOCK call** (default — works today, no setup) — A press-to-talk experience. Looks like a call screen, but each turn is a voice note sent over the existing Saaras → Sarvam-30B → Bulbul pipeline.
- **LIVE call** (requires LiveKit) — Real WebRTC, full-duplex, with VAD-driven barge-in. Same Sarvam pipeline running continuously inside a LiveKit agent worker.

You can use the simulator with the MOCK call first to validate the UX, then upgrade to LIVE when you want real-time interruptibility.

## Option A — LiveKit Cloud (fastest)

LiveKit offers a free Cloud tier — 100 concurrent participants and a few thousand minutes of audio per month. Plenty for development.

### 1. Create a LiveKit Cloud project

Sign up at <https://cloud.livekit.io>. After signup, you'll land on the project dashboard. Copy:

- **WebSocket URL** — something like `wss://your-project-xyz.livekit.cloud`
- **API Key** — starts with `API…`
- **API Secret** — long random string (click "Reveal")

Paste them into `phase4/.env`:

```env
LIVEKIT_URL=wss://your-project-xyz.livekit.cloud
LIVEKIT_API_KEY=APIxxxxxxxxxxxxxxxxx
LIVEKIT_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### 2. Install LiveKit + Sarvam plugin dependencies

The LiveKit agent worker has its own Python deps separate from the main backend. Install them in your existing venv:

```bash
# Activate your virtualenv first (the same one run.bat / run.sh uses)
pip install "livekit-agents[sarvam]~=1.5" livekit-plugins-silero
```

### 3. Start the main backend (terminal 1)

```bash
run.bat            # Windows
./run.sh           # macOS / Linux
```

The startup banner should now read `LiveKit mode: LIVE`.

### 4. Start the LiveKit agent worker (terminal 2)

In a fresh terminal, with the same venv active:

```bash
cd phase4
python -m backend.livekit_agent_worker dev
```

You should see:

```
[livekit-worker] starting LiveKit agent worker
[livekit-worker] watching for new rooms...
```

The worker is now waiting for citizens to open call sessions.

### 5. Make a call from the simulator

Open <http://127.0.0.1:8000/>. Add a phone, open any agent (Health, Revenue, etc.), and click the green **📞 Call** button in the chat header.

You'll see the call screen. Within 2-3 seconds the agent will answer with a greeting in English. Speak naturally — the agent transcribes via Saaras V3, reasons via Sarvam-30B, and replies via Bulbul V3 with the per-agent voice (Revenue=arjun, Health=vidya, etc.). Talk over the agent and Silero VAD will detect the barge-in.

When you tap the red end-call button, the transcript is persisted as a system message in the chat, and the call duration is logged.

## Option B — Self-hosted LiveKit (sovereign-stack)

For production aligned with the v2 architecture document's strict-sovereignty mandate, you'll want LiveKit on Indian-hosted infrastructure (MeghRaj or AWS Mumbai). The setup is well documented at <https://docs.livekit.io/home/self-hosting/deployment/>. Once your self-hosted server is up:

1. Note the WebSocket URL (your domain + path)
2. Generate an API key + secret via the LiveKit CLI: `livekit-cli create-token --create`
3. Put those in `.env` exactly as in Option A
4. The agent worker and main backend talk to your self-hosted instance — no other code changes needed

## Option C — Skip LiveKit entirely (default)

If you don't set the LiveKit env vars, the simulator's call button still works — it uses press-to-talk mode. The UX:

1. Tap **📞 Call**. A call screen appears with the agent avatar and a big mic button.
2. Press-and-hold the mic — speak — release. Your audio uploads. Saaras transcribes. Sarvam-30B replies. Bulbul speaks the reply. Audio auto-plays.
3. Repeat for each turn.
4. Tap red end-call to finish. Transcript is persisted.

It's the same Sarvam pipeline, just turn-based instead of streaming. Useful for testing voice quality without LiveKit setup.

## Troubleshooting

**Worker says "import error: livekit-agents not installed"** — Run `pip install "livekit-agents[sarvam]~=1.5" livekit-plugins-silero` in the same venv the main backend uses.

**Worker connects but the call doesn't ring through** — Make sure the `LIVEKIT_URL` in `.env` matches the URL in the LiveKit Cloud dashboard exactly, including the `wss://` prefix. The worker and the browser must connect to the same project.

**Worker connects, but no audio either way** — Browser permission denied for microphone. Most modern browsers gate `getUserMedia` to `localhost` or HTTPS contexts. `127.0.0.1:8000` qualifies; refresh and accept the mic permission prompt.

**Agent speaks but I can't hear it** — Browser audio output device may be muted. Try a different speaker/headset. Also verify the LiveKit Cloud dashboard shows audio being published by the worker (Project → Sessions → Audio tracks).

**Agent reply is in English, but I spoke Tamil** — Saaras detected the wrong language. Set `language_code` explicitly on `sarvam.STT(...)` in `backend/livekit_agent_worker.py` if you want to lock to a specific language. For free-form conversations, the agent will eventually pick it up.

**I want the live call but I'm in MOCK Sarvam mode** — That works fine. The LiveKit + sarvam plugin will fall back to internal placeholders for STT/LLM/TTS but the WebRTC pipeline is real. Of course the conversation will be incoherent until you plug in a real `SARVAM_API_KEY`.

## What changes in production

For going to production:

- Move from LiveKit Cloud to self-hosted LiveKit in Indian datacentres (sovereignty mandate)
- Run multiple `livekit_agent_worker` processes behind a supervisor (one process can handle many concurrent calls but you want horizontal scaling for fault tolerance)
- Add the SIP bridge to terminate Twilio Voice (PSTN) calls into the same LiveKit rooms — see the v2 architecture doc §10.3 Path A
- Wire the Sarvam dedicated tenancy for guaranteed P95 latency
