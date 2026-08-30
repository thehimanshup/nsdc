# Twilio WhatsApp Setup — Step by Step

This guide takes you from zero to "I sent a real WhatsApp message from my phone and Sarvam-30B replied." Time required: about 20 minutes.

## What you need

- A free [Twilio account](https://www.twilio.com/try-twilio) (no credit card needed for sandbox testing)
- A way to expose your local machine to the public internet — either [ngrok](https://ngrok.com/) (free) or [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (free, no signup if you use `cloudflared` quick tunnels)
- The Phase 3 backend running locally
- Your phone with WhatsApp installed

## Path A — Twilio Sandbox (fastest)

This works without phone-number purchase or Meta Business verification — perfect for testing.

### 1. Get your Twilio credentials

Sign in at <https://console.twilio.com>. From the **Account Info** card at the top of the dashboard, copy:

- **Account SID** (starts with `AC...`)
- **Auth Token** (click "View" to reveal)

Paste them into `phase3/.env`:

```env
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
```

### 2. Join the Twilio WhatsApp Sandbox

In the Twilio Console, navigate to **Messaging → Try it out → Send a WhatsApp message** (or directly to <https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn>).

You'll see a sandbox number `+1 415 523 8886` and a unique join phrase like `join smooth-tiger`. From your phone's WhatsApp:

- Send **`join smooth-tiger`** (use *your* phrase) to **+1 415 523 8886**
- You'll get a confirmation back from Twilio

Your phone is now connected to the sandbox.

### 3. Start an HTTPS tunnel to your machine

Pick one of these — both work. Twilio needs to reach your `localhost:8000` from the internet.

**Option 1 — ngrok** (most popular)

```bash
# Install once: https://ngrok.com/download
ngrok http 8000
```

You'll see something like:

```
Forwarding   https://abc123.ngrok-free.app -> http://localhost:8000
```

Copy that `https://` URL.

**Option 2 — Cloudflare Tunnel quick tunnel** (no signup)

```bash
# Install once: https://github.com/cloudflare/cloudflared/releases
cloudflared tunnel --url http://localhost:8000
```

Same idea — copy the `https://...trycloudflare.com` URL it prints.

### 4. Configure the sandbox webhook

Back in the Twilio Console sandbox page, scroll to **Sandbox settings** at the bottom. Set:

- **When a message comes in**: `https://YOUR-TUNNEL-URL/webhooks/twilio/whatsapp` (use POST)
- **Status callback URL**: `https://YOUR-TUNNEL-URL/webhooks/twilio/status` (optional but recommended)
- **Method**: HTTP POST

Click **Save**.

### 5. Update `.env` with the public URL and start the server

```env
PUBLIC_BASE_URL=https://abc123.ngrok-free.app
```

Save and run:

```bash
run.bat               # Windows
# or
./run.sh              # macOS / Linux
```

You should see the banner including:

```
Twilio mode: LIVE
  SID:         ACxxxxxx…
  From WA:     whatsapp:+14155238886
  Validate sig: True
  Public URL:  https://abc123.ngrok-free.app
```

### 6. Send a real WhatsApp message

From your phone, send any of these to the sandbox number (after the join phrase):

- *Hello*
- *I need help with my Patta application*
- *வணக்கம், எனக்கு பட்டா பற்றி தெரிய வேண்டும்*
- *मेरे राशन कार्ड में नाम कैसे जोड़ें?*
- *There's a leak in my area in T. Nagar*

Within a couple of seconds, Sarvam-30B will reply on WhatsApp. You'll also see logs in your terminal showing the inbound webhook hit and the outbound API call.

### 7. Try the DigiLocker consent flow on WhatsApp

Send: *"I need my Patta document"*

The bot will reply asking for permission:

> 🔐 Permission required
>
> The Revenue Department would like to fetch your Patta from DigiLocker to help you with your query.
>
> Reply YES to allow, or NO to deny. (This request expires in 5 minutes.)

Reply with **YES**. The bot will fetch the mock Patta and reply with the document details, formatted as plain WhatsApp text (no rich UI on this channel — the simulator gets the prettier modal).

## Path B — Local testing without Twilio

If you don't want to set up Twilio yet, you can exercise the entire Twilio code path locally. Phase 3 includes a `/api/v1/test/twilio-inbound` endpoint that simulates a Twilio webhook hit.

### Test it with curl

```bash
curl -X POST http://127.0.0.1:8000/api/v1/test/twilio-inbound \
  -H 'Content-Type: application/json' \
  -d '{
    "from_msisdn": "9876543210",
    "body": "I need my Patta document",
    "agent_id": "revenue"
  }'
```

Response:

```json
{
  "ok": true,
  "citizenId": "ctz_...",
  "msisdn": "9876543210",
  "routedTo": "revenue",
  "serverMsgId": "msg_...",
  "convId": "ctz_...:revenue",
  "note": "Watch the server log..."
}
```

Watch the server log — you'll see `[TWILIO MOCK] WhatsApp ⇒ whatsapp:+919876543210 | body=...` showing what *would* have been sent. The simulator UI for that citizen (if open with the same 10-digit number) also receives the streamed reply via WebSocket.

### Trigger the consent flow

```bash
curl -X POST http://127.0.0.1:8000/api/v1/test/twilio-inbound \
  -H 'Content-Type: application/json' \
  -d '{"from_msisdn": "9876543210", "body": "fetch my patta", "agent_id": "revenue"}'

# Server log shows the consent prompt would be sent. Then "reply YES":

curl -X POST http://127.0.0.1:8000/api/v1/test/twilio-inbound \
  -H 'Content-Type: application/json' \
  -d '{"from_msisdn": "9876543210", "body": "YES"}'
```

The orchestrator will resume the parked turn, execute the DigiLocker mock, and the bot will respond with the Patta data.

## Troubleshooting

**Twilio replies "We've encountered an internal error".** Check your tunnel is still up and the webhook URL in the Twilio console matches it exactly. ngrok URLs change every time you restart unless you have a paid plan.

**The webhook hits but returns 403.** Signature validation is failing. Check that `TWILIO_AUTH_TOKEN` in `.env` matches the token shown in your Twilio Console. If you're behind a proxy that rewrites the URL, the URL Twilio signed and the URL we reconstruct may differ. As a quick test, set `TWILIO_VALIDATE_SIGNATURES=false` and reload — if messages start flowing, this is the cause.

**I sent a message but got no reply on WhatsApp.** Check the backend logs. Two possibilities: (1) the webhook never arrived — check tunnel and webhook URL; (2) the orchestrator processed it but the outbound failed — look for a Twilio API error in the logs (usually a 21408 "permission denied" if you forgot the sandbox join phrase, or 21610 "destination outside opt-in 24h window").

**Voice notes from WhatsApp don't play back.** Twilio downloads the audio as Ogg/Opus. Saaras V3 may not accept that format in LIVE mode without transcoding. For Phase 3 we pass it through unchanged; if you hit format errors, set `SARVAM_API_KEY=` (empty) to drop into mock mode and verify the rest of the pipeline works, then we'll add ffmpeg transcoding in a small fix.

**Inbound works but the reply is the wrong language.** The intent router uses Sarvam-30B for language detection in LIVE mode; mock mode uses naive script detection (Tamil/Hindi/Telugu scripts recognised, Latin-script Indian languages default to English). Type something with at least a few characters in the target language's native script for best results.

## What changes when you go to production

When you're ready to leave the sandbox and use a real WhatsApp Business number:

1. Buy a Twilio phone number (or migrate one in) and enable WhatsApp on it via the **Self-Signup** flow in the Twilio console.
2. Get your Meta Business Manager account verified — this unlocks the 20-number limit and template approvals.
3. Submit your message templates (the ones in `backend/templates.py`) for Meta approval, one per language. Use Twilio Content Template Builder.
4. Update `TWILIO_WHATSAPP_FROM` in `.env` to your production sender.
5. Move the webhook URL from a tunnel to a real domain pointing at your production deployment.

The rest of the code path is identical to sandbox — no code changes required.
