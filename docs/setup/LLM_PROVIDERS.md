# Multi-LLM Provider Setup

Phase 4b lets you swap the LLM behind the agents without touching code. Default is **Sarvam-30B** (sovereign, Indian-hosted). Drop in keys for OpenAI, Claude, Gemini, Groq, Together, Mistral, or run a local Ollama server — and the same simulator + Twilio + LiveKit stack uses your chosen LLM.

## Why this exists

Even though the v2 architecture document mandates Sarvam-only for production sovereignty, you may want to:

- **Benchmark** Sarvam-30B against frontier models (GPT-4o, Claude 3.5 Sonnet, Gemini 2.0) on your Tamil/Hindi government queries to validate "good enough"
- **Develop offline** using a local Ollama model when you don't have internet or want zero API cost
- **Compare answer quality** by switching providers mid-conversation
- **Build dev/staging environments** that don't burn Sarvam tokens

The LLM swap covers chat replies and intent routing. **Voice (Saaras) and Vision (Sarvam Vision) stay on Sarvam** — those are specialised speech/document models, not interchangeable.

## Sovereignty notes

The system badges providers as either 🇮🇳 sovereign or ⚠️ overseas:

| Provider | Sovereign? | Notes |
|---|---|---|
| Sarvam | 🇮🇳 yes | Indian-hosted, default |
| Ollama (local) | 🇮🇳 yes | Runs on your hardware; never leaves your machine |
| OpenAI / Groq / Together / Mistral / Anthropic / Gemini | ⚠️ no | All US-hosted; citizen data crosses borders |

The simulator status pill and the `/api/health` endpoint surface this so you never accidentally ship a production change to a non-sovereign provider.

## Switching providers

### Method 1 — env var + restart

Edit `phase4b/.env`:

```env
LLM_PROVIDER=openai      # or anthropic, gemini, groq, ollama, etc.
OPENAI_API_KEY=sk-xxxxx
OPENAI_MODEL=gpt-4o-mini
```

Then `run.bat` (or `./run.sh`). The startup banner confirms:

```
LLM provider: OpenAI gpt-4o-mini  [⚠️  overseas]
LLM base URL: https://api.openai.com/v1
LLM mock:     False
```

### Method 2 — runtime switch (no restart)

```bash
curl -X POST http://127.0.0.1:8000/api/v1/llm/switch \
  -H 'Content-Type: application/json' \
  -d '{"provider":"anthropic"}'
```

The next message routes through the new provider. Useful for A/B testing or quickly demonstrating provider comparisons in the same simulator.

### Method 3 — inspect the registry

```bash
# List all providers + which are mock vs live based on current env
curl http://127.0.0.1:8000/api/v1/llm/providers | python -m json.tool

# Show only the active one
curl http://127.0.0.1:8000/api/v1/llm/info
```

## Provider-specific setup

### Sarvam (default, sovereign)

```env
LLM_PROVIDER=sarvam
SARVAM_API_KEY=sk-xxxxx     # from https://dashboard.sarvam.ai
SARVAM_CHAT_MODEL=sarvam-30b
```

Models: `sarvam-30b` (recommended for voice/realtime), `sarvam-30b-16k`, `sarvam-105b`, `sarvam-105b-32k`.

### OpenAI

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-xxxxx     # from https://platform.openai.com/api-keys
OPENAI_MODEL=gpt-4o-mini    # or gpt-4o, gpt-4-turbo
```

### Anthropic Claude

```env
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-xxxxx     # from https://console.anthropic.com
ANTHROPIC_MODEL=claude-3-5-sonnet-20241022   # or claude-3-5-haiku, claude-3-opus
```

Note: Anthropic puts the system instruction in a separate `system` field. The provider handles this transparently — your code passes OpenAI-style messages.

### Google Gemini

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIxxxxx      # from https://aistudio.google.com/apikey
GEMINI_MODEL=gemini-2.0-flash-exp
```

Note: Gemini uses `role: "model"` instead of `"assistant"` and wraps content in `parts`. The provider adapts on your behalf.

### Groq (super-fast OpenAI-compatible)

```env
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_xxxxx      # from https://console.groq.com/keys
GROQ_MODEL=llama-3.3-70b-versatile    # or mixtral-8x7b, gemma-7b-it
```

Groq is OpenAI-compatible (same request shape) but typically 5-10× faster on Llama-class models. Useful for low-latency demos.

### Together AI

```env
LLM_PROVIDER=together
TOGETHER_API_KEY=xxxxx      # from https://api.together.xyz/settings/api-keys
TOGETHER_MODEL=meta-llama/Llama-3.3-70B-Instruct-Turbo
```

Many open-weight models hosted; OpenAI-compatible.

### Mistral La Plateforme

