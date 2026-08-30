"""Centralised configuration. Reads from environment with sane defaults.

Automatically loads .env from the project root if present.
"""
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # ---- Deployment profile ----
    # development | staging | production. Production never silently runs mock
    # providers, demo loops, seed resets, or unauthenticated admin APIs.
    app_env: str = os.getenv("APP_ENV", os.getenv("ENV", "development")).lower()
    production_mode: bool = _env_bool("PRODUCTION_MODE", False)

    # ---- LLM Provider (Phase 4b) ----
    # Which provider serves agent chat + intent routing.
    # Options: sarvam | openai | groq | together | mistral | anthropic | gemini | ollama | mock
    # Default: sarvam (sovereign).
    llm_provider: str = os.getenv("LLM_PROVIDER", "sarvam").lower()

    # ---- Sarvam ----
    sarvam_api_key: str = os.getenv("SARVAM_API_KEY", "")
    # Sarvam's API root. Note that paths under this root are INCONSISTENT:
    #   - Chat:        /v1/chat/completions   (has /v1)
    #   - TTS:         /text-to-speech         (no /v1)
    #   - STT:         /speech-to-text         (no /v1)
    #   - Translate:   /translate              (no /v1)
    #   - Doc Intel:   /doc-digitization/job/v1 (different scheme)
    # Each call site appends its own path; this base does NOT include /v1.
    sarvam_base_url: str = os.getenv("SARVAM_BASE_URL", "https://api.sarvam.ai")
    sarvam_chat_model: str = os.getenv("SARVAM_CHAT_MODEL", "sarvam-30b")
    sarvam_router_model: str = os.getenv("SARVAM_ROUTER_MODEL", "sarvam-30b")

    # Phase 6g — data.gov.in API key for the Agmarknet live mandi-price tool.
    # Get a free key at https://data.gov.in (My Account → API key). Without it
    # the agriculture agent falls back to the MSP table.
    data_gov_api_key: str = os.getenv("DATA_GOV_IN_API_KEY", "")

    # SSL / TLS knobs for Sarvam calls.
    # - sarvam_verify_ssl=true (default) → verify chain via OS / certifi
    # - sarvam_ca_bundle=/path/to.pem    → use a specific corporate CA bundle
    # - sarvam_verify_ssl=false          → DEV-ONLY: skip verification
    sarvam_verify_ssl: bool = os.getenv("SARVAM_VERIFY_SSL", "true").lower() != "false"
    sarvam_ca_bundle: str = os.getenv("SARVAM_CA_BUNDLE", "")

    # ---- OpenAI ----
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # ---- Groq ----
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_base_url: str = os.getenv("GROQ_BASE_URL", "")
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    # ---- Together AI ----
    together_api_key: str = os.getenv("TOGETHER_API_KEY", "")
    together_base_url: str = os.getenv("TOGETHER_BASE_URL", "")
    together_model: str = os.getenv("TOGETHER_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo")

    # ---- Mistral ----
    mistral_api_key: str = os.getenv("MISTRAL_API_KEY", "")
    mistral_base_url: str = os.getenv("MISTRAL_BASE_URL", "")
    mistral_model: str = os.getenv("MISTRAL_MODEL", "mistral-large-latest")

    # ---- Anthropic Claude ----
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")

    # ---- Google Gemini ----
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-exp")

    # ---- Ollama (local, no key needed) ----
    ollama_api_key: str = os.getenv("OLLAMA_API_KEY", "")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.2")

    # ---- Twilio ----
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_whatsapp_from: str = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    twilio_voice_from: str = os.getenv("TWILIO_VOICE_FROM", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "")
    twilio_validate_signatures: bool = os.getenv("TWILIO_VALIDATE_SIGNATURES", "true").lower() == "true"

    # ---- LiveKit ----
    livekit_url: str = os.getenv("LIVEKIT_URL", "")
    livekit_api_key: str = os.getenv("LIVEKIT_API_KEY", "")
    livekit_api_secret: str = os.getenv("LIVEKIT_API_SECRET", "")

    # ---- Server ----
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))

    # ---- Behaviour ----
    enable_streaming: bool = os.getenv("ENABLE_STREAMING", "true").lower() == "true"
    enable_intent_router: bool = os.getenv("ENABLE_INTENT_ROUTER", "true").lower() == "true"
    push_demo_enabled: bool = _env_bool("PUSH_DEMO_ENABLED", False)
    allow_mock_providers: bool = _env_bool("ALLOW_MOCK_PROVIDERS", app_env not in {"prod", "production"})
    allow_demo_routes: bool = _env_bool("ALLOW_DEMO_ROUTES", app_env not in {"prod", "production"})
    auto_seed_corpora: bool = _env_bool("AUTO_SEED_CORPORA", app_env not in {"prod", "production"})
    require_live_integrations: bool = _env_bool("REQUIRE_LIVE_INTEGRATIONS", app_env in {"prod", "production"})

    # ---- Storage ----
    data_dir: str = os.getenv("DATA_DIR", "./data")

    # ---- Phase 6f — orchestration engine ----
    # "legacy" (default) = the hand-rolled orchestrator turn loop.
    # "graph"            = the LangGraph/LangChain StateGraph (backend/graph).
    # Single-agent turns route through the chosen engine; coordinator
    # (cross-agent) flows + consent-resume stay on legacy in 6f-2.
    orchestrator_engine: str = os.getenv("ORCHESTRATOR_ENGINE", "legacy").lower()

    # ---- Phase 7 — Skills ----
    # Master switch for attachable skill bundles (tools + instructions + corpus).
    # When false, skills_for_agent() returns [] and the graph behaves as before.
    skills_enabled: bool = _env_bool("SKILLS_ENABLED", True)

    # ---- Phase 7 — Topical-scope guardrail ----
    # Block off-topic asks (jokes, puzzles, code, roleplay, …) before they reach
    # the agent LLM, returning a warm in-language refusal instead. When false,
    # only the prompt-level STAY-IN-ROLE instruction applies.
    scope_guard_enabled: bool = _env_bool("SCOPE_GUARD_ENABLED", True)
    # Minimum classifier confidence required to BLOCK (bias toward allowing
    # genuine citizens — only block when the model is sure it's off-topic).
    scope_guard_threshold: float = float(os.getenv("SCOPE_GUARD_THRESHOLD", "0.6"))

    @property
    def is_production(self) -> bool:
        return self.production_mode or self.app_env in {"prod", "production"}

    def production_issues(self) -> list[str]:
        """Return blocking issues for a real public/government deployment.

        Local development may still use mock providers and open admin tools,
        but production startup must fail loudly instead of falling back to toy
        data or unauthenticated consoles.
        """
        if not self.is_production:
            return []
        issues: list[str] = []
        if self.llm_provider == "mock":
            issues.append("LLM_PROVIDER=mock is forbidden in production")
        if not self.sarvam_api_key:
            issues.append("SARVAM_API_KEY is required for live chat/STT/TTS/vision")
        if not self.sarvam_verify_ssl:
            issues.append("SARVAM_VERIFY_SSL=false is forbidden in production")
        if self.allow_mock_providers:
            issues.append("ALLOW_MOCK_PROVIDERS must be false in production")
        if self.allow_demo_routes:
            issues.append("ALLOW_DEMO_ROUTES must be false in production")
        if self.push_demo_enabled:
            issues.append("PUSH_DEMO_ENABLED must be false in production")
        if os.getenv("REQUIRE_AUTH", "false").lower() != "true":
            issues.append("REQUIRE_AUTH=true is required in production")
        if not os.getenv("ADMIN_API_TOKEN", ""):
            issues.append("ADMIN_API_TOKEN is required in production")
        if self.require_live_integrations:
            if not (self.twilio_account_sid and self.twilio_auth_token):
                issues.append("Twilio credentials are required when REQUIRE_LIVE_INTEGRATIONS=true")
            if not self.public_base_url:
                issues.append("PUBLIC_BASE_URL is required when REQUIRE_LIVE_INTEGRATIONS=true")
            if not (self.livekit_url and self.livekit_api_key and self.livekit_api_secret):
                issues.append("LiveKit credentials are required for production live call support")
        return issues

    def enforce_production_ready(self) -> None:
        issues = self.production_issues()
        if issues:
            joined = "\n  - ".join(issues)
            raise RuntimeError(
                "Production readiness check failed. Fix these settings before "
                "serving citizens:\n  - " + joined
            )

    @property
    def mock_mode(self) -> bool:
        """LEGACY (Phase 1-4): True when Sarvam-specific path has no key.

        Phase 4b's LLM abstraction has finer-grained provider mock detection
        — use llm.get_llm().mock_mode for the active provider. This property
        is kept for backward compat with voice/vision code that still maps to
        Sarvam directly (since those are Sarvam-only, not switchable).
        """
        return not self.sarvam_api_key

    @property
    def twilio_mock_mode(self) -> bool:
        return not (self.twilio_account_sid and self.twilio_auth_token)


settings = Settings()
