# Audit log

Per-component review of v0.1. The plan in `bedcrock-plan.md` is the
spec; this doc records how the implementation aligns and where it diverges.
Each section answers: **what's solid, what's a known gap, what's deferred to v0.2+**.

Reviewed: 2026-05-03

---

## Foundation: config, db models, alembic

**Solid**

- `src/config.py` reads `.env` via Pydantic Settings. Validates `DATABASE_URL` uses `+asyncpg` and (historically, v0.1/0.2) that `VAULT_PATH` is absolute — these were the two failure modes in the planning doc's risk register. (v0.3.0 dropped `VAULT_PATH` entirely — see the v3-status appendix.)
- `src/db/models.py` covers every entity from plan §5: Trader, Signal, Indicators, EarningsCalendar, DraftOrder, Position, EquitySnapshot, Snooze, IngestorHeartbeat, AuditLog. All enums (`Mode`, `SignalSource`, `SignalStatus`, `Action`, `GateName`, `OrderStatus`, `PositionStatus`, `CloseReason`) match the plan.
- The Mode tag (`paper`|`live`|`baseline`) on every relevant table means paper and live records coexist for side-by-side comparison — the plan's invariant #1.
- Alembic migration `0001_initial.py` matches models 1:1.

**Gap**

- (Resolved) Broker factory now uses IBKR for both paper and live. Paper vs live is controlled by IBKR_PORT.

**Deferred**