```env
LLM_PROVIDER=mistral
MISTRAL_API_KEY=xxxxx       # from https://console.mistral.ai
MISTRAL_MODEL=mistral-large-latest
```

### Ollama (local, fully sovereign)

If you have [Ollama](https://ollama.ai) running locally:

```bash
# Install + pull a model
ollama pull llama3.2
ollama serve     # default port 11434
```

Then:

```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=llama3.2
```

No API key needed. Marked sovereign because nothing leaves your machine. Quality varies by model — smaller local models won't match Sarvam-30B on Indian-language tasks but are fine for English dev work.

### Mock (always available)

```env
LLM_PROVIDER=mock
```

Forces canned responses from `backend/agents.py::mock_responses`. Useful when you want the UI to work without any LLM at all — for screen-recording demos, network-isolated environments, CI tests.

## What "mock fallback" means

If you set `LLM_PROVIDER=openai` but leave `OPENAI_API_KEY` blank, the system automatically falls back to a `MockProvider` tagged as `mock(openai)`. The status pill shows `MOCK fallback (openai)` and a warning is logged. This means:

- The UI always works — no crashes from a missing key
- You see which provider was *intended* but couldn't be initialized
- Switching back to a configured provider works without restart

## How the abstraction works internally

```
                     ┌─────────────────────┐
                     │   Orchestrator      │
                     │   + Intent Router   │
                     └──────────┬──────────┘
                                │ calls llm.chat_stream / chat_complete
                                ▼
                  ┌─────────────────────────┐
                  │      LLMProvider        │  ← abstract base
                  │   (chat_stream, info)   │
                  └─────────────┬───────────┘
                                │
            ┌───────────────────┼───────────────────────┐
            │                   │                       │
   ┌────────▼──────┐   ┌────────▼──────────┐   ┌────────▼─────────┐
   │SarvamProvider │   │OpenAICompatBase   │   │AnthropicProvider │
   │(sovereign)    │   │(OpenAI/Groq/      │   │(claude)          │
   │               │   │ Together/Mistral/ │   │                  │
   │               │   │ Ollama)           │   │                  │
   └───────────────┘   └───────────────────┘   └──────────────────┘
                                │
                       ┌────────▼──────────┐
                       │GeminiProvider     │
                       │(google.com format)│
                       └───────────────────┘
                                │
                       ┌────────▼──────────┐
                       │MockProvider       │ ← auto-substituted
                       │(when key missing) │   by factory
                       └───────────────────┘
```

Adding a new provider is one file under `backend/llm/` implementing `chat_stream`, `chat_complete`, `mock_mode`, and `info`. Register it in `backend/llm/__init__.py::_PROVIDER_REGISTRY`. Done.

## What is NOT pluggable

- **Saaras V3 STT** — Indian-language ASR is Sarvam-specific. Whisper would lose Indian-language quality.
- **Bulbul V3 TTS** — same. Indian voices, including the per-agent ones (manisha, vidya, arjun, anjali, shubh).
- **Sarvam Vision** — Indian document OCR. Cloud Vision / Document AI / GPT-4-Vision are not optimised for Tamil/Hindi documents.
- **DigiLocker, Aadhaar, API Setu, UPI** — Indian government infrastructure, no alternatives.

These stay on Sarvam regardless of which LLM you pick. If you switch `LLM_PROVIDER=openai`, the chat LLM is GPT but voice notes are still Saaras (or its mock) and document OCR is still Sarvam Vision (or its mock).

## Troubleshooting

**Switch endpoint returns 400 "unknown provider"** — Check spelling and that the provider is in `list_providers()` output.

**Provider switched but messages still feel like the old one** — The `MockProvider` is pretty uniform across tags (it picks from the same agent canned-response pool). To see real differences, you need to plug in actual API keys.

**Anthropic returns 400 about consecutive assistant messages** — Phase 4b's `AnthropicProvider._adapt_messages` collapses consecutive same-role messages automatically. If you still hit this, share the exact `messages` array that failed.

**Gemini returns 400 about role: assistant** — Same fix; `GeminiProvider._adapt_messages` maps `assistant` → `model`. Should be transparent.

**Ollama returns 404 model not found** — `ollama pull llama3.2` (or your model of choice) first. Run `ollama list` to see what's installed.

**My provider isn't in the registry** — Three ways to add it:

1. **OpenAI-compatible service** (e.g., Fireworks, OpenRouter, Cerebras, Perplexity): subclass `_OpenAICompatBase` in `openai_compat.py`, override the four `_read_*` methods. Total: ~15 lines.

2. **Native API** (different request format, e.g., Cohere, Bedrock): copy `anthropic.py` as a template, adapt to the new API's request/response shape.

3. **Self-hosted (vLLM, LM Studio, TGI)**: Most expose an OpenAI-compatible endpoint. Use `OllamaProvider` as the template, change the default base URL.
