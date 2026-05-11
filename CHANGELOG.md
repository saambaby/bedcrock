# Changelog

All notable changes to bedcrock. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
