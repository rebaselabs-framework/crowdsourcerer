from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # App
    app_name: str = "CrowdSorcerer API"
    app_version: str = "0.1.0"
    debug: bool = False
    port: int = 8100

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/crowdsourcerer"

    # Security
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 days
    api_key_salt: str = "change-me-in-production"

    # RebaseKit (worker APIs)
    rebasekit_api_key: str = ""
    rebasekit_base_url: str = "https://api.rebaselabs.online"

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id_starter: str = ""
    stripe_price_id_pro: str = ""

    # Credits
    free_tier_credits: int = 100   # credits given on signup
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
