"""Application configuration. Reads from .env and environment.

All other modules import from here, never from os.environ directly.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class LogFormat(str, Enum):
    JSON = "json"
    TEXT = "text"


class Settings(BaseSettings):
    """Top-level settings. Environment variables override .env which overrides defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Mode ---
    mode: Mode = Mode.PAPER

    # --- Database ---
    database_url: str = "postgresql+asyncpg://bedcrock:bedcrock@localhost:5432/bedcrock"

    # --- Vault ---
    vault_path: Path = Field(default=Path("/home/bedcrock/vault/Trading"))

    # --- Broker (IBKR) ---
    # Paper: port 4002 (Gateway) or 7497 (TWS)
    # Live:  port 4001 (Gateway) or 7496 (TWS)
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002
    ibkr_client_id: int = 1
    ibkr_account: str = ""

    # --- Data sources ---
    quiver_api_key: SecretStr = SecretStr("")
    unusual_whales_api_key: SecretStr = SecretStr("")
    finnhub_api_key: SecretStr = SecretStr("")
    polygon_api_key: SecretStr = SecretStr("")
    sec_user_agent: str = "Bedcrock you@example.com"

    # --- Discord ---
    discord_webhook_firehose: str = ""
    discord_webhook_high_score: str = ""
    discord_webhook_positions: str = ""
    discord_webhook_system_health: str = ""
    discord_bot_token: SecretStr = SecretStr("")
    discord_guild_id: int | None = None

    # --- API ---
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    api_signing_secret: SecretStr = SecretStr("change-me")

    # --- Schedule ---
    ingest_interval_fast_min: int = 15
    ingest_interval_slow_min: int = 30
    ingest_earnings_hour_et: int = 6

    # --- Risk limits (overridden by 99-Meta/risk-limits.md if present) ---
    risk_daily_loss_pct: float = 2.0
    risk_per_trade_pct: float = 1.0
    risk_max_open_positions: int = 8
    risk_min_adv_usd: float = 5_000_000
    risk_earnings_blackout_days: int = 3
    risk_event_blackout_days: int = 2

    # --- Observability ---
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON
    sentry_dsn: str = ""

    @field_validator("vault_path")
    @classmethod
    def _vault_path_absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError(f"VAULT_PATH must be absolute, got {v}")
        return v

    @field_validator("database_url")
    @classmethod
    def _database_url_async(cls, v: str) -> str:
        if "+asyncpg" not in v:
            raise ValueError("DATABASE_URL must use postgresql+asyncpg:// driver")
        return v

    # Convenience properties

    @property
    def is_paper(self) -> bool:
        return self.mode == Mode.PAPER

    @property
    def is_live(self) -> bool:
        return self.mode == Mode.LIVE


# Singleton — import this everywhere
settings = Settings()
