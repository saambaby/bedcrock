# Environment variables

Every variable read by `src/config.py`. Defaults in parentheses; required vars
have no default and the app will refuse to start without them.

---

## Mode

| Variable | Default | Description |
|---|---|---|
| `MODE` | `paper` | `paper` \| `live`. Selects broker adapter and tags signals/orders/positions accordingly. |

---

## Broker selection

| Variable | Default | Description |
|---|---|---|
| `BROKER` | `ibkr` | `ibkr` \| `alpaca`. Selects the adapter `make_broker()` returns. `ibkr` supports paper and live; `alpaca` is paper-only (US-only brokerage; live is refused at boot). |

The validator (`Settings._validate_broker_mode()`) enforces the §3 truth table
from `docs/V4_ALPACA_PLAN.md`:

| `BROKER` | `MODE` | Boot behaviour / error |
|---|---|---|
| `ibkr` | `paper` | OK if `IBKR_PORT ∈ {4002, 7497}`. Otherwise: `IBKR_PORT must be 4002 or 7497 when MODE=paper`. |
| `ibkr` | `live` | OK if `IBKR_PORT ∈ {4001, 7496}`. Otherwise: `IBKR_PORT must be 4001 or 7496 when MODE=live`. |
| `alpaca` | `paper` | OK if both `ALPACA_API_KEY` and `ALPACA_API_SECRET` are set. Otherwise: `ALPACA_API_KEY and ALPACA_API_SECRET are required when BROKER=alpaca`. |
| `alpaca` | `live` | **Refuse.** Error: `Alpaca live brokerage is US-only; use BROKER=ibkr for live in Canada.` |

`IBKR_*` vars are ignored when `BROKER=alpaca`; `ALPACA_*` vars are ignored
when `BROKER=ibkr`.

---

## Database

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://bedcrock:bedcrock@localhost:5432/bedcrock` | Async DSN — must include `+asyncpg`. |

---

## Broker — IBKR

IB Gateway or TWS must be running. See `docs/BROKER_SETUP.md` for full setup.
**All `IBKR_*` vars are ignored when `BROKER=alpaca`.**

| Variable | Default | Description |
|---|---|---|
| `IBKR_HOST` | `127.0.0.1` | IB Gateway/TWS host (usually local). |
| `IBKR_PORT` | `4002` | `4002`/`7497` paper, `4001`/`7496` live. |
| `IBKR_CLIENT_ID` | `1` | Per-connection unique ID. |
| `IBKR_ACCOUNT` | `""` | DUxxxxxx (paper) or Uxxxxxxx (live) account number. |

---

## Broker — Alpaca

Used only when `BROKER=alpaca`. Generate paper keys at
<https://app.alpaca.markets/paper/dashboard/overview>. See
`docs/BROKER_SETUP.md` § Path A for the full walk-through.

| Variable | Default | Description |
|---|---|---|
| `ALPACA_API_KEY` | `""` | Paper account Key ID (`APCA-API-KEY-ID` header on every REST call). Required when `BROKER=alpaca`. |
| `ALPACA_API_SECRET` | `""` | Paper account Secret Key (`APCA-API-SECRET-KEY` header). Stored as `SecretStr`; never logged. Required when `BROKER=alpaca`. |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` | REST base for orders/account/positions. Pinned to the paper host; do not point at `api.alpaca.markets`. |
| `ALPACA_DATA_URL` | `https://data.alpaca.markets` | Market-data base used by `get_last_price()`. |
| `ALPACA_STREAM_URL` | `wss://paper-api.alpaca.markets/stream` | WebSocket for `subscribe_trade_updates()`. Auto-reconnects with backoff on disconnect. |

---

## Data sources

| Variable | Source | Default | Notes |
|---|---|---|---|
| `QUIVER_API_KEY` | https://www.quiverquant.com/ | — | Hobbyist tier $30/mo Tier 1 access. |
| `UNUSUAL_WHALES_API_KEY` | https://unusualwhales.com | — | Required for options flow + UW congress. |
| `FINNHUB_API_KEY` | https://finnhub.io | — | Free tier: 60 req/min. |
| `POLYGON_API_KEY` | https://polygon.io | — | Optional. If unset, ohlcv falls back to yfinance. |
| `SEC_USER_AGENT` | self-set | `Bedcrock you@example.com` | SEC requires `Name email@host` format or 403s. |

---

## Discord

See `docs/DISCORD_SETUP.md` for end-to-end setup.

| Variable | Description |
|---|---|
| `DISCORD_WEBHOOK_FIREHOSE` | URL of #signals-firehose webhook (every signal). |
| `DISCORD_WEBHOOK_HIGH_SCORE` | URL of #high-score webhook (score ≥ 6). |
| `DISCORD_WEBHOOK_POSITIONS` | URL of #position-alerts webhook (entries, closes). |
| `DISCORD_WEBHOOK_SYSTEM_HEALTH` | URL of #system-health webhook (heartbeats). |
| `DISCORD_BOT_TOKEN` | Bot token from Developer Portal — for slash commands. |
| `DISCORD_GUILD_ID` | Optional. Set to your server ID for instant slash-command sync. Empty syncs globally (1h propagation). |

---

