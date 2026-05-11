# Bedcrock

**Vault context:** `<vault-repo>/nore.vault/projects/bedcrock/bedcrock.md`
Read this on every session for architecture, decisions, patterns, gotchas. Also check `projects/bedcrock/context.md` for live state. Resolve `<vault-repo>` from `~/.claude/.vault-config`.

## Stack at a glance

- Python 3.11+ async backend — FastAPI + SQLAlchemy[asyncio]/asyncpg on Postgres
- Broker — `ib_async` against IB Gateway (paper 4002, live 4001; MODE↔port coupling enforced at boot)
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
- No mocks in prod paths. Tests use VCR cassettes recorded against real endpoints, not hand-written fakes.
- Never emit a child order with `tif != "GTC"`. The reconciler is the safety net; don't rely on it.
- Postgres is canonical for signals/orders history; IBKR is the source of truth for live positions/open orders. On conflict, the broker wins and the DB is repaired with an audit-log row.
- `MODE=paper` requires `IBKR_PORT ∈ {4002, 7497}`; `MODE=live` requires `{4001, 7496}`. Don't add code that bypasses the boot-time check in `src/config.py`.
- `docs/AUDIT.md` is the running per-component review log — append to it on any PR that touches a reviewed component.
