# Sarvam Diagnostics — Why am I hearing a beep?

If voice previews in the admin console play a "beep, beep" sound instead of a real voice, **you are in mock mode**. The two-tone chime is a programmatically-generated sine wave (660Hz + 880Hz, ~2.5s), not a Bulbul voice — it's deliberately distinctive so you know the API isn't being called.

This guide gets you from "beep" to a real Sarvam Bulbul voice in under 5 minutes.

## Quick fix (most common cause)

You don't have a Sarvam API key set. Three steps:

1. Visit <https://dashboard.sarvam.ai> and create a free account.
2. Generate an API key and copy it.
3. Edit `phase5b/.env`:

   ```env
   SARVAM_API_KEY=sk_xxxxxxxxxxxxxxxxxxxxxxxxx
   ```

4. Stop the server (`Ctrl+C`) and restart (`run.bat`).
5. Open <http://127.0.0.1:8000/admin/> → **🔬 Sarvam** tab → click **Run full diagnose**.

If all checks go green, the voice preview will play real Bulbul. If anything's red, the diagnostic tells you exactly why.

## Verifying it worked

The startup banner is the fastest way to see if your key is being read. After restart, look at the terminal output. You should see:

```
  Sarvam API key:    ✓ present
  Sarvam chat:       sarvam-30b  (LIVE)
  Sarvam STT/TTS:    saaras:v3 / bulbul:v3  (LIVE)
  Sarvam Vision:     LIVE
```

If you see `✗ MISSING`, the `.env` file isn't being read. Check:
- Is the file named exactly `.env` (with the leading dot)?
- Is it in `phase5b/` (same folder as `run.bat`)?
- Is the line `SARVAM_API_KEY=...` (no spaces around `=`)?

## Running diagnostics from the command line

For automation or quick checks without opening the browser:

```bash
cd phase5b
python -m backend.sarvam_diagnostics
```

This runs the full sweep — chat, STT, TTS, translate, vision — and prints a coloured report. Exit code is 0 if all green, 1 otherwise. Useful in CI or pre-deployment smoke tests.

Sample output when the key is set correctly:

```
======================================================================
  SARVAM DIAGNOSTICS
======================================================================
  Overall:    GREEN
  Key set:    True
  Base URL:   https://api.sarvam.ai/v1
  Chat model: sarvam-30b
  Result:     7 passed / 0 failed / 0 skipped
======================================================================

  ✓ api_key_present
     Key present: sk_xxx…xxxx (length=48)
  ✓ base_url
     https://api.sarvam.ai/v1
  ✓ chat (Sarvam-30B)
     HTTP:    200
     Latency: 824 ms
     Reply received in 824ms: ok
  ✓ STT (Saaras v3)
     HTTP:    200
     Latency: 1245 ms
  ✓ TTS (Bulbul v3 / voice=shubh)
     HTTP:    200
     Latency: 1672 ms
     TTS produced 88200 bytes in 1672ms — WAV detected
  ✓ translate (Sarvam-Translate)
     HTTP:    200
     Latency: 612 ms
     Translated in 612ms: வணக்கம், இது ஒரு மொழிபெயர்ப்பு சோதனை.
  ✓ vision (Sarvam Vision)
     HTTP:    200
     Latency: 1031 ms
```

## CERTIFICATE_VERIFY_FAILED — the most common Windows pain

```
✗ chat (Sarvam-30B) · network
   ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
   unable to get local issuer certificate (_ssl.c:1081)
```

Almost always means **you're on a corporate network that does TLS inspection**. Your IT department installs a custom Certificate Authority on every machine; the OS trust store has it, but Python's default trust list (`certifi`) doesn't.

### The fix (works in 95% of cases)

```bash
pip install truststore
```

That's it. Restart the server. The included `backend/http_client.py` detects `truststore` automatically and uses it for all Sarvam calls. truststore reads your OS cert store directly (Windows certmgr, macOS Keychain, Linux ca-certificates), so corporate CAs are trusted without manual configuration.

Verify it kicked in: open the admin **🔬 Sarvam** tab → status panel should show `SSL strategy: truststore`.

### If that didn't work — provide the corporate CA bundle explicitly

Ask your IT team for the corporate root CA in PEM format. Save it somewhere, e.g. `C:\certs\corp-ca.pem`. Add to `.env`:

```env
SARVAM_CA_BUNDLE=C:\certs\corp-ca.pem
```

Restart. The diagnostic will use that bundle. Strategy shows `ca_bundle`.

### Emergency override (NOT for production)

If you absolutely need to push through to verify everything else works:

```env
SARVAM_VERIFY_SSL=false
```