- ~~Rehydrate worker that rebuilds DB from vault.~~ Obsolete in v0.3.0 — vault layer was deleted; if DB is wiped, you re-run alembic and the next ingest cycles repopulate (Postgres is the single source of truth).
- Per-trader size percentile backfill (used by scorer's `size` component). Currently uses a flat $50k threshold heuristic.

---

## Ingestors: SEC, Quiver, UW (flow + congress), Finnhub, OHLCV

**Solid**

- All five ingestors hit verified, current endpoints (May 2026):
  - SEC EDGAR: `efts.sec.gov/LATEST/search-index` for Form 4 search; `www.sec.gov/Archives/...` for the XML
  - Quiver: `api.quiverquant.com/beta/live/congresstrading`
  - Unusual Whales: `api.unusualwhales.com/api/option-trades/flow-alerts` and `/api/congress/recent-trades`
  - Finnhub: `finnhub.io/api/v1/calendar/earnings`
  - Polygon: `api.polygon.io` (with yfinance fallback so the system runs without a Polygon key)
- Dedupe via `(source, source_external_id)` unique constraint with `ON CONFLICT DO NOTHING` — re-runs are safe.
- Heartbeats written on every run (success or failure) → `/health` and `/heartbeat` slash command surface staleness.
- Tenacity retry on transient HTTP errors with exponential backoff.
- `IngestorRegistry` is the only place ingestors are wired up — adding a new source = one register call.

**Gap**

- SEC EDGAR Form 4 ingestor relies on `efts.sec.gov`'s search response shape. SEC has changed this twice in 5 years; if it breaks the symptom is "zero new SEC signals for >6h." The heartbeat alert covers this.
- Unusual Whales `MIN_PREMIUM` filter is hardcoded at $100k. This should move to a runtime-tunable setting (originally planned for `99 Meta/scoring-rules.md` in the vault era; now would land as a `scoring_proposals` row or a settings table column).
- IV percentile (`iv_percentile_30d` field on Indicators) is always `None` in v0.1 — the UW ingestor doesn't populate it. Indicator computer leaves it null; the scorer doesn't depend on it yet.

**Deferred to v0.2**

- 13D/13G ingestor (activist filings)
- 13F ingestor (quarterly fund holdings) — explicitly low priority because the lag (45 days) defeats the system's edge
- News sentiment ingestor — gates `news_sentiment` score component

---

## Indicators

**Solid**

- `compute.py` uses pandas-ta (Wilder ATR via `ta.atr`, `ta.rsi`) for the indicators that have well-known formulas with subtle gotchas.
- Sector ETF mapping covers the 50 largest names and falls back to SPY-only RS for unknowns.
- Trend regime (`uptrend`/`downtrend`/`chop`) is the same simple rule the plan §5.8 specifies: price > SMA50 > SMA200, etc.
- ADV30 is dollar volume, not share count — what actually matters for liquidity gates.

**Gap**

- Compute is on-demand (per-ticker, per-signal). No daily batch refresh. With 60-90 unique tickers per day this works fine; if signal volume scales to 500+ tickers we'll need to batch.
- Setup hint (`breakout`/`pullback`/`base`/`mean_reversion`) is left at `none` — the morning reasoning pass interprets structure manually rather than auto-tag (v0.1/0.2: Cowork's morning prompt; v0.3.0: the `morning-analyze` Claude Code skill).

**Deferred**

- Volatility-of-volatility regime (used by some advanced setups)
- Sector RS leadership ranking (which sectors lead today vs SPY)

---

## Scoring + gates

**Solid**

- `scorer.py` is **pure** — takes `RawSignal` + pre-fetched context, returns `(score, breakdown)`. No I/O. Easy to unit test.
- `gates.py` has six concrete gates: liquidity, earnings_proximity, snoozed, max_open_positions, daily_kill_switch, stale_signal. All read from typed `GateContext`.
- LiquidityGate fail-closes when ADV data is missing — if we don't know the liquidity, we don't trade.
- All score components default to 0 when their inputs are missing — the score is monotonic in available evidence.

**Gap**

- `daily_kill_switch` reads `daily_pnl_pct` from a context value that nobody currently populates. The live monitor would need to compute and stash it; v0.1 leaves it at 0, so the gate effectively never trips. **Mitigation**: relies on broker-side risk limits; user should set IBKR account-level risk limits as a backstop.
- `event_proximity` (FOMC/CPI) and `correlation` gates are listed in the enum but have no concrete classes. They're stubs.

**Deferred**

- Trader track record component (`trader_track_record`). The weekly synthesis is supposed to populate per-trader stats; the scorer currently passes `None`, which scores as 0. No regression — just opportunity unrealized.
- Committee → sector match for politicians.
- Public statement alignment.

---

## Broker layer

**Solid**

- `BrokerAdapter` ABC has the minimum surface: `get_account`, `submit_bracket`, `cancel_order`, `get_order`, `get_last_price`, `aclose`.
- `client_order_id` is set to the `DraftOrder.id` UUID — broker rejects duplicate submissions, so confirm-twice is safe.
- Bracket orders are server-side OCO. If the VPS dies, exits still fire.
- IBKR adapter is fully implemented. Paper and live use the same code path — only the port differs.

**Gap**
- Position-level partial fills are tracked at the broker level but the Position row records full quantity at the entry fill — partial follow-up fills aren't summed. **Impact**: rare in practice for liquid names; observable in the audit log if it happens.

**Deferred**

- Multi-broker portfolio aggregation
- Fractional share support (IBKR supports it for some symbols; we round to whole shares)
- Order modification (we only `cancel + resubmit`)

---

## Orders: builder + monitor

**Solid**

- `OrderBuilder` validates: stop on correct side of entry, ATR-floor (auto-widens stops < 1.5×ATR), R:R ≥ 1.5, position size > 0.
- Risk-based sizing: `qty = (equity × risk_pct/100) / |entry - stop|`.
- `LiveMonitor` subscribes to IBKR's `orderStatusEvent` + `execDetailsEvent` for instant fill notifications + 30s polling fallback for missed events.
- Closures fire a Discord alert. (v0.1/0.2 also dropped a closure event in `00 Inbox/` for the hourly Cowork run; in v0.3.0 the hourly-closure skill polls the dashboard endpoint instead — vault writes are gone.)

**Gap**

- The polling fallback (`_reconcile_orders`) only catches FILLED state. If a draft is REJECTED while the WS is offline, the DB row stays in SENT until the next websocket reconnect.
- Fill events rely on `orderRef` matching the draft UUID. If the ref is lost (e.g. IB Gateway restart mid-order), the polling fallback catches it via `broker_order_id`.
- `setup_at_entry` is set on the Position row from the DraftOrder, but the order builder never gets the setup string from anywhere — the human (or, in v0.1/0.2, Cowork) would need to set it on the draft at confirm time. **Currently null** in v0.1.

**Deferred**

- Trailing stop adjustments mid-trade
- Scale-out / partial-target logic
- Time-based force-close (e.g., "close at end of week if not stopped")

---

## Vault writer

> **Obsolete in v0.3.0.** The vault writer (`src/vault/`) was deleted; reasoning
> migrated to Claude Code Routines + FastAPI dashboard endpoints. The historical
> notes below describe the v0.1/0.2 design only.

**Solid (historical, v0.1/0.2)**

- Writes only to `00 Inbox/`, `02 Open Positions/`, `03 Closed/` per plan invariant #2 (inbox-then-process).
- Frontmatter is YAML-safe; bodies are templated markdown.
- Filename conventions: `<date>-<TICKER>-<source>.md` for signals, `<TICKER>-<entry-date>.md` for positions.
- Closure event has `urgent: true` frontmatter so the hourly Cowork run can prioritize.

**Gap (historical)**

- No vault → DB rehydrate. (Moot in v0.3.0 — Postgres is the only source of truth.)
- No file lock when writing — if the backend writes while Syncthing is mid-replicate, you can get a `.sync-conflict-` file. (Moot in v0.3.0.)

---

## Discord

**Solid**

- Three webhooks (firehose, high-score, position-alerts, system-health) decoupled — you can mute the noisy one without losing critical alerts.
- Slash commands cover the human-facing flows: `/confirm`, `/skip`, `/positions`, `/pnl`, `/thesis`, `/snooze`, `/heartbeat`.
- Bot uses `discord.py` slash command tree — modern, supported.
- Webhook posts are fire-and-forget with httpx async; webhook failures don't crash ingestion.

**Gap**

- No image/chart embeds yet (e.g., a price chart on the high-score embed). Discord supports it; could be a v0.2 nice-to-have.
- No reaction-based confirmation (✅/❌ on the draft message). Slash command is more explicit; reactions could be a UX improvement later.

---

## API

**Solid**

- `/health` exposes per-ingestor heartbeat ages — drives external monitoring.
- `/healthz` is a minimal liveness probe for k8s/uptime services.
- `/confirm/{id}` and `/skip/{id}` accept JSON bodies with `actor` for audit-log attribution.
- `itsdangerous` signed deep links (`/c/<token>`, `/s/<token>`) for one-tap mobile confirm. Tokens expire with the draft (8h).
- Docs are exposed at `/docs` only in paper mode (security: don't advertise live endpoints).

**Gap**

- No API rate limiting. Confirm/skip endpoints would benefit from it; the signing secret + draft-state-must-be-DRAFT acts as a backstop.
- No HTTPS in the bundled config — assumed to be terminated by a reverse proxy (Caddy/nginx). Without TLS, the API_SIGNING_SECRET protects the tokens themselves but not the bearer-on-the-wire.

---

## Workers

**Solid**

- Five entry points: `ingest_worker`, `monitor_worker`, `bot_worker`, `api_worker`, `eod_worker` (cron'd) + `healthcheck` CLI.
- APScheduler wires ingestors at their declared `interval_seconds`.
- Each worker has a clear single responsibility.
- SIGTERM handling on ingest_worker for clean shutdown via systemd.

**Gap**

- No worker auto-restart on crash within the worker — relies on systemd `Restart=always`. Fine in production but means a crash loop won't surface fast unless you watch journalctl.

---

## Cowork integration

> **Replaced in v0.3.0.** Cowork prompts were retired in favour of five Claude
> Code skills (`morning-analyze`, `intraday-check`, `hourly-closure`,
> `weekly-synthesis`, `status`) under `.claude/skills/`, fired by Claude Code
> Routines (`/schedule`) on the same cadence. Skills hit the FastAPI
> `/dashboard/*` endpoints — no shared filesystem required. The historical
> notes below describe the v0.1/0.2 design only.

**Solid (historical, v0.1/0.2)**

- Four prompts cover the full operating cadence (morning heavy, intraday light, hourly closure, weekly synthesis).
- Strict separation: backend writes inbox, Cowork writes watchlist + analysis. (Documented in the now-deleted `COWORK_INTEGRATION.md`.)
- Vault-as-source-of-truth means Cowork on a different host can fully reason without backend access.

**Gap (historical)**

- The "promote to ACT-TODAY" hand-off relies on the human seeing the morning brief and choosing to confirm — there's no auto-create-draft path. **By design** per plan invariant #4 (humans confirm entries). Carries forward to v0.3.0.

---

## Tests

**Gap**

No test suite in v0.1. The components most worth testing are:

1. `Scorer.score()` — pure, easy to unit test
2. `Gate` classes — straightforward with mock contexts
3. `OrderBuilder.build_draft()` — validation logic for stop side, R:R, ATR floor
4. ~~Vault frontmatter round-trip~~ (obsolete in v0.3.0)

These are all on the v0.1.1 backlog. The risk of shipping without them is mitigated by:
- Paper-only mode for the first 90 days
- Manual `/confirm` on every order (no auto-fire)
- Audit log on every consequential action (we can replay any failure)

---

## Open questions to revisit before live

1. **Daily kill-switch wiring** — make sure `daily_pnl_pct` actually populates from the live monitor.
2. **Scoring rule loader** — read weights from `99 Meta/scoring-rules.md` YAML at runtime so tweaks ship without redeploy.
3. **Tests** — at minimum, scorer + gates + order builder before flipping `MODE=live`.

---

## Summary

v0.1 is **paper-ready**. It can:
- ingest from 4 paid + 2 free sources
- score with a 9-component model
- gate with 6 active blockers
- build risk-sized bracket orders
- monitor live fills via WS + polling fallback
- post to 4 Discord channels
- accept human confirm/skip via Discord or signed deep link
- write to a structured Obsidian vault for Cowork to reason over (historical v0.1; deleted in v0.3.0 — reasoning now via Claude Code Routines hitting FastAPI dashboard endpoints)

It is **paper-ready on IBKR**. To go live, meet the plan §9 graduation criteria (Sharpe > 1.0, 50+ closed trades, 90 days), then switch `IBKR_PORT` to 4001 and `MODE=live`.

---

## Resume-session reconciliation (2026-05-03)

This pass merged two parallel build threads into one consistent codebase:

**Aliases added so workers and modules speak the same names:**

- `src/db/session.py` — added `async_session = get_session` alias. Both names work; older callers prefer `SessionLocal` + `get_session`, newer ones use `async_session()` as a context manager.
- `src/broker/__init__.py` — added `get_broker = make_broker` alias plus re-exports of `BaseBroker = BrokerAdapter`, `AccountState = AccountSnapshot`, `SubmittedBracket = BrokerOrder` so `ibkr.py` (stub) compiles against the same base interface.
- `src/broker/base.py` — same three aliases at the bottom for direct importers.
- `src/orders/builder.py` — added `BracketBuilder = OrderBuilder` alias.
- `src/indicators/__init__.py` — exports both `SECTOR_ETF` (the actual dict) and `DEFAULT_SECTOR_ETFS` (alias).
- `src/discord_bot/webhooks.py` — rewritten with the kwarg-shaped functions the workers call (`post_firehose(ticker=..., action=..., source=..., score=...)`, `post_high_score(...with breakdown and draft_id)`, `post_system_health(title=..., body=..., ok=...)`). Adds `HIGH_SCORE_THRESHOLD = 6.0` constant and `post_firehose_signal` alias.
- `src/discord_bot/bot.py` — added `async def run()` entry point so `bot_worker.py` can `from src.discord_bot.bot import run as run_bot`.
- `src/vault/writer.py` — (historical, v0.1/0.2 only; deleted in v0.3.0) appended sync wrapper functions (`write_signal`, `write_position`, `write_closure_event`, `write_draft_order`, `ensure_vault_layout`) that delegate to `VaultWriter` class methods. Let monitor.py and other older callers use the function-style API.

**Verification results:**

- All 41 `src/*.py` files parse cleanly (`ast.parse` no errors).
- Cross-module import resolution: every `from src.X import Y` resolves to a real export.
- Modules that fail to import in the build sandbox (`src.db.session`, `src.broker.ibkr`, `src.discord_bot.webhooks`, etc.) all fail because `asyncpg`/`ib_insync`/`httpx`/`discord` aren't pip-installed — the deps are correctly listed in `pyproject.toml` and will resolve on the VPS after `pip install .`.

**Files NOT touched in this pass (existing build was already correct):**

`src/config.py`, `src/db/models.py`, `src/logging_config.py`, `src/schemas/__init__.py` (the comprehensive Pydantic version with `RawSignal`, `ScoredSignal`, `ScoreBreakdown`, `GateResult`, `IndicatorSnapshot`, `BracketOrderSpec`, `FillEvent`, `DraftOrderPayload`, etc.), `src/scoring/scorer.py`, `src/scoring/gates.py` (existing `GateEvaluator` class), `src/indicators/compute.py`, all 5 ingestors, `src/api/main.py`, all 6 workers, `src/vault/writer.py` (existing `VaultWriter` class — since deleted in v0.3.0), all 4 cowork prompts (since deleted in v0.3.0), all 6 vault-templates (since deleted in v0.3.0), all 5 systemd units, all 6 docs files, all 3 test files, `pyproject.toml`, `docker-compose.yml`, `alembic/versions/0001_initial.py`.

**Known minor inconsistencies remaining (low priority):**

1. Two scorer styles coexist: the canonical pure-logic `Scorer.score(raw, prior, indicators)` (used by `ingest_worker`) and a DB-aware `score_pending_signals(db)` helper I drafted earlier in this session. The worker uses the canonical one; the helper is dead code that can be removed in v0.2 cleanup.
2. The `Scorer` returns `(total, ScoreBreakdown_pydantic)` from `src/schemas/__init__.py` — different from the `@dataclass ScoreBreakdown` I defined in `src/schemas/signal.py`. The Pydantic one wins (it's what the workers use). The dataclass version in `signal.py` is unused; remove in v0.2.
3. `DraftOrderPayload` is defined twice: in `src/schemas/__init__.py` (Pydantic, comprehensive) and in `src/schemas/order.py` (Pydantic, simpler). The init version wins; `order.py` is unused.

These do not affect correctness — they're cruft from the merge. Cleanup is a v0.1.1 chore, not a blocker.

---

## v2 status (2026-05-10)

The 2026-05-10 audit (`docs/AUDIT_2026-05-10.md`) surfaced 6 blockers (F1–F6) and a parallel research pass identified 4 components worth porting from a competing "Proxy Bot" design (N1–N4). All 10 items, plus the duplicate-scorer cleanup, landed on `v2-staging` over four parallel waves (A/B/C/D).

| Item | Audit ref | Status | Landing commit |
|---|---|---|---|
| F1 — `ib_insync` → `ib_async` migration | §3.1 | Landed | `90ec1ad` |
| F2 — `tif="GTC"` + `outsideRth=True` on stop/take-profit children | §3.2 | Landed | `de5eaa0` |
| F3 — Idempotency check on `_on_entry_fill` + `UNIQUE(Position.broker_order_id)` | §3.3 | Landed | `f324062` |
| F4 — `_reconcile_against_broker` on `LiveMonitor.start()` | §3.4 | Landed | in B1 reconciler commit (`de5eaa0`) |
| F5 — `daily_pnl_pct` wired end-to-end → `daily_kill_switch` actually trips | §3.5 | Landed | `071a82d` |
| F6 — Connection retry with backoff + IBC + nightly-logout docs | §3.6 | Landed | B1 (`de5eaa0`) + Wave D docs |
| N1 — Heavy-movement ingestor (volume + 52w-high + gap) | §3.N1 | Landed | `3a2b658` |
| N2 — Concrete sector-correlation gate | §3.N2 | Landed | `2fd9354` |
| N3 — Half-Kelly per-position size cap (5%) | §3.N3 | Landed | B4 (`2fd9354`) |
| N4 — Mini-backtester for scoring-rule evaluation | §3.N4 | Landed | `7c2955c` |
| Cleanup — duplicate scorers / `DraftOrderPayload` shims | §3.cleanup | Already done pre-v2 | — |

The canonical spec at `bedcrock-plan.md` is now `status: active, version: v2` and reflects v0.2.0 reality. v2 also added three new safety invariants (broker-truth-wins, GTC-by-construction, mode↔port coupled) — see `bedcrock-plan.md` §2 and Appendix C (Version history).

**Test status at merge candidate:** 118 of 123 tests pass. The 5 failing tests are all in `tests/test_vault.py` and predate v2 (they exercise a real `VaultWriter` that's not present on this branch tree — tracked as a v0.1 issue, not in v2 scope). **(Resolved in v0.3.0: `tests/test_vault.py` was deleted with the rest of the vault layer.)**

---

## v3 status (2026-05-11)

v0.3.0 was a single-purpose refactor: **delete the vault layer, retire Cowork
prompts, and migrate reasoning to Claude Code Routines + FastAPI dashboard
endpoints.** Driver: the user has no Obsidian Sync / Syncthing setup, so a
VPS-side vault was never accessible from phone or laptop, and the vault writer
had silently been no-op stubs since v0.1. Postgres is now the single source
of truth.

| Item | Description | Landing commit |
|---|---|---|
| Drop `vault_path` from config + `Signal`/`Position` models | Removed `VAULT_PATH` Field, `_vault_path_absolute` validator, and the two `vault_path` columns | `53daf1b` |
| Alembic 0003 — drop vault columns + add scoring tables | Drops `signals.vault_path` and `positions.vault_path`; adds `scoring_proposals` + `scoring_replay_reports` for the weekly-synthesis skill | `db0d226` |
| Delete `src/vault/` + fix call sites | Removed writer/frontmatter modules; pruned imports + write calls from `ingest_worker` and `orders/monitor`; deleted `tests/test_vault.py` | `16f4de8` |
| EOD worker — drop vault writes, persist replay reports to DB | `write_daily_note` removed; weight proposals now read from `scoring_proposals` table; EOD summary posts to Discord | `7281b1e` |
| Five Claude Code skills replace four Cowork prompts | `morning-analyze`, `intraday-check`, `hourly-closure`, `weekly-synthesis`, `status` under `.claude/skills/` — fired by `claude.ai/code/routines` cron | `1686ae1` |
| FastAPI `/dashboard/*` + `/scoring-proposals` | Read endpoints the skills hit via `curl` with `API_BEARER_TOKEN`; weekly synthesis POSTs proposed weights | `05ec4e9` |
| Remove `cowork-prompts/`, `vault-templates/`, vault deps | Both directories deleted; `python-frontmatter` and related deps dropped from `pyproject.toml` | `2bc01d1` |

For the v0.3.0 architecture rationale and the per-wave plan, see Appendix C in
`bedcrock-plan.md` and the `v3-staging` branch history.

---

## v4 status (2026-05-11)

v0.4.0 added Alpaca as a second broker behind `BrokerAdapter`. IBKR remains the
only live path (Alpaca brokerage is US-only). Plan: `docs/V4_ALPACA_PLAN.md`.
Four parallel waves landed on `v4-staging` before squash-merge to `main`.

### `src/broker/alpaca.py` — Alpaca paper adapter

**Solid.** Raw `httpx` + `websockets` (no `alpaca-py`) keeps the dep surface
small and the transport debuggable. REST hits `paper-api.alpaca.markets` only;
the base URL is pinned in config so a stray key can't be aimed at the live
endpoint. `submit_bracket` passes `client_order_id` for idempotency and treats
`422 code=40010001` ("client_order_id already exists") as success by fetching
the existing order. `tenacity.AsyncRetrying` with `wait_random_exponential` (3
attempts, max 10s, treats `429`/5xx as retriable) absorbs Alpaca paper's
200 req/min cap. **Gap.** Equities only — no crypto, no options; matches
current scope. **Watch.** Alpaca occasionally returns DAY-TIF children on
bracket submit even when the parent is GTC; the adapter verifies and self-
repairs but the reason for the inconsistency is not documented upstream.

### `src/broker/base.py` — contract extension

**Solid.** Adds `iter_open_orders()`, `iter_positions()`, `repair_child_to_gtc()`,
and `subscribe_trade_updates()` as the only new surfaces. `OpenOrder`,
`BrokerPosition`, `TradeUpdate` dataclasses carry a `raw: dict` field so
broker-specific shape stays inspectable without bloating the typed contract.
Default `subscribe_trade_updates` raises `NotImplementedError`; both adapters
override. **Gap.** No abstract method for partial-fill aggregation — Position
rows still record full quantity at entry fill (carried over from v0.1). Logged
as deferred.

### `src/safety/reconciler.py` — broker-agnostic refactor

**Solid.** `audit_open_order_tifs(broker: BrokerAdapter)` walks
`broker.iter_open_orders()` and calls `broker.repair_child_to_gtc()` on any
child found with `tif != "gtc"`. `reconcile_against_broker` uses
`broker.iter_positions()` for orphan detection. No `_ib` access remains
outside `src/broker/ibkr.py`; `grep -r "broker\._ib" src/` returns zero
matches outside that file. Existing tests pass against `IBKRBroker`; the same
suite runs against `AlpacaBroker` VCR cassettes for parity. **Gap.** The
audit-log `event` field is now stringly-typed across two brokers; a dedicated
column for `broker` would be cleaner but is migration cost we punted.

### `src/orders/monitor.py` — trade-updates loop

**Solid.** Main loop is now `async for update in broker.subscribe_trade_updates()`,
broker-agnostic. The 30s polling fallback survives and reads
`broker.iter_open_orders()`. IBKR's `subscribe_trade_updates` bridges
`ib.execDetailsEvent` + `ib.orderStatusEvent` into an `asyncio.Queue`; Alpaca's
opens the WS, authenticates with the secret, subscribes to `trade_updates`,
and yields. Both reconnect on disconnect with exponential backoff and the
polling loop covers any window in between. **Gap.** The Alpaca WebSocket free
tier rate-limits aggressive reconnects; persistent flapping will surface as
`alpaca_stream_reconnect` log noise — alertable, not a correctness issue.