## API

| Variable | Default | Description |
|---|---|---|
| `API_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` if exposing publicly (then put a TLS proxy in front). |
| `API_PORT` | `8080` | Port. |
| `API_SIGNING_SECRET` | `change-me` | Required. Used to sign deep-link tokens (`itsdangerous`). Must be ≥ 32 chars in production. |

---

## Schedule

| Variable | Default | Description |
|---|---|---|
| `INGEST_INTERVAL_FAST_MIN` | `15` | Fast ingestors (UW flow, SEC). |
| `INGEST_INTERVAL_SLOW_MIN` | `30` | Slow ingestors (Quiver, UW congress). |
| `INGEST_EARNINGS_HOUR_ET` | `6` | Hour of day (ET) to refresh earnings calendar. |

---

## Risk limits

These are gate defaults read at startup from environment variables.

| Variable | Default | Description |
|---|---|---|
| `RISK_DAILY_LOSS_PCT` | `2.0` | Daily kill-switch threshold. |
| `RISK_PER_TRADE_PCT` | `1.0` | Equity at risk per trade. |
| `RISK_MAX_OPEN_POSITIONS` | `8` | Max concurrent positions. |
| `RISK_MIN_ADV_USD` | `5000000` | 30-day average dollar volume floor. |
| `RISK_EARNINGS_BLACKOUT_DAYS` | `3` | Block entries within N days of earnings. |
| `RISK_EVENT_BLACKOUT_DAYS` | `2` | Block entries within N days of FOMC/CPI/NFP (v0.2). |

---

## Observability

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` for development. |
| `LOG_FORMAT` | `json` | `json` for production (parseable by Loki/Datadog), `text` for development. |
| `SENTRY_DSN` | `""` | Optional. Errors auto-report. |

---

## v2 additions

These variables were introduced in v2 (see `bedcrock-plan.md`).

### Risk — half-Kelly cap and sector concentration (N2, N3)

| Variable | Default | Description |
|---|---|---|
| `RISK_MAX_POSITION_SIZE_PCT` | `0.05` | Half-Kelly per-position size cap as a fraction of equity. The order builder caps `qty` so notional ≤ `equity × this`. Defends against pathological tight-stop sizing. |
| `RISK_SECTOR_CONCENTRATION_LIMIT` | `0.25` | Sector-correlation gate ceiling — fraction of equity allowed in any one sector. The gate blocks new entries that would push a sector's open notional above this. |

### Heavy-movement ingestor (N1)

| Variable | Default | Description |
|---|---|---|
| `MOVEMENT_VOLUME_SPIKE_THRESHOLD` | `3.0` | Volume multiple over 30-day average that flags a "heavy movement" candidate. |
| `MOVEMENT_GAP_THRESHOLD` | `0.05` | Gap (open vs prior close) magnitude that flags a candidate. |
| `MOVEMENT_CHECK_INTERVAL_SECONDS` | `300` | Poll cadence for the heavy-movement ingestor. |

### Mode↔port coupling (invariant 9)

The config validator refuses to start when `MODE` and `IBKR_PORT` disagree
(when `BROKER=ibkr`):

- `MODE=paper` requires `IBKR_PORT ∈ {4002, 7497}`
- `MODE=live` requires `IBKR_PORT ∈ {4001, 7496}`

A mismatched pair raises a `ValueError` at import time — the bot will not run.
When `BROKER=alpaca`, this check is skipped (the validator dispatches per broker
per the §3 truth table above).

---

## Claude Code Routine variables

These variables are **not** read from bedcrock's `.env`. They are configured in
the Routine config at https://claude.ai/code/routines (per scheduled skill).
The Claude Code skills under `.claude/skills/` use them to call bedcrock's
FastAPI dashboard endpoints and post to Discord.

| Variable | Description |
|---|---|
| `API_BASE_URL` | Bedcrock FastAPI base URL (e.g. `https://bedcrock.example.com`). Skills hit `${API_BASE_URL}/dashboard/*` and `${API_BASE_URL}/scoring-proposals`. |
| `API_BEARER_TOKEN` | Bearer token for `/dashboard/*` and `/scoring-proposals`. Must match the server's `settings.api_bearer_token`, or fall back to `api_signing_secret` when bearer-token is unset. |
| `DISCORD_WEBHOOK_HIGH_SCORE` | Webhook the morning/intraday skills post high-score gameplans to. |
| `DISCORD_WEBHOOK_POSITIONS` | Webhook the hourly-closure skill posts position-state updates to. |
| `DISCORD_WEBHOOK_SYSTEM_HEALTH` | Webhook the weekly-synthesis + skill heartbeat posts go to. |

These mirror the same Discord webhook URLs that bedcrock itself uses
(`DISCORD_WEBHOOK_*` in the Discord section above) — the skills post directly
rather than via the bedcrock backend, so they need their own copies in the
Routine config.

---

## A note on secrets

- Never commit `.env`. The `.gitignore` already excludes it.
- Rotate API keys quarterly; broker keys yearly. Never share between paper and live.
- `API_SIGNING_SECRET` is what protects the deep-link confirm/skip URLs. If it
  leaks, anyone who saw a Discord embed in your server could submit orders.
