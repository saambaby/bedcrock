# Bedcrock

**Vault context:** `<vault-repo>/nore.vault/projects/bedcrock/bedcrock.md`
Read this on every session for architecture, decisions, patterns, gotchas. Also check `projects/bedcrock/context.md` for live state. Resolve `<vault-repo>` from `~/.claude/.vault-config`.

## Stack at a glance

- Python 3.11+ async backend — FastAPI + SQLAlchemy[asyncio]/asyncpg on Postgres
- Broker — `BROKER=ibkr|alpaca`. IBKR (`ib_async` against IB Gateway, paper 4002 / live 4001) is the only live path. Alpaca (raw `httpx` + `websockets` against `paper-api.alpaca.markets`) is paper-only. `BROKER=alpaca MODE=live` refuses to boot.
- Reasoning — Claude Code Routines in `.claude/skills/` (morning-analyze, intraday-check, hourly-closure, weekly-synthesis, status), registered via `/schedule`
- Alerts/control — discord.py + discord-webhook; humans `/confirm` entries, broker enforces exits via server-side OCO
- Deploy — `docker compose` (dev), systemd on VPS (prod); see `docs/DEPLOYMENT.md`

## Common commands

```
poetry install              # or: pip install -e .[dev]
alembic upgrade head
python -m src.workers.healthcheck
docker compose up           # all services
uvicorn src.api.main:app --host 0.0.0.0 --port 8080
pytest
ruff check .
mypy src
```

## Notes

- No in-process LLM. Don't reintroduce one — reasoning lives in `.claude/skills/` only.
- No mocks in prod paths. Tests use VCR cassettes recorded against real endpoints (Alpaca paper cassettes use `httpx.MockTransport` until recorded — see `tests/broker/cassettes/README.md`).
- Never emit a child order with `tif != "GTC"`. Two-layer safety net: every adapter verifies + self-repairs on submit, and the reconciler audits drift on the wire. Don't rely on either alone.
- Consumers must talk to `BrokerAdapter` (`src/broker/base.py`), not concrete classes. No `from src.broker.ibkr import IBKRBroker` outside `src/broker/`. No `broker._ib` outside `src/broker/ibkr.py`.
- Postgres is canonical for signals/orders history; the broker is the source of truth for live positions/open orders. On conflict, the broker wins and the DB is repaired with a broker-tagged audit-log row.
- Boot-time validator dispatches per broker (see `_validate_broker_mode` in `src/config.py`): IBKR has MODE↔port coupling; Alpaca refuses live and requires the two API keys for paper. Don't add code that bypasses it.
- `docs/AUDIT.md` is the running per-component review log — append to it on any PR that touches a reviewed component.
