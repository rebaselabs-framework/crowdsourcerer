from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # App
    app_name: str = "CrowdSorcerer API"
    app_version: str = "0.1.0"
    debug: bool = False
    port: int = 8100

    # Public-facing site URL. Used for email templates, password-reset
    # links, OAuth redirect builders, OpenAPI server metadata, and system
    # alert notifications. Override per deployment so staging / preview /
    # white-label stacks do not link back to the production domain.
    public_site_url: str = "https://crowdsourcerer.rebaselabs.online"

    @property
    def public_site_host(self) -> str:
        """Bare host (no scheme) — used as display text in email footers."""
        return (
            self.public_site_url
            .removeprefix("https://")
            .removeprefix("http://")
            .rstrip("/")
        )

    # Observability — Sentry error tracking
    # Leave sentry_dsn empty to disable (zero-cost no-op init). When set,
    # unhandled exceptions in the FastAPI app, background workers, and
    # the narrowed try/except catches (webhooks, sweeper, notifications,
    # etc.) are automatically captured with request context.
    sentry_dsn: str = ""
    sentry_environment: str = "production"
    sentry_traces_sample_rate: float = 0.1      # 10% APM sampling
    sentry_profiles_sample_rate: float = 0.0    # profiling off by default

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/crowdsourcerer"

    # Security
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30  # 30-minute access tokens (refresh tokens handle long sessions)
    refresh_token_expire_days: int = 30  # refresh tokens last 30 days
    api_key_salt: str = "change-me-in-production"

    # ── LLM provider configuration ────────────────────────────────────
    # llm_generate / data_transform / web_research go through
    # core.llm_client.get_llm_client(), which picks a provider based
    # on ``llm_provider`` (or auto-detects from whichever key is set).
    # Set LLM_PROVIDER explicitly to 'anthropic' / 'gemini' / 'openai'
    # to disambiguate when multiple keys are configured.
    llm_provider: str = ""  # empty → auto-detect
    llm_default_model: str = ""  # empty → per-provider default
    llm_timeout_seconds: float = 60.0

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_default_model: str = "claude-haiku-4-5-20251001"

    # Google Gemini
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    gemini_default_model: str = "gemini-2.5-flash"

    # OpenAI
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com"
    openai_default_model: str = "gpt-4o-mini"

    # RebaseKit (legacy — retained for pii fallback only; now unused)
    rebasekit_api_key: str = ""
    rebasekit_base_url: str = "https://api.rebaselabs.online"

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id_starter: str = ""
    stripe_price_id_pro: str = ""

    # Credits
    free_tier_credits: int = 1000  # credits given on signup (beta — generous for onboarding)
    credits_per_usd: int = 100     # 1 USD = 100 credits

    # Crypto payment addresses (owner-controlled)
    btc_address: str = "bc1qvzvwjcvpcztwcuv5ef42frzlq2xn46g7usxfcm"
    sol_address: str = "8tGVz7wUr89bVQVR4MbiUQPfnEHeGPzSgjWqjorsZ91o"
    evm_address: str = "0x16F086e2292eA895B0eC3a4DeBb255e3d6fD9E01"

    # CORS
    cors_origins: list[str] = [
        "http://localhost:4321",
        "https://crowdsourcerer.rebaselabs.online",
        "https://crowd.rebaselabs.online",
    ]

    # Email (SMTP)
    email_enabled: bool = False          # Set true once SMTP is configured
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = "noreply@crowdsourcerer.rebaselabs.online"
    smtp_use_tls: bool = True            # True = SSL (port 465), False = STARTTLS (port 587)
    admin_email: str = ""                # Recipient for system health alert emails (defaults to smtp_from if blank)

    # System alert thresholds
    alert_error_rate_window_minutes: int = 5    # Rolling window for 5xx error counting
    alert_error_rate_threshold: int = 10        # Fire alert if >= N errors in window
    alert_sweeper_stall_minutes: int = 15       # Fire alert if sweeper hasn't run in N minutes
    alert_cooldown_hours: int = 1               # Don't re-fire same alert type within N hours

    # Webhook secret encryption
    # Fernet key for encrypting webhook signing secrets at rest.
    # Generate with: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # If empty, a deterministic key is derived from jwt_secret (sufficient for dev).
    webhook_encryption_key: str = ""

    # Google OAuth (social login)
    # To enable: set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET from Google Cloud Console
    # Redirect URI to register: https://crowdsourcerer.rebaselabs.online/v1/auth/google/callback
    google_client_id: str = ""
    google_client_secret: str = ""

    # Task result cache
    task_result_cache_enabled: bool = True      # Master on/off switch
    # Per-type TTL overrides (hours; 0 = never expire).  Env var: CACHE_TTL_WEB_RESEARCH etc.
    # These are optional — the hardcoded defaults in core/result_cache.py are used otherwise.
    cache_ttl_web_research: int | None = None
    cache_ttl_screenshot: int | None = None
    cache_ttl_web_intel: int | None = None
    cache_ttl_audio_transcribe: int | None = None
    cache_ttl_document_parse: int | None = None
    cache_ttl_data_transform: int | None = None
    cache_ttl_llm_generate: int | None = None
    cache_ttl_entity_lookup: int | None = None
    cache_ttl_pii_detect: int | None = None
    cache_ttl_code_execute: int | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
