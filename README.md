# Bedcrock

Always-on backend that ingests politician trades, hedge fund filings, insider buys, and options flow; scores them with hard gates; computes an indicator/regime layer per ticker; writes signal `.md` files into your Obsidian vault for Cowork to reason over; and routes one-click bracket orders to a paper or live broker.

**Status:** v0.1 — built for paper trading on Interactive Brokers. Same broker for paper and live (just different ports).

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

For production deployment (VPS + Syncthing + systemd), see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Project layout

```
bedcrock/
├── alembic/                   # DB migrations
├── src/
│   ├── config.py              # env loading
│   ├── db/                    # SQLAlchemy models, async session
│   ├── schemas/               # Pydantic request/response models
│   ├── ingestors/             # one per data source
│   ├── indicators/            # OHLCV + indicator computation
│   ├── scoring/               # scorer + hard gates
│   ├── broker/                # alpaca, ibkr (stub)
│   ├── orders/                # bracket builder, live monitor
│   ├── vault/                 # writes .md files into the Obsidian vault
│   ├── discord_bot/           # webhooks + slash command bot
│   ├── api/                   # FastAPI: health, /confirm, /skip
│   └── workers/               # process entry points (one per systemd unit)
├── vault-templates/           # frontmatter templates + 99-Meta seed files
├── cowork-prompts/            # the four scheduled-task prompts
├── deploy/
│   ├── systemd/               # unit files for VPS deployment
│   └── docker/                # Dockerfile
└── docs/
    ├── DEPLOYMENT.md
    ├── COWORK_INTEGRATION.md
    ├── DISCORD_SETUP.md
    ├── BROKER_SETUP.md
    ├── ENV.md
    └── AUDIT.md
```

## Design invariants

These come from the plan and are enforced in code:

1. **Paper and live share one path.** Differs only by `mode` env var and broker adapter selection.
2. **The vault is the source of truth.** The DB is a fast cache; if it disappears, you can rebuild from the vault.
3. **Inbox-then-process.** Backend writes only to `00 Inbox/`. Cowork writes everywhere else.
4. **Humans confirm entries; the broker enforces exits.** Server-side OCO at the broker. Bot never opens positions without your `/confirm`.
5. **No mocks in prod.** All ingestors talk to real endpoints. Tests use VCR cassettes against real responses, not hand-written fakes.

## Audit trail

`docs/AUDIT.md` records the per-component review notes from the initial build. Every PR that touches a component appends to it.

## License

Personal use only. This is not a commercial product. Don't sell signals from it.
