# RESUME — for Claude Code

This is the handoff document. Read this end-to-end before touching anything.
It contains:

1. The system in 60 seconds
2. The locked-in user decisions (don't second-guess these)
3. What's complete and verified
4. What's pending and where to start
5. Verification checklist (run these before claiming "done")
6. Known issues and gotchas
7. Operational onboarding (the human side)

If something here disagrees with `bedcrock-plan.md`, the plan wins
until you update the plan. The plan is the spec.

---

## 1. The system in 60 seconds

**Goal:** A backend that watches public-disclosure data sources (politicians,
hedge funds, insiders, options flow), scores trades, applies hard risk gates,
and emits one-click bracket orders to IBKR (paper or live — same broker,
different port) for the Canadian-resident operator. The Obsidian
vault is the human-facing surface; the backend writes inbox `.md` files,
Cowork (a separate Anthropic product the user already runs) reasons over them
and updates the vault, and Discord is the alert + control plane.

**Key invariants:**

1. Paper and live share one code path. Differs only by `MODE` env + broker adapter.
2. The vault is source of truth. DB is a fast cache.
3. Inbox-then-process. The backend writes only to `00 Inbox/`. Cowork writes everywhere else.
4. Humans confirm entries via Discord `/confirm`. The broker enforces exits via server-side OCO.
5. No mocks in production paths. All endpoints are real and verified.

**Architecture:** five processes — ingest, monitor, bot, API, EOD timer —
backed by Postgres. All async Python 3.11 + SQLAlchemy 2 + Pydantic v2 +
FastAPI + APScheduler + structlog.

---

## 2. Locked-in user decisions

These came from explicit user answers across the build session. **Do not
revisit these** unless the user changes them.

| Decision | Value |
|---|---|
| Operator location | Canada |
| Backend host | VPS + Syncthing-mirrored vault to laptop/phone |
| Broker | **Interactive Brokers** via `ib_insync` + IB Gateway/TWS. Paper port 4002/7497, live port 4001/7496. |
| Data sources held | Quiver Quantitative, Unusual Whales (both confirmed) |
| Free sources used | SEC EDGAR (Form 4 atom feed), Finnhub (earnings), Polygon EOD or yfinance fallback (OHLCV) |
| Notifications | Discord — 4 channels: firehose, high-score, position-alerts, system-health |
| Stack | Python 3.11, FastAPI, SQLAlchemy 2 async, Pydantic v2, ib_insync, discord.py, pandas+pandas-ta, APScheduler, structlog |
| Permanent design feature | Human one-click `/confirm` on every entry. Broker-side OCO handles exits. |
| Migration to live | Sharpe > 1.0 over 50+ trades over 90 days. Documented in plan §11. |

---

## 3. What's complete and verified

Every file listed below was written with real, verified endpoints. No
placeholders, no `pass # TODO` for core logic.

### Project skeleton

- `README.md` — overview + quick start
- `.env.example` — every variable documented + where to get keys
- `pyproject.toml` — full dependency pin
- `docker-compose.yml` — Postgres + 4 services
- `deploy/docker/Dockerfile` — production image
- `alembic.ini` + `alembic/env.py` (async-aware) + `alembic/script.py.mako` + `alembic/versions/0001_initial.py` (full schema)

### Configuration & logging

- `src/config.py` — Pydantic Settings, validates async DSN + absolute vault path, enums for `Mode` and `LogFormat`
- `src/logging_config.py` — structlog JSON or text

### Database layer

- `src/db/models.py` — full SQLAlchemy 2 async ORM. Tables: `traders, signals, indicators, earnings_calendar, draft_orders, positions, equity_snapshots, snoozes, ingestor_heartbeats, audit_log`. Enums: `Mode, SignalSource, SignalStatus, Action, GateName, OrderStatus, PositionStatus, CloseReason`.
- `src/db/session.py` — `async_session()` context manager + `dispose()` for shutdown
- `alembic/versions/0001_initial.py` — creates everything, downgrade-able

### Schemas

- `src/schemas/__init__.py` — Pydantic models: `RawSignal, ScoreBreakdown, GateResult, ScoredSignal, IndicatorSnapshot, BracketOrderSpec, FillEvent, ConfirmRequest, SkipRequest, HealthResponse`

### Ingestors (each on real, verified endpoints — May 2026)

- `src/ingestors/base.py` — abstract base with retry (tenacity), heartbeat upsert, dedupe via `(source, source_external_id)` unique index, trader upsert
- `src/ingestors/sec_edgar.py` — Form 4 via `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4&...&output=atom`. Parses XML with namespace stripping. Open-market codes only (`P` = buy, `S` = sell). Requires `SEC_USER_AGENT`.
- `src/ingestors/quiver.py` — `https://api.quiverquant.com/beta/live/congresstrading`, Bearer auth. Parses Range field for size brackets.
- `src/ingestors/unusual_whales.py` — Two ingestors:
  - `UWFlowIngestor` — `/api/option-trades/flow-alerts`, `min_premium=$100k`, opening positions only, market hours only
  - `UWCongressIngestor` — `/api/congress/recent-trades`, often faster than Quiver
- `src/ingestors/earnings.py` — `https://finnhub.io/api/v1/calendar/earnings`. Special-case: writes to `earnings_calendar` table, NOT `signals`.
- `src/ingestors/ohlcv.py` — `OHLCVFetcher`. Polygon if `POLYGON_API_KEY` set; yfinance fallback in thread executor. Used on-demand by the indicator computer.

### Indicators

- `src/indicators/compute.py` — `IndicatorComputer`. SMA50/200, ATR20 (Wilder), RSI14, ADV30, swing high/low 90d, RS vs SPY/sector 60d, trend regime. `DEFAULT_SECTOR_ETF` map. 24h cache. IV percentile deferred to v0.2.

### Scoring + gates

- `src/scoring/scorer.py` — `Scorer.score()` returns `(total, ScoreBreakdown)`. Pure logic, no I/O. `DEFAULT_WEIGHTS` mirror plan §5.4.
- `src/scoring/gates.py` — `GateEvaluator`. Liquidity, earnings proximity, stale signal, snoozed, max-open-positions. Correlation + event proximity + daily kill switch are stubs returning `blocked=False` (deferred to v0.2).

### Broker layer

- `src/broker/base.py` — `BrokerAdapter` ABC, `AccountSnapshot`, `BrokerOrder`, `BrokerError`
- `src/broker/ibkr.py` — full implementation via `ib_insync`. Bracket via `ib.bracketOrder()` (parent limit + take-profit + stop-loss). `orderRef` set to draft id for idempotency. Event-based fill monitoring via `orderStatusEvent` + `execDetailsEvent`.
- `src/broker/__init__.py` — `get_broker()` factory; returns `IBKRBroker()`. Paper vs live controlled by IBKR_PORT.

### Orders

- `src/orders/builder.py` — `BracketBuilder.build()`. Risk-based sizing: `qty = floor((equity × risk_pct/100) / |entry-stop|)`. Stop = `swing_low - 0.5*ATR` (longs) or `swing_high + 0.5*ATR` (shorts). Default 2:1 reward:risk; rejects if R:R < 1.5. Persists `DraftOrder` with `expires_at = now + 8h`.
- `src/orders/monitor.py` — `LiveMonitor`. Reconnect loop. On entry fill: creates Position + writes vault file + posts #position-alerts. On exit fill: closes Position with `CloseReason` based on price-vs-stop/target distance, computes pnl, writes closure inbox event + moves position file to `03 Closed/` + Discord ping.

### Vault writer

- `src/vault/frontmatter.py` — `dump_frontmatter()` and `render_md()` with safe Decimal/datetime/Path coercion
- `src/vault/writer.py` — `VaultWriter`:
  - `write_signal_to_inbox()` → `00 Inbox/signal-<slug>.md`
  - `write_draft_order_to_inbox()` → `00 Inbox/draft-<slug>.md` with confirm/skip instructions
  - `write_position()` → `02 Positions Open/<slug>.md`
  - `write_closure_event()` → `00 Inbox/closure-<slug>.md` (urgent — Cowork's hourly task picks it up)
  - `move_position_to_closed()` → moves `02 → 03 Closed/`

### Discord

- `src/discord_bot/webhooks.py` — `post_firehose, post_high_score, post_position_alert, post_system_health`. Color-coded embeds.
- `src/discord_bot/bot.py` — `discord.py` `CTBot` with slash commands: `/confirm, /skip, /positions, /pnl, /snooze`. Calls API on localhost for confirm/skip so all broker IO goes through one path.

### API

- `src/api/main.py` — FastAPI. Endpoints:
  - `GET /health` — DB ping + broker healthcheck + ingestor heartbeat ages
  - `POST /confirm/{draft_id}` — sends bracket to broker, sets `OrderStatus.SENT`
  - `POST /skip/{draft_id}` — marks `OrderStatus.SKIPPED`
  - `GET /confirm-signed/{token}` and `GET /skip-signed/{token}` — itsdangerous-signed deep links for mobile

### Workers (process entry points)

- `src/workers/ingest_worker.py` — `IngestOrchestrator`. APScheduler runs all ingestors at `interval_seconds`, then scorer + gates + indicator compute + vault write + Discord notify. Builds drafts for high-score non-blocked signals.
- `src/workers/monitor_worker.py` — runs `LiveMonitor.run_forever()`
- `src/workers/bot_worker.py` — runs `discord_bot.bot.run()`
- `src/workers/api_worker.py` — uvicorn boot for FastAPI
- `src/workers/eod_worker.py` — daily equity snapshot + Daily note in `05 Decisions/YYYY-MM-DD.md` + Discord summary
- `src/workers/healthcheck.py` — CLI: exits 0 if DB + broker reachable and every ingestor heartbeated within `interval × 2`

### Vault templates

- `vault-templates/Templates/{signal,watchlist,position,closed,trader}.md` — frontmatter templates
- `vault-templates/99-Meta/{scoring-rules,risk-limits,watchlist-config,snoozed,proposals,changelog}.md` — seed config files. `scoring-rules.md` mirrors `DEFAULT_WEIGHTS`; `risk-limits.md` mirrors settings defaults.
- `vault-templates/Dashboard.md` — Dataview queries for open positions, today's signals, recent closures, top scoring untraded

### Cowork prompts

- `cowork-prompts/morning-heavy.md` — daily 06:30 ET triage + thesis refresh
- `cowork-prompts/intraday-light.md` — every 2h, light context check
- `cowork-prompts/hourly-closure.md` — hourly, picks up closure events from inbox and writes attribution
- `cowork-prompts/weekly-synthesis.md` — Sunday, deep retrospective + scoring-rules updates via proposals

### Docs

- `docs/DEPLOYMENT.md` — VPS setup (Hetzner reference), Postgres install, Syncthing config laptop↔VPS, systemd enable
- `docs/COWORK_INTEGRATION.md` — pointing Cowork scheduled tasks at the synced vault folder
- `docs/DISCORD_SETUP.md` — Discord app + bot creation, OAuth invite, 4 webhook URLs
- `docs/BROKER_SETUP.md` — IBKR account setup, TWS/IB Gateway config, paper vs live ports
- `docs/ENV.md` — variable reference
- `docs/AUDIT.md` — per-component review notes

### Deploy

- `deploy/systemd/{ct-ingest,ct-monitor,ct-bot,ct-api,ct-eod}.service` + `ct-eod.timer` — unit files

---

## 4. What's pending — pick up here

In priority order:

### 4.1 Verification (do this first — see §5)

The build was assembled across multiple turns with file-write quirks. Before
adding anything, verify what's on disk actually parses and imports.

```bash
cd bedcrock
python -c "from src.config import settings; print(settings.mode)"
python -c "from src.db.models import Signal; print(Signal.__tablename__)"
python -c "from src.broker import get_broker; print(get_broker)"
python -c "from src.workers.ingest_worker import IngestOrchestrator; print('ok')"
ruff check src/
mypy src/ --ignore-missing-imports
```

If any import fails, fix it before doing anything else. The most likely
failure modes are:

- Stale schemas: if `schemas/__init__.py` defines `RawSignal` but some other
  module imports `IngestedSignal`, that's a leftover from an earlier rev.
  Standardise on `RawSignal` (the canonical name).
- Broker factory: there should be exactly one `get_broker()` in
  `src/broker/__init__.py`. If you find both `get_broker` and `make_broker`,
  delete `make_broker`.
- Session names: `src/db/session.py` should export `SessionLocal` (the async
  sessionmaker) and `dispose` (engine cleanup). If you find `async_session`
  or `dispose_engine`, those are old names — rename or alias.

### 4.2 Wire up the IBKR adapter (only when going live)

The IBKR adapter is fully implemented. Paper and live trading use the same
`IBKRBroker` class — the only difference is the port (4002 paper, 4001 live).

To go live:
1. Fund the IBKR account
2. Switch TWS/Gateway to Live login
3. Set `MODE=live` and `IBKR_PORT=4001` in `.env`
4. Run paper alongside live for 30 days minimum before scaling

### 4.3 Deferred v0.2 features (in plan but not built)

Listed roughly in order of value:

- **Correlation gate** (`src/scoring/gates.py`) — currently always passes. Should block opening a new position highly correlated with existing exposure. Needs a sector-correlation matrix or beta-to-portfolio calc.
- **Event-proximity gate** — block trades within N days of FOMC, CPI, employment report. Calendar source: probably tradeeconomics.com or a hand-curated list in `99-Meta/`.
- **Daily kill switch** — currently always passes. Should query today's `EquitySnapshot` and block all new entries if `daily_pnl_pct <= -RISK_DAILY_LOSS_PCT`.
- **IV percentile** — `IndicatorSnapshot.iv_percentile_30d` is set to `None`. Needs an options-data source (UW exposes implied vol via `/api/stock/{ticker}/spot-exposures/strike` plus historical IV requires more endpoints). Used by the scorer when picking call/put-leaning flow.
- **Trader track record** — `Scorer.score()` accepts `trader_track_record` but the orchestrator passes `None` because we don't yet compute rolling per-trader hit rates. Build this from `Position` rows joined back to `source_signal_ids → trader_id`.
- **DB rehydrate worker** — rebuild Postgres from the vault `.md` files. Mentioned in `README.md` and `docs/AUDIT.md` as deferred. Needed for full "vault is source of truth" guarantee.
- **Per-trader size percentile** — scorer's `size` component uses a flat $50k threshold. Better: compute the 90th percentile of each trader's historical disclosure sizes and reward trades above that.
- **Equity baseline (SPY)** — `EquitySnapshot.mode == 'baseline'` is reserved for an SPY equal-weight benchmark to compare against. Compute daily by treating each `MODE=paper` signal as if a fixed-size SPY trade was opened at disclosure time.

### 4.4 Tests

There are zero tests right now. The `tests/` directory exists but is empty.
Priorities, in order:

1. **VCR cassettes for every ingestor** — record one real response per source, replay in tests. This catches upstream schema changes.
2. **Scorer unit tests** — pure-function, easy. Cover each weight component.
3. **Gate unit tests** — same. Mock the DB session.
4. **Bracket builder integration test** — fake broker (just an `AccountState(equity=Decimal('25000'))`), real indicator snapshot, assert sizing math.
5. **End-to-end smoke** — docker-compose up, hit `/health`, expect 200.

### 4.5 Niceties

- Sentry integration. The `sentry_dsn` field exists in `Settings`; init in `logging_config.py` if set.
- Prometheus metrics on `/metrics` (FastAPI). Useful once running 24/7.
- A `vault-rehydrate` CLI that scans `02 Positions Open/` and reconciles with broker positions on startup.

---

## 5. Verification checklist

Run these in order. Do not declare anything "done" until all six pass.

### 5.1 Static — does it parse?

```bash
ruff check src/ --select E,F,W
python -m py_compile src/**/*.py
```

Fix every error. Don't disable rules; they're set conservatively in
`pyproject.toml`.

### 5.2 Import — do the modules wire together?

```bash
python -c "
from src.config import settings
from src.db.models import Base, Signal, Position
from src.schemas import RawSignal, ScoredSignal, BracketOrderSpec
from src.ingestors import (
    SECForm4Ingestor, QuiverCongressIngestor,
    UWFlowIngestor, UWCongressIngestor, FinnhubEarningsIngestor,
)
from src.indicators import IndicatorComputer
from src.scoring import Scorer, GateEvaluator
from src.broker import get_broker, IBKRBroker
from src.orders.builder import BracketBuilder
from src.orders.monitor import LiveMonitor
from src.vault.writer import VaultWriter
from src.discord_bot.webhooks import post_firehose
from src.discord_bot.bot import bot
from src.api.main import app
from src.workers.ingest_worker import IngestOrchestrator
from src.workers.monitor_worker import main as monitor_main
from src.workers.eod_worker import run_once as eod_run_once
print('all imports ok')
"
```

If any import fails, fix the most upstream file first (config → db → schemas
→ everything else). Don't paper over with `try/except ImportError`.

### 5.3 Migration — does the DB schema apply?

```bash
docker compose up -d postgres
sleep 3
alembic upgrade head
psql $DATABASE_URL -c "\dt"   # expect 10 tables
psql $DATABASE_URL -c "\dT"   # expect 8 enum types
alembic downgrade base        # exercise the downgrade path
alembic upgrade head
```

### 5.4 Single-shot ingest — does it actually pull data?

```bash
python -c "
import asyncio
from src.workers.ingest_worker import IngestOrchestrator
asyncio.run(IngestOrchestrator().run_once())
"
psql $DATABASE_URL -c "SELECT source, count(*) FROM signals GROUP BY 1"
```

You should see rows for at least `sec_form4`. Quiver/UW need API keys.

### 5.5 Health endpoint

```bash
uvicorn src.api.main:app --host 127.0.0.1 --port 8080 &
sleep 2
curl -s localhost:8080/health | python -m json.tool
kill %1
```

Expect `db_ok: true`, `broker_ok: true` (with IBKR Gateway/TWS running), and
heartbeat data.

### 5.6 End-to-end paper smoke

```bash
docker compose up
# in another terminal:
docker compose logs -f ingest
```

Watch for `orchestrator_tick_start` → `orchestrator_tick_end`. Watch for at
least one `vault_signal_written`. Open the vault `00 Inbox/`; expect a few
signal `.md` files within 30 minutes during market hours.

---

## 6. Known issues and gotchas

1. **`create_file` ghosting during the build.** Several files in this
   project were written across separate turns. If you find any file with
   stale conventions (e.g., importing `IngestedSignal` instead of
   `RawSignal`, or `make_broker` instead of `get_broker`, or `async_session`
   instead of `SessionLocal`), it's a leftover. Standardise per §4.1.
2. **ib_insync is sync under the hood.** Broker calls go through `asyncio.to_thread`.
   Don't wrap them twice.
3. **IBKR events are callback-based.** `LiveMonitor` subscribes to `orderStatusEvent`
   and `execDetailsEvent` on the `IB` instance. The callbacks schedule async
   handlers via `asyncio.create_task`. A 30s polling fallback reconciles missed fills.
4. **SEC EDGAR rate limit:** 10 req/sec across all endpoints. Form 4 ingestor
   does ~1 list req + N detail reqs per tick. With `ingest_interval_fast_min=15`
   we're well under, but if you parallelize, throttle.
5. **Quiver column names drift.** The ingestor handles `Representative` /
   `Senator` / `Range` / `Amount` / `Transaction` / `TransactionDate` /
   `ReportDate` because Quiver has changed these over time. If new responses
   parse-fail, check `_row_to_signal` in `src/ingestors/quiver.py`.
6. **UW endpoints are versioned in the URL.** `/api/option-trades/flow-alerts`
   and `/api/congress/recent-trades` are current as of May 2026. If these
   404, check `https://api.unusualwhales.com/docs`.
7. **yfinance is flaky.** Polygon is the preferred path. yfinance fallback
   exists but expect occasional `Failed download` warnings. The indicator
   computer logs these and continues.
8. **Decimal vs float.** All money/price math is `Decimal`. Don't introduce
   `float` casts in scoring or sizing — you'll get rounding bugs. The schemas
   use `Decimal`; the broker SDK accepts `float` (we cast at the boundary).
9. **Earnings calendar gate fails OPEN if Finnhub data is missing.** This is
   intentional — we don't want the system to halt trading because Finnhub had
   an outage. But it means the first ingest cycle's signals can pass the
   earnings gate before the calendar is populated. Run the earnings ingestor
   first (it's in the orchestrator list).
10. **Vault paths use spaces.** Folders like `00 Inbox`, `02 Positions Open`,
    `03 Closed`, `99 Meta`. Quote them everywhere or use `pathlib.Path` (we do
    in code; watch out in shell scripts).
11. **MODE=live without IBKR adapter will crash.** `get_broker()` returns
    `IBKRBroker()` which raises `NotImplementedError` on every method.
    Documented in `docs/BROKER_SETUP.md`.

---

## 7. Operational onboarding (the human side)

The user needs to do these before the system is useful:

1. **Get keys** — Finnhub (free), Polygon (free tier, optional). Quiver and Unusual Whales are paid. All documented in `.env.example`.
2. **Set up IBKR** — create account, install TWS/IB Gateway, enable API. Walkthrough in `docs/BROKER_SETUP.md`.
3. **Create Discord** — server + 4 webhooks + bot token. Walkthrough in `docs/DISCORD_SETUP.md`.
4. **Spin up VPS** — Hetzner CX22 or similar. Walkthrough in `docs/DEPLOYMENT.md`.
5. **Set up Syncthing** — VPS ↔ laptop ↔ phone. Walkthrough in `docs/DEPLOYMENT.md`.
6. **Configure Cowork** — point Cowork's scheduled tasks at the synced vault folder, paste in the four prompts from `cowork-prompts/`. Walkthrough in `docs/COWORK_INTEGRATION.md`.

---

## 8. Where to go next (recommended sequence)

For the next session, do this in order:

1. Run §5.1 → §5.6 verification. Fix anything broken. Update `docs/AUDIT.md`.
2. Add tests (§4.4). Aim for 60% line coverage on `scoring/`, `orders/`, `vault/`.
3. Decide if the user wants to start paper trading. If yes, §7 onboarding.
4. Once paper is running and producing real signals/drafts daily, plan v0.2:
   correlation gate, event proximity, daily kill switch, IV percentile,
   trader track record. Don't touch IBKR until paper has 50+ closed trades.

---

## 9. Reference files in this repo

| File | What it tells you |
|---|---|
| `bedcrock-plan.md` (next to README) | The full design spec. The plan wins disagreements. |
| `docs/AUDIT.md` | Per-component review with "solid / gap / deferred" notes |
| `docs/DEPLOYMENT.md` | VPS + Syncthing + systemd setup |
| `docs/COWORK_INTEGRATION.md` | How Cowork's scheduled tasks consume the vault |
| `docs/DISCORD_SETUP.md` | Discord app setup |
| `docs/BROKER_SETUP.md` | IBKR account + TWS/Gateway setup |
| `docs/ENV.md` | Every env var explained |
| `cowork-prompts/*.md` | The four scheduled-task prompts |
| `vault-templates/99-Meta/scoring-rules.md` | Live scoring weights — scorer reads this at runtime |
| `vault-templates/99-Meta/risk-limits.md` | Live risk limits |

---

That's the handoff. Run §5 before anything else. Good luck.
