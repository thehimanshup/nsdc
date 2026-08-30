# Reducing voice-call delay — brainstorm + plan

Goal: make the agent feel like a person on the phone — reply starts ~1 second
after the caller stops, with no dead air.

## Where the delay actually comes from

There are two voice paths:

- **Mock "press-to-talk" path** (what the simulator uses when LiveKit is in MOCK
  mode — i.e. right now). One turn runs strictly **serially**:
  `record → upload → Saaras STT (full) → LLM (full reply) → Bulbul TTS (full) → send one audio file → play`.
  Nothing overlaps, and audio only starts after the **entire** reply is
  synthesised.
- **LiveKit live path** (`livekit_agent_worker.py`) is already a real-time
  streaming pipeline (VAD → STT → LLM → TTS with barge-in), so it's much
  snappier. The delay complaint is essentially the mock path.

Typical mock-path turn, before this change:

| Stage | Rough cost |
|---|---|
| Front-end VAD end-of-speech wait | ~0.8 s (already tuned down from 1.2 s) |
| Upload audio | 0.05–0.2 s |
| **TLS handshake for STT** (new connection every call) | 0.15–0.4 s |
| Saaras STT | 0.4–1.0 s |
| LLM (full reply, non-streaming) | 0.8–2.0 s |
| **TLS handshake for TTS** (new connection every call) | 0.15–0.4 s |
| Bulbul TTS (full reply) | 0.5–1.5 s |
| Deliver + start playback | 0.1 s |

So **~2.5–5 s** of dead air, and two of those stages were pure waste
(handshakes we paid on every single turn).

## What was already optimised (earlier work)

- Front-end VAD retuned: silence 1200→800 ms, cooldown 700→350 ms, start-gate
  5→3 frames.
- Empty-stream retry back-off shortened for spoken turns (0.12 s vs 0.4 s).
- Replies forced to 1–2 short sentences (fewer tokens ⇒ faster LLM **and** TTS).
- Anti-repeat / no re-greeting (less audio to speak).
- The **LLM** client was already pooled (keep-alive).

## Shipped now: connection pooling for STT + TTS

STT and TTS each used to open a **brand-new HTTPS connection per call**, paying
a fresh TCP+TLS handshake every turn (and the corporate-CA / truststore
handshake is not cheap). They now share **one pooled keep-alive client**
(`get_sarvam_client()` in `backend/http_client.py`). Because STT and TTS hit the
same host, the TTS call reuses the warm connection from the STT call, and the
connection stays warm across turns.

**Expected saving: ~300–800 ms per turn**, zero quality risk. Verified the
client is reused across calls and recreated cleanly after shutdown.

## Biggest remaining win (recommended next): sentence-chunked streaming TTS

Today the mock path waits for the **whole** reply before it speaks. Instead, as
the LLM streams text, synthesise **each sentence as soon as it finishes** and
send it as its own audio segment; the browser plays segments back-to-back. The
caller then hears the first sentence while the rest is still being generated.

- **Time-to-first-audio** drops from "STT + full LLM + full TTS" to
  "STT + first-sentence LLM + first-sentence TTS" — typically **1.5–2.5 s saved**
  on a 2-sentence reply. This is the single biggest perceived-latency fix.
- Implementation sketch:
  - Backend: in the voice branch of the reply generator, accumulate streamed
    tokens, and at each sentence boundary (`. ! ? ।`) call `tts_synthesize` for
    that sentence and emit a `voice_segment` WS frame (`{seq, audioUrl}`).
  - Front-end: queue segments by `seq` and play them sequentially on a single
    audio channel (the call already has a single-channel player from the
    overlap fix — extend it to a small FIFO queue).
  - Keep the current single-file path as a fallback for non-streaming clients.
- Effort: moderate (one backend function + ~30 lines of front-end queue).

## Other levers (in priority order)

1. **Pre-warm on call start.** When a call begins, fire a tiny throwaway STT/TTS
   (or a `/ping`) so the TLS connection and model are warm before the first real
   turn. Removes the first-turn cold-start spike.
2. **Trim the front-end VAD a little more** *only if* the room is quiet: silence
   800→650 ms, cooldown 350→250 ms. Risky in noisy rooms (false triggers), so
   make it a per-deployment constant.
3. **Smaller / faster model for voice.** Voice replies are short; a smaller
   Sarvam chat model (or a lower token budget) for the spoken channel cuts LLM
   time. CMO can stay on the larger model for complex grievances.
4. **Perceived-latency filler.** Play a very short, instant acknowledgement
   ("mm-hmm", "ek minute") the moment STT finishes, masking the LLM+TTS wait.
   Cheap and surprisingly effective; must be barge-in-safe.
5. **Audio format / size.** Record mono 16 kHz (8 kHz for PSTN) and a compact
   codec so uploads are tiny; keep TTS output at the minimum sample rate that
   still sounds natural.
6. **Co-locate with the Sarvam region.** Round-trip time to the API dominates
   the handshake + per-call latency; hosting the backend in the same region as
   Sarvam shaves fixed network cost off every call.
7. **Prefer the LiveKit path for real calls.** It already streams end-to-end
   with barge-in; the mock path is best treated as a demo fallback.

## Suggested roadmap

1. ✅ Connection pooling (done — safe, immediate).
2. Sentence-chunked streaming TTS (biggest perceived win).
3. Pre-warm on call start + optional filler token.
4. Per-deployment VAD tuning + voice-channel model/token budget.

Want me to implement #2 (streaming TTS) next? It's the change that will make the
mock path feel genuinely real-time.
