# Low-latency streaming voice — the real fix (Sarvam + LiveKit)

This is the plan Sarvam recommended, grounded in their docs
([Collection Agent](https://docs.sarvam.ai/api-reference-docs/cookbook/example-voice-agents/collection-agent),
[Government Scheme Agent](https://docs.sarvam.ai/api-reference-docs/cookbook/example-voice-agents/government-scheme-agent))
and the [LiveKit Sarvam LLM plugin](https://docs.livekit.io/agents/models/llm/sarvam/).

## Why voice feels slow today

The app is running the **mock "press-to-talk" path**: record the whole clip →
REST Saaras (full) → LLM (full reply) → REST Bulbul (full) → play one audio file.
Every stage waits for the previous one to *finish*. Nothing streams. That is the
delay — not the models.

There is already a real-time worker in the repo
(`backend/livekit_agent_worker.py`), but two things mean it isn't actually used:

1. `livekit-agents` isn't installed and LiveKit credentials aren't set, so the
   call falls back to mock mode.
2. **The browser never joins the LiveKit room.** `startCall()` in
   `simulator/index.html` mints a token but then just shows "token issued" and
   keeps using the mock VAD. So even with the worker running, no audio reaches
   it.

## The target architecture (streaming, low-latency)

```
caller mic ─WebRTC─► LiveKit room ◄─ joins ─► agent worker (this repo)
                                              AgentSession pipeline:
                                                Silero VAD + turn detector
                                                Saaras v3  STT   (streaming)
                                                Sarvam-105B LLM  (token stream)
                                                Bulbul v3  TTS   (streaming)
                                              barge-in enabled
```

The streaming is **inherent to the LiveKit `AgentSession`** — partial transcripts,
LLM tokens, and TTS audio all flow continuously, and the agent starts speaking
the first words while it's still thinking. This is exactly the stack you asked
for: **Saaras v3 → Sarvam-105B → Bulbul v3**, with streaming in between.

What's already done in code:
- `livekit_agent_worker.py` uses `sarvam.STT(model="saaras:v3")`,
  `sarvam.LLM(...)`, `sarvam.TTS(model="bulbul:v3")` inside an `AgentSession`
  with barge-in and tuned endpointing.
- The voice "thinking" model now defaults to **`sarvam-105b`** (override with
  `VOICE_LLM_MODEL`; e.g. `sarvam-105b-32k` for long context, or `sarvam-30b`
  for maximum speed). `sarvam.LLM` streams tokens, so 105B's latency is masked.

## Steps to turn it on

### 1. Credentials (free)
Create a LiveKit Cloud project → add to `.env`:
```
LIVEKIT_URL=wss://YOUR-PROJECT.livekit.cloud
LIVEKIT_API_KEY=APIxxxxx
LIVEKIT_API_SECRET=xxxxx
SARVAM_API_KEY=sk_xxxxx        # already set
```

### 2. Install the voice worker deps (on the worker machine)
```
pip install -r requirements-voice.txt
```

### 3. Run the worker (separate terminal, alongside the backend)
```
python -m backend.livekit_agent_worker dev
```
Quick local check without a browser:
```
python -m backend.livekit_agent_worker console
```
This talks to you in the terminal using the full streaming pipeline — the
fastest way to confirm latency before wiring the browser.

### 4. Frontend WebRTC join — the remaining work (not yet built)
The browser must actually connect to the room. Concretely, in the call overlay:
- Load the LiveKit client SDK (CDN): `livekit-client`.
- On `startCall()` when `mode === 'livekit'`:
  - `const room = new LivekitClient.Room();`
  - `await room.connect(livekitUrl, token);`
  - publish the mic: `await room.localParticipant.setMicrophoneEnabled(true);`
  - on `RoomEvent.TrackSubscribed`, attach the agent's audio track to an
    `<audio autoplay>` element (this is the agent's streamed voice).
  - optionally render live captions from the room's text/transcription stream.
  - on hang-up: `room.disconnect()`.
- Skip the mock VAD (`_startCallVAD`) entirely in LiveKit mode.

This is ~60–100 lines in `simulator/index.html`, isolated to the call overlay,
and leaves the mock path untouched as a fallback. It must be tested against a
real LiveKit project (it can't be exercised in an offline sandbox), so it's best
done as its own focused change once the credentials above are in place.

## Tuning knobs (after it's live)
- `VOICE_LLM_MODEL` — `sarvam-105b` (default) / `sarvam-105b-32k` / `sarvam-30b`.
- Worker VAD: `silero.VAD.load(min_silence_duration=…, activation_threshold=…)`.
- Endpointing: `AgentSession(min_endpointing_delay=…, max_endpointing_delay=…)`.
- Add `livekit-plugins-turn-detector` (a MultilingualModel turn detector) for
  faster, more accurate end-of-turn than raw VAD — fewer awkward pauses and
  fewer false cut-offs.
- Keep replies short (already enforced) — fewer tokens = faster first audio.

## Important
Do **not** remove the mock press-to-talk path — it's the fallback when LiveKit
isn't configured, and it's what works today. The streaming pipeline is additive.

## Suggested next step
With LiveKit credentials in `.env`, I can implement step 4 (the frontend WebRTC
join) as a self-contained change and we can verify latency end-to-end.
