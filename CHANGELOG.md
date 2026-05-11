# Changelog

All notable changes to bedcrock. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.4.0] — 2026-05-11

**Theme:** add Alpaca as a second broker behind a generic `BrokerAdapter`; keep IBKR as the only live path.

Paper trading no longer requires IB Gateway/TWS. Set `BROKER=alpaca` plus two API keys and the full ingest → score → confirm → bracket → fill → close loop runs against Alpaca paper. Live trading remains IBKR-only (Alpaca brokerage is US-only and the user is in Canada — `BROKER=alpaca MODE=live` refuses to boot). The change is a contract refactor: every consumer (monitor, reconciler, scorer, workers) talks to `BrokerAdapter`, never a concrete broker class.

### Added
- Alpaca paper broker (`src/broker/alpaca.py`) over raw `httpx` + `websockets`. Equities only, paper base URL pinned to `paper-api.alpaca.markets`.
- `Broker` enum (`IBKR`, `ALPACA`) on `Settings`; new vars `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `ALPACA_BASE_URL`, `ALPACA_DATA_URL`, `ALPACA_STREAM_URL`.
- `BrokerAdapter` contract extended with `iter_open_orders()`, `iter_positions()`, `repair_child_to_gtc()`, and `subscribe_trade_updates()`. New dataclasses `OpenOrder`, `BrokerPosition`, `TradeUpdate` in `src/broker/base.py`.
- WebSocket trade-updates stream — `subscribe_trade_updates()` yields normalized `TradeUpdate` events with auto-reconnect and exponential backoff. IBKR side bridges `ib_async`'s `execDetailsEvent` + `orderStatusEvent` into the same generator shape.
- VCR cassette tests for `AlpacaBroker` under `tests/broker/` plus a re-record protocol in `tests/broker/cassettes/README.md`.
- Truth-table tests for `_validate_broker_mode()` covering all four `(BROKER, MODE)` cells.

### Changed
- `make_broker()` in `src/broker/__init__.py` dispatches on `settings.broker` and returns either `IBKRBroker` or `AlpacaBroker`.
- `src/safety/reconciler.py` and `src/orders/monitor.py` no longer import `IBKRBroker` concretely; they type their broker field as `BrokerAdapter` and reach `_ib` zero times. `audit_open_order_tifs` works on both adapters.
- `src/orders/monitor.py` main loop became `async for update in broker.subscribe_trade_updates(): ...`, replacing the IBKR-specific event callback. The 30s polling fallback now uses `broker.iter_open_orders()` and remains broker-agnostic.
- Audit-log rows include the broker tag (`settings.broker.value`) alongside the existing `MODE` tag so paper and live data on different brokers can be told apart.
- Discord embeds carry an `[alpaca-paper]` / `[ibkr-paper]` / `[ibkr-live]` prefix in the title so channel context is obvious without splitting channels.
- `Settings._validate_mode_port()` → `_validate_broker_mode()`; dispatches per broker per `docs/V4_ALPACA_PLAN.md` §3.
- `BROKER=alpaca MODE=live` refuses to boot with: `Alpaca live brokerage is US-only; use BROKER=ibkr for live in Canada.`

### Migration notes
- `BROKER` defaults to `ibkr`. **Existing v0.3 deployments need no env-var changes** — they keep running on IBKR as before.
- To switch to Alpaca paper: set `BROKER=alpaca` and provide `ALPACA_API_KEY` + `ALPACA_API_SECRET`. `IBKR_*` vars are ignored.
- No DB migration required; existing `audit_log` schema already had a free-form `event` field where the broker tag now lives.
- Run `python -m src.workers.healthcheck` after switching to confirm credentials and connectivity.

**Tagged commits:** Wave A (foundation), Wave B (Alpaca adapter + tests), Wave C (consumer refactor + WS plumbing), Wave D (docs) — see merge history on `v4-staging`.

---

## [0.3.0] — 2026-05-11

**Theme:** drop the vault layer; migrate reasoning to Claude Code Routines.

The vault writer (`src/vault/`) was discovered to have never been implemented — no-op stubs since v0.1. With Claude Code 2026 shipping cloud-hosted Routines via `/schedule`, the Cowork product (which the vault was bridging to) is no longer needed. The user has no Obsidian Sync paid subscription, so the vault gave zero advantage over Postgres. Result: the entire layer was removed and the reasoning surface migrated to native Claude Code skills.

### Added
- 5 Claude Code project skills under `.claude/skills/`: `morning-analyze`, `intraday-check`, `hourly-closure`, `weekly-synthesis`, `status`. Cloud-hosted via `/schedule` Routines.
- 5 FastAPI dashboard endpoints (`GET /dashboard/{morning,intraday,closures,weekly,status}`) consumed by the skills via `curl`. Bearer-auth.
- `POST /scoring-proposals` endpoint for the weekly-synthesis skill to submit weight changes.
- DB tables: `scoring_proposals`, `scoring_replay_reports` (replace the v0.2 `99 Meta/scoring-rules-proposed.md` vault file).
- Config: `API_BEARER_TOKEN` (with `API_SIGNING_SECRET` fallback).
- New tests: `tests/test_dashboard.py` (7 endpoint tests), `tests/test_eod_worker.py` (2 EOD tests).
- `CHANGELOG.md` (this file).

### Changed
- README rewritten — vault/Syncthing/Obsidian framing removed. Status bumped to v0.3.
- `bedcrock-plan.md` rewritten (924 → 791 lines): three-layer authority model (broker → DB → Claude Code), Routine-based reasoning, dashboard endpoints, no Obsidian appendices.
- `docs/DEPLOYMENT.md` — Syncthing setup section removed; Claude Code Routines setup section added.
- `docs/ENV.md` — `VAULT_PATH` removed; Claude Code Routine env vars documented.
- `docs/AUDIT.md` — v3-status appendix added.
- `docs/AUDIT_2026-05-10.md` — top-of-doc resolution banner clarifying vault findings are obsolete.
- `src/workers/eod_worker.py` — daily-note vault writes replaced with EOD Discord summary embed; replay reports persist to DB instead of `06 Weekly/*.md`.
- `.env.example` — `VAULT_PATH` removed; v2/v3 risk + movement settings added; Routine env vars documented.

### Removed
- `src/vault/` directory (writer, frontmatter helper, `__init__`).
- `cowork-prompts/` directory (4 files; semantics moved to `.claude/skills/`).
- `vault-templates/` directory (12 files; templates and meta seeds no longer needed).
- `docs/COWORK_INTEGRATION.md`.
- `tests/test_vault.py`.
- `Signal.vault_path` and `Position.vault_path` columns (alembic 0003).
- `python-frontmatter` dependency.

### Migration notes
- Run `alembic upgrade head` to apply migration 0003 (drops vault_path columns, creates scoring tables).
- Set `API_BEARER_TOKEN` in `.env`; configure matching value in Claude Code Routine env vars.
- Register the 5 routines via `/schedule` from a `claude` session at the bedcrock root.

**Tagged commits:** Wave A (`53daf1b`, `db0d226`), Wave B (`16f4de8`, `7281b1e`, `1686ae1`, `05ec4e9`, `2bc01d1`), Wave C (`168fb66`, `7016bcb`, `6771773`), final merge `4da20e0`.

---

## [0.2.0] — 2026-05-10

**Theme:** audit fixes + selective ports from Proxy Bot research.

Surfaced 6 blockers in v0.1 code via [`docs/AUDIT_2026-05-10.md`](docs/AUDIT_2026-05-10.md), and ported 4 components from a parallel Proxy Bot research pass (heavy-movement ingestor, sector-correlation gate, half-Kelly cap, mini-backtester). Implemented across 4 waves of parallel agents (~25 minutes wall time).

### Added
- Heavy-movement ingestor (`src/ingestors/heavy_movement.py`) — corroboration source only (volume spike + 52w high + gap), never triggers drafts on its own. George & Hwang 2004 alpha.
- Sector-correlation gate — concrete implementation of v1 §10 stub. SECTOR_ETF_MAP for ~40 tickers; 25% concentration cap.
- Half-Kelly per-position size cap — never more than 5% of equity in one position regardless of risk math.
- Mini-backtester (`src/backtest/replay.py`) — re-scores historical signals under proposed weights, requires out-of-sample Sharpe > baseline before recommending ADOPT.
- Reconciler (`src/safety/reconciler.py`) — `audit_open_order_tifs` re-issues any non-GTC bracket child; `reconcile_against_broker` catches orphan IBKR positions on startup.
- Tests: `test_v2_invariants.py` (5 integration invariants), expanded `test_orders.py`, `test_gates.py`, `test_scoring.py`, `test_broker_safety.py`, `test_dashboard.py`, `test_daily_pnl.py`, `test_eod_worker.py`, `test_backtester.py`.

### Changed
- **F1:** `ib_insync` → `ib_async==2.1.0` (the `ib_insync` library was unmaintained after Ewald de Wit's death in 2024).
- **F2:** Bracket stop & take-profit children now `tif="GTC"`, `outsideRth=True`. Without this, overnight stops expired at 16:00 ET.
- **F3:** Idempotency check on `_on_entry_fill` + `UNIQUE(Position.broker_order_id)` constraint. Prevents duplicate Position rows when WS handler and 30s polling reconciler race.
- **F4:** `_reconcile_against_broker` runs on `LiveMonitor.start()`. Catches orphan IBKR positions if bot crashed mid-fill.
- **F5:** `daily_pnl_pct` wired end-to-end via `update_daily_pnl` worker task → `daily_kill_switch` gate now actually trips at -2% (was a stub).
- **F6:** Connection retry with exponential backoff in `IBKRBroker.connect`; IBC + nightly logout documented in DEPLOYMENT.md.

### New invariants (added to plan §2)
7. **Broker truth wins on conflict.** On startup or post-disconnect, IBKR is the source of truth; DB is repaired to match (with audit-log entry per repair).
8. **Stops are GTC by construction.** No code path may submit a child order with `tif != "GTC"`. Reconciler audit re-issues any non-conforming order found on the wire.
9. **Mode and port are coupled.** `MODE=paper` requires `IBKR_PORT ∈ {4002, 7497}`; `MODE=live` requires `{4001, 7496}`. Mismatched config refuses to boot.

**Tagged commits:** see Appendix C of [`bedcrock-plan.md`](bedcrock-plan.md) for the full v2 commit table.

---

## [0.1.0] — 2026-05-04

**Theme:** initial codebase.

First implementation of the bedcrock spec. Postgres-backed signal aggregation from politician trades (Quiver), insider buys (SEC Form 4), options flow (Unusual Whales), earnings calendar (Finnhub). Signal scoring + hard gates, IBKR adapter (paper + live, port-controlled), Discord bot for `/confirm`/`/skip` approval flow, FastAPI for signed deep links, 5-worker process model under systemd.

Initial test suite: 70 tests covering scorer, gates, orders, vault (vault tests later removed in v0.3.0 when the vault layer itself was deleted — they had been testing an aspirational interface that was never wired up).

**Known gaps acknowledged at release time** (see [`docs/AUDIT.md`](docs/AUDIT.md) for the full review):
- IBKR adapter shipped before connection retry / reconciliation logic — addressed in v0.2.0.
- Vault writer was a stub; production wrote nothing — discovered and resolved in v0.3.0 by deleting the entire layer.
- Daily kill switch was a stub — wired in v0.2.0.
- Correlation gate was a stub — implemented in v0.2.0.