Disables SSL verification on Sarvam calls. The server logs a loud warning every time it's used. Strategy shows `verify_off`. Remove this for any non-local use.

### Other possible causes

- **Outdated Python install** — Python 3.10+ recommended. Older versions ship outdated `certifi`.
- **WSL / dual-environment confusion** — make sure the Python that runs `run.bat` is the one where you installed truststore.
- **Antivirus doing SSL inspection** — some endpoint security products inject their own CA. Same fix as corporate proxy.

---

## Common failure modes (other than SSL)

### HTTP 401 / 403 — authentication

```
✗ chat (Sarvam-30B)
   HTTP:    401
   Kind:    auth
   HTTP 401: {"error": "Invalid API subscription key"}
```

Your key is wrong or revoked. Regenerate at <https://dashboard.sarvam.ai>.

### HTTP 429 — rate limited

```
✗ chat (Sarvam-30B)
   HTTP:    429
   Kind:    rate_limit
```

You've hit the per-minute or per-day quota. Wait, or upgrade your Sarvam plan. Free tiers have low limits.

### `content` is `None` (chat-specific gotcha)

```
✗ chat (Sarvam-30B)
   HTTP:    200
   Kind:    shape
   Sarvam returned a 2xx but the message content was empty.
   Likely cause: max_tokens too low...
```

Sarvam's 30B/105B models do internal reasoning before emitting `content`. If `max_tokens` is < ~500, all the budget is spent on reasoning and `content` ends up null. The diagnostic uses 500. If you hit this in your own code, raise `max_tokens`.

### Network errors

```
✗ chat (Sarvam-30B)
   Kind:    network
   ConnectError: [Errno -2] Name or service not known
```

Common causes:
- No internet at all
- Corporate firewall blocking `api.sarvam.ai`
- Behind a proxy without `HTTPS_PROXY` set
- Wrong `SARVAM_BASE_URL` (you accidentally changed it to something invalid)

### Vision returns 404

The `/vision/extract` endpoint path may have changed since this code was written. Check <https://docs.sarvam.ai/api-reference-docs> for the current path and update `backend/vision.py` accordingly.

## What "MOCK" actually means in this codebase

Each Sarvam capability has a mock fallback so the UI keeps working without an API key. The mock outputs are deliberately distinguishable:

| Capability | LIVE behaviour | MOCK behaviour |
|---|---|---|
| Chat | Real LLM reply, RAG-grounded | Per-agent canned response, streamed word-by-word |
| STT (voice notes) | Real Saaras transcription | Random Tamil/Hindi sample sentence |
| TTS (voice replies) | Real Bulbul voice, per-agent (vidya, arjun, manisha, ...) | **Two-tone sine chime, 660Hz → 880Hz, ~2.5s** ← *this is the beep* |
| Vision OCR | Real Sarvam Vision extraction | Fixture from `data/vision_fixtures.json` keyed by filename |
| Translate | Real Sarvam-Translate | Wraps text with `[lang]` prefix |

The voice preview button in the admin now explicitly tells you which mode each preview ran in — a yellow banner appears for MOCK, green for LIVE.

## API endpoints exposed by the diagnostics

| Endpoint | Purpose |
|---|---|
| `GET  /api/v1/admin/sarvam/status` | Quick pre-flight check, no API calls |
| `GET  /api/v1/admin/sarvam/diagnose` | Full sweep (10-30s in LIVE mode) |
| `POST /api/v1/admin/sarvam/test-chat` | Single chat test |
| `POST /api/v1/admin/sarvam/test-stt` | Single STT test |
| `POST /api/v1/admin/sarvam/test-tts` | Single TTS test |
| `POST /api/v1/admin/sarvam/test-translate` | Single translate test |
| `POST /api/v1/admin/sarvam/test-vision` | Single Vision test |

All return structured JSON with `status`, `http_status`, `latency_ms`, `error_kind`, and a human-readable `message`. The admin console's 🔬 Sarvam tab renders this as a coloured per-check report.

## What changed in Phase 5b (vs. earlier phases)

The bug that made the beep diagnose-resistant: in Phase 4 the TTS code path was:

```python
except Exception:
    return await _tts_mock_chime(...)    # silent fallback, no log, no error surface
```

So when Sarvam was unreachable OR returned a 401, you got a beep with no clue why. Phase 5b changes this to:

```python
if r.status_code >= 400:
    log.error("Sarvam TTS HTTP %d: %s", r.status_code, r.text[:200])
    fallback = await _tts_mock_chime(...)
    fallback.mock = True       # surfaced to the caller
    return fallback
```

The error is now in the server log AND the API response includes `is_mock=true` so the admin UI can show the yellow banner.
