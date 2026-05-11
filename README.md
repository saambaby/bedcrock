# Bedcrock

Always-on backend that ingests politician trades, hedge fund filings, insider buys, and options flow; scores them with hard gates; computes an indicator/regime layer per ticker; and emits one-click bracket orders to a paper or live broker via Discord. Reasoning runs on Claude Code Routines that consume the data via a FastAPI read layer.

**Status:** v0.3 — built for paper trading on Interactive Brokers. Same broker for paper and live (just different ports).

## What this is

This is the implementation of the Bedcrock system plan. The plan is the spec; this code is the spec made real.

## Quick start

```bash
# 1. Postgres + Python deps
cp .env.example .env                  # fill in keys
docker compose up -d postgres         # or use a managed Postgres
poetry install                        # or: pip install -e .

# 2. Migrate
alembic upgrade head

# 3. Sanity check
python -m src.workers.healthcheck

# 4. Run all services (dev mode)
docker compose up
# or run each separately:
#   python -m src.workers.ingest_worker
#   python -m src.workers.monitor_worker
#   python -m src.workers.bot_worker
#   uvicorn src.api.main:app --host 0.0.0.0 --port 8080
```

For production deployment (VPS + systemd), see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Reasoning layer (Claude Code Routines)

Bedcrock has no in-process LLM. The backend is a deterministic pipeline (ingest → score → gate → write); reasoning lives in five Claude Code skills under `.claude/skills/`:

- **`morning-analyze/`** — 06:30 ET weekday gameplan: triages overnight signals, picks the day's priority tickers with triggers/stops/targets.
- **`intraday-check/`** — 12:00 and 14:00 ET reality checks against the morning plan.
- **`hourly-closure/`** — top-of-hour during market hours: reviews open positions for stop/target/thesis-break exits.
- **`weekly-synthesis/`** — Sunday 19:00 ET retrospective: what worked, what didn't, what to change.
- **`status/`** — on-demand health and positions snapshot.

Each skill calls the FastAPI read endpoints (`/dashboard/morning`, `/positions`, etc.) with a bearer token, then posts decisive embeds to the appropriate Discord webhook. Register them as cloud-hosted Routines from a `claude` session in this repo via `/schedule` (one per skill) — see `docs/DEPLOYMENT.md`. Routines run on Anthropic infrastructure against your Pro/Max subscription, not on the VPS.

## Project layout

```
bedcrock/
├── alembic/                   # DB migrations
├── .claude/skills/            # Claude Code Routines (reasoning layer)
│   ├── morning-analyze/
│   ├── intraday-check/
│   ├── hourly-closure/
│   ├── weekly-synthesis/
│   └── status/
├── src/
│   ├── config.py              # env loading
│   ├── db/                    # SQLAlchemy models, async session
│   ├── schemas/               # Pydantic request/response models
│   ├── ingestors/             # one per data source
│   │   └── heavy_movement.py  # v2 N1 — volume/52w/gap corroboration ingestor
│   ├── indicators/            # OHLCV + indicator computation
│   ├── scoring/               # scorer + hard gates (incl. v2 sector-correlation)
│   ├── broker/                # ibkr adapter (paper + live, port-switched)
│   ├── orders/                # bracket builder, live monitor
│   ├── safety/                # v2 — startup reconciler (broker truth wins)
│   ├── backtest/              # v2 N4 — mini-replay for scoring-rule changes
│   ├── discord_bot/           # webhooks + slash command bot
│   ├── api/                   # FastAPI: health, /confirm, /skip, dashboard reads
│   └── workers/               # process entry points (one per systemd unit)
│       └── daily_pnl.py       # v2 F5 — populates daily_pnl_pct for kill switch
├── deploy/
│   ├── systemd/               # unit files for VPS deployment
│   └── docker/                # Dockerfile
└── docs/
    ├── DEPLOYMENT.md
    ├── DISCORD_SETUP.md
    ├── BROKER_SETUP.md
    ├── ENV.md
    └── AUDIT.md
```

## Design invariants

These come from the plan and are enforced in code:

1. **Paper and live share one path.** Differs only by `mode` env var and broker adapter selection.
2. **Postgres is the canonical store; Claude Code skills are the reasoning surface; Discord is the alert + control plane; broker (IBKR) is the source of truth for live positions and open orders.**
3. **Humans confirm entries; the broker enforces exits.** Server-side OCO at the broker. Bot never opens positions without your `/confirm`.
4. **No mocks in prod.** All ingestors talk to real endpoints. Tests use VCR cassettes against real responses, not hand-written fakes.
5. **Broker truth wins on conflict.** (v2) On startup or post-disconnect reconnect, IBKR's view of positions and open orders is the source of truth; the DB is repaired to match, with an audit-log entry per repair.
6. **Stops are GTC by construction.** (v2) No code path may submit a child order with `tif != "GTC"`. The reconciler audit re-issues any non-conforming order found on the wire.
7. **Mode and port are coupled.** (v2) `MODE=paper` requires `IBKR_PORT ∈ {4002, 7497}`; `MODE=live` requires `{4001, 7496}`. Mismatched config refuses to boot.

## Audit trail

`docs/AUDIT.md` records the per-component review notes from the initial build. Every PR that touches a component appends to it.

## License

Personal use only. This is not a commercial product. Don't sell signals from it.
