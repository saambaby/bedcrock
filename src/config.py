"""Application configuration. Reads from .env and environment.

All other modules import from here, never from os.environ directly.
"""

from __future__ import annotations

from enum import Enum

from pydantic import SecretStr, field_validator, model_validator
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
    # Bearer token for /dashboard/* and /scoring-proposals endpoints consumed
    # by Claude Code skills. If empty, falls back to api_signing_secret so
    # local-dev setups don't need a second secret.
    api_bearer_token: SecretStr = SecretStr("")

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
    # v2 (N3): half-Kelly per-position size cap — never more than this fraction
    # of equity in a single position regardless of how tight the stop is.
    risk_max_position_size_pct: float = 0.05
    # v2 (N2): sector concentration limit for the correlation gate.
    risk_sector_concentration_limit: float = 0.25

    # --- Heavy-movement ingestor (v2, N1) ---
    movement_volume_spike_threshold: float = 3.0
    movement_gap_threshold: float = 0.05
    movement_check_interval_seconds: int = 300

    # --- Observability ---
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON
    sentry_dsn: str = ""

    @field_validator("database_url")
    @classmethod
    def _database_url_async(cls, v: str) -> str:
        if "+asyncpg" not in v:
            raise ValueError("DATABASE_URL must use postgresql+asyncpg:// driver")
        return v

    # v2 invariant 9: mode and IBKR port are coupled. Mismatched config
    # (e.g. MODE=live with the paper port 4002) refuses to boot.
    @model_validator(mode="after")
    def _validate_mode_port(self) -> "Settings":
        valid_paper_ports = {4002, 7497}
        valid_live_ports = {4001, 7496}
        if self.mode == Mode.PAPER and self.ibkr_port not in valid_paper_ports:
            raise ValueError(
                f"MODE=paper requires IBKR_PORT in {sorted(valid_paper_ports)}, "
                f"got {self.ibkr_port}. 4002 is IB Gateway paper, 7497 is TWS paper."
            )
        if self.mode == Mode.LIVE and self.ibkr_port not in valid_live_ports:
            raise ValueError(
                f"MODE=live requires IBKR_PORT in {sorted(valid_live_ports)}, "
                f"got {self.ibkr_port}. 4001 is IB Gateway live, 7496 is TWS live."
            )
        return self

    # Convenience properties

    @property
    def is_paper(self) -> bool:
        return self.mode == Mode.PAPER

    @property
    def is_live(self) -> bool:
        return self.mode == Mode.LIVE


# Singleton — import this everywhere
settings = Settings()
