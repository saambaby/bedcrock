---
title: Bedcrock — Build & Migration Plan v2
status: draft
phase: paper-1
created: 2026-05-10
supersedes: bedcrock-plan.md (v1, 2026-05-03)
tags: [trading/system, plan, obsidian]
---

# Bedcrock v2

This is a **delta on v1**. Read [`bedcrock-plan.md`](bedcrock-plan.md) first — v2 only documents what changes. Architecture, vault layout, Cowork prompts, phase model, frontmatter schemas, and the Discord/broker/inbox-then-process invariants are unchanged from v1 unless explicitly called out below.

> **Why v2 exists:** the [2026-05-10 audit](docs/AUDIT_2026-05-10.md) surfaced 6 blockers in v0.1 code (3 real bugs, 3 dependency/config issues), and a parallel research pass on a competing "Proxy Bot" design surfaced 4 transferable ideas worth folding in. v2 is the consolidated forward-going spec.

---

## Changelog v1 → v2

**Bug fixes (apply to v0.1 code immediately):**

| # | Change | Source |
|---|---|---|
| F1 | `ib_insync` → `ib_async==2.1.0` | Audit §3.1 |
| F2 | Bracket stop & take-profit children: `tif="GTC"`, `outsideRth=True` | Audit §3.2 |
| F3 | Idempotency check on `_on_entry_fill` + `UNIQUE(Position.broker_order_id)` | Audit §3.3 |
| F4 | `_reconcile_against_broker` on `LiveMonitor.start()` | Audit §3.4 |
| F5 | Wire `daily_pnl_pct` end-to-end → `daily_kill_switch` actually trips | Audit §3.5 |
| F6 | Connection retry with exponential backoff + IBC + nightly-logout docs | Audit §3.6 |

**New invariants (additions to v1 §2):**

7. **Broker truth wins on conflict.** On any startup or post-disconnect reconnect, IBKR's view of positions and open orders is the source of truth; the DB is repaired to match (with an audit-log entry per repair).
8. **Stops are GTC by construction.** No code path may submit a child order with `tif != "GTC"`. A reconciler audit re-issues any non-conforming order found on the wire.
9. **Mode and port are coupled.** `MODE=paper` requires `IBKR_PORT ∈ {4002, 7497}`; `MODE=live` requires `{4001, 7496}`. Mismatched config refuses to boot.

**New components (ported from Proxy Bot research):**

| # | Component | Rationale | Section |
|---|---|---|---|
| N1 | Heavy-movement ingestor (volume + 52w-high + gap) | George & Hwang 2004 alpha; fills bedcrock's gap as a corroboration source | §V2.5 |
| N2 | Sector-correlation gate (concrete, not stub) | v1 §10 mandated, never implemented; bedcrock universe clusters in defense/biotech | §V2.6 |
| N3 | Half-Kelly per-position size cap (5%) | Defends against pathological tight-stop sizing; complements existing 1%-risk rule | §V2.7 |
| N4 | Mini-backtester for scoring-rule evaluation | Lets v0.2 weight tweaks be evaluated without burning paper-trade budget | §V2.8 |

**Explicitly NOT ported from Proxy Bot (rejected):**

- Inline `anthropic` SDK call per signal — breaks the Cowork-via-vault decoupling, dilutes the vault-as-source-of-truth invariant, and Cowork's intraday cadence (11/14/16:30 ET) already covers the off-morning case.
- Tier-2 software hard-stop monitor — bedcrock's broker-side-OCO-only design is *safer* (no double-sell race possible). Don't add complexity that has to be defended against itself.
- Convergence-multiplier scoring — bedcrock's additive 9-component scorer is more flexible than a 3-source convergence model.
- SQLite, Telegram, ARK-as-primary-signal — covered in audit §4.

---

## V2.1 — `ib_async` migration (F1)

**Required changes:**

```diff
# pyproject.toml
- "ib-insync>=0.9.86",
+ "ib_async==2.1.0",
```

```diff
# src/broker/ibkr.py
- from ib_insync import IB, Stock, Trade
+ from ib_async import IB, Stock, Trade
```

```diff
# src/orders/monitor.py
  async def _keep_alive():
-     """Keep ib_insync event loop running."""
-     if isinstance(self._broker, IBKRBroker):
-         ib = self._broker._ib
-         while not self._stopped and ib.isConnected():
-             ib.sleep(1)
-             await asyncio.sleep(0.1)
+     """ib_async is pure asyncio — no event-loop bridging needed.
+     Just keep the task alive so cancellation is observed."""
+     while not self._stopped and self._broker._ib.isConnected():
+         await asyncio.sleep(5)
```

Replace `await asyncio.to_thread(ib.X, ...)` with native async variants where `ib_async` provides them:

| Before (ib_insync, blocking) | After (ib_async, native async) |
|---|---|
| `await asyncio.to_thread(ib.accountSummary, account)` | `await ib.accountSummaryAsync(account)` |
| `await asyncio.to_thread(ib.qualifyContracts, contract)` | `await ib.qualifyContractsAsync(contract)` |
| `await asyncio.to_thread(ib.reqTickers, contract)` | `await ib.reqTickersAsync(contract)` |
| `await asyncio.to_thread(ib.fills)` | `await ib.reqExecutionsAsync()` |
| `await asyncio.to_thread(ib.placeOrder, contract, order)` | `ib.placeOrder(contract, order)` (already non-blocking — returns Trade) |

**Acceptance:** 48 hours of paper-mode operation under v2 with zero `_keep_alive` log messages, no `RuntimeError: This event loop is already running`, and all existing `tests/test_orders.py` cases pass.

---

## V2.2 — Bracket child TIF (F2)

**Required changes** in `src/broker/ibkr.py::submit_bracket`:

```python
async def submit_bracket(self, spec) -> BrokerOrder:
    ...
    bracket = ib.bracketOrder(
        action=ib_action,
        quantity=quantity,
        limitPrice=entry_limit,
        takeProfitPrice=target_price,
        stopLossPrice=stop_price,
    )

    # bracket = [parent, take_profit, stop_loss]
    parent, take_profit, stop_loss = bracket

    # Parent: DAY (entry zone is a same-day decision per plan v1 §6.2)
    parent.tif = "DAY"
    parent.outsideRth = False

    # Children MUST be GTC + outsideRth — otherwise stop expires at session
    # close and overnight gap risk has no protection.
    for child in (take_profit, stop_loss):
        child.tif = "GTC"
        child.outsideRth = True

    if client_order_id:
        parent.orderRef = client_order_id

    trades = []
    for order in bracket:
        trade = ib.placeOrder(contract, order)
        trades.append(trade)
    ...
```

**Reconciler audit** — new module `src/safety/reconciler.py`:

```python
from src.broker.ibkr import IBKRBroker
from src.discord_bot.webhooks import post_system_health
from src.logging_config import get_logger

logger = get_logger(__name__)


async def audit_open_order_tifs(broker: IBKRBroker) -> list[int]:
    """Walk all open orders. Re-issue any child with tif != 'GTC'.
    Returns the list of order IDs that were repaired."""
    ib = broker._ib
    repaired: list[int] = []
    for trade in ib.openTrades():
        order = trade.order
        if not order.parentId:
            continue  # parent orders may legitimately be DAY
        if order.orderType not in ("STP", "STP LMT", "TRAIL", "LMT"):
            continue
        if order.tif == "GTC":
            continue

        logger.error(
            "child_order_not_gtc",
            order_id=order.orderId,
            tif=order.tif,
            parent_id=order.parentId,
            order_type=order.orderType,
        )
        # Cancel + reissue with GTC + outsideRth
        ib.cancelOrder(order)
        order.tif = "GTC"
        order.outsideRth = True
        order.orderId = 0  # force IBKR to assign a new ID
        ib.placeOrder(trade.contract, order)
        repaired.append(order.orderId)
        await post_system_health(
            title=f"Repaired non-GTC child order on {trade.contract.symbol}",
            body=f"Was tif={order.tif}, parent={order.parentId}. Re-issued as GTC.",
            ok=False,
        )
    return repaired
```

Wire into `monitor_worker._poll` (every 30s) alongside existing `_reconcile_orders`.

**Tests required** in `tests/test_orders.py`:

```python
def test_bracket_children_are_gtc(mock_ib):
    broker = IBKRBroker()
    broker._ib = mock_ib
    spec = make_test_spec(ticker="AAPL", entry=100, stop=90, target=120, qty=10)
    asyncio.run(broker.submit_bracket(spec))
    placed = mock_ib.placed_orders
    assert placed[0].tif == "DAY"           # parent
    assert placed[1].tif == "GTC"           # take-profit
    assert placed[1].outsideRth is True
    assert placed[2].tif == "GTC"           # stop-loss
    assert placed[2].outsideRth is True
```

---

## V2.3 — Double-Position-row idempotency (F3)

**Schema migration** (alembic `0002_position_unique_broker_order_id.py`):

```python
def upgrade():
    # Drop any existing duplicates first (manual or via dedupe query)
    op.execute("""
        DELETE FROM positions p1
        USING positions p2
        WHERE p1.id > p2.id
          AND p1.broker_order_id = p2.broker_order_id
          AND p1.broker_order_id IS NOT NULL
    """)
    op.create_unique_constraint(
        "uq_positions_broker_order_id",
        "positions",
        ["broker_order_id"],
    )

def downgrade():
    op.drop_constraint("uq_positions_broker_order_id", "positions")
```

**Code changes** in `src/orders/monitor.py::_on_entry_fill`:

```python
async def _on_entry_fill(self, db, draft, broker_order_id, ticker, filled_qty, filled_avg):
    if draft is None:
        logger.warning("entry_fill_no_draft", ticker=ticker)
        return

    # Idempotency — has WS handler or polling reconciler already created this Position?
    existing = (await db.execute(
        select(Position).where(Position.broker_order_id == broker_order_id)
    )).scalar_one_or_none()
    if existing is not None:
        logger.info("entry_fill_already_processed", broker_order_id=broker_order_id)
        if draft.status != OrderStatus.FILLED:
            draft.status = OrderStatus.FILLED
            await db.commit()
        return

    # ... existing creation logic ...
```

**Code changes** in `_reconcile_orders`:

```python
async def _reconcile_orders(self, db):
    stmt = select(DraftOrder).where(DraftOrder.status == OrderStatus.SENT)
    sent = (await db.execute(stmt)).scalars().all()
    for draft in sent:
        if not draft.broker_order_id:
            continue

        # Skip if Position already exists — WS handler beat us to it
        already_filled = (await db.execute(
            select(Position.id).where(Position.broker_order_id == draft.broker_order_id)
        )).scalar_one_or_none()
        if already_filled:
            draft.status = OrderStatus.FILLED  # repair drift
            await db.commit()
            continue

        try:
            bo = await self._broker.get_order(draft.broker_order_id)
            if bo.state == BrokerOrderState.FILLED and bo.filled_avg_price:
                await self._on_entry_fill(...)
        except Exception as e:
            logger.debug("reconcile_skip", draft_id=str(draft.id), error=str(e))
```

**Tests required:**

```python
async def test_double_fill_idempotency(test_db, fake_draft):
    monitor = LiveMonitor()
    await monitor._on_entry_fill(test_db, fake_draft, "ibkr_123", "AAPL", Decimal(10), Decimal(150))
    await monitor._on_entry_fill(test_db, fake_draft, "ibkr_123", "AAPL", Decimal(10), Decimal(150))
    rows = (await test_db.execute(select(Position).where(Position.broker_order_id == "ibkr_123"))).scalars().all()
    assert len(rows) == 1
```

---

## V2.4 — Startup reconciliation against IBKR (F4 + invariant 7)

**New method** in `src/orders/monitor.py::LiveMonitor`:

```python
async def _reconcile_against_broker(self, db: AsyncSession) -> None:
    """On startup: any IBKR position not in our DB → orphan alert.
    Any DB position not in IBKR → mark closed-externally."""
    ib = self._broker._ib
    if not ib.isConnected():
        return

    # Reqest positions (forces a fresh refresh, doesn't trust cache)
    await ib.reqPositionsAsync()
    ibkr = {p.contract.symbol: p for p in ib.positions() if p.position != 0}

    db_open = {
        p.ticker: p for p in
        (await db.execute(
            select(Position).where(
                Position.mode == settings.mode,
                Position.status == PositionStatus.OPEN,
            )
        )).scalars().all()
    }

    # Orphans in IBKR (entered manually, or DB row was lost)
    for sym, ibp in ibkr.items():
        if sym not in db_open:
            logger.error("orphan_ibkr_position", symbol=sym, qty=ibp.position)
            db.add(AuditLog(
                actor="reconciler",
                action="orphan_ibkr_detected",
                target_kind="position",
                target_id=sym,
                details={"qty": ibp.position, "avg_cost": str(ibp.avgCost)},
            ))
            await post_position_alert(
                title=f"⚠️ ORPHAN: {sym}",
                description=f"IBKR shows {ibp.position} @ ${ibp.avgCost} — no DB record.",
                color=0xFBBF24,
            )

    # Stale in DB (closed externally, e.g. via IBKR mobile app)
    for sym, dbp in db_open.items():
        if sym not in ibkr:
            logger.warning("stale_db_position", symbol=sym, db_id=str(dbp.id))
            dbp.status = PositionStatus.CLOSED
            dbp.close_reason = CloseReason.EXTERNAL  # add to enum
            dbp.exit_at = datetime.now(UTC)
            db.add(AuditLog(
                actor="reconciler",
                action="closed_externally",
                target_kind="position",
                target_id=str(dbp.id),
                details={"ticker": sym},
            ))

    await db.commit()
```

**Wire into** `LiveMonitor.start()` after `await self._broker.connect()`, before `_poll` starts.

**Add to enum** in `src/db/models.py::CloseReason`:

```python
class CloseReason(str, Enum):
    STOP_HIT = "stop_hit"
    TARGET_HIT = "target_hit"
    SIGNAL_EXIT = "signal_exit"
    DISCRETIONARY = "discretionary"
    EXTERNAL = "external"          # ← NEW: closed outside the bot (mobile app, manual)
```

---

## V2.5 — Heavy-movement ingestor (N1)

**Why this exists:** bedcrock's signal universe (politicians, insiders, options flow) is information-driven and *slow*. Adding a real-time market-action layer as a **corroboration source** (not a primary) sharpens the cluster score on signals that already passed the fundamental gate. George & Hwang (*Journal of Finance*, 2004) is the canonical paper: 52-week-high breakouts return +0.65%/month with no long-run reversal. That's a free corroboration channel for any insider buy or politician trade landing in the same window.

**Design constraint:** this is *not* a primary signal source. It must never trigger a draft order on its own. It only adds points to the `flow_corroboration` slot of an existing scored signal. This preserves bedcrock's "disclosure-driven thesis" invariant.

**New ingestor** at `src/ingestors/heavy_movement.py`:

```python
"""Heavy-movement detector. Runs every 5 minutes during market hours.

Computes for each ticker in the watchlist (built from open positions +
01 Watchlist/ + recent #high-score candidates):
  - volume_spike: today's volume vs 20-day average
  - gap_pct: today's open vs prior close
  - is_52w_breakout: today's high >= prior 52-week high

Writes a Signal row with source=MARKET_MOVEMENT, action inferred from
gap direction. The scorer treats MARKET_MOVEMENT signals as
corroboration for any existing signal on the same ticker in the
last 14 days; they cannot trigger drafts on their own.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.config import settings
from src.db.models import Action, Signal, SignalSource, SignalStatus
from src.ingestors.base import IngestorBase
from src.ingestors.ohlcv import get_recent_bars  # existing
from src.logging_config import get_logger

logger = get_logger(__name__)

VOLUME_SPIKE_THRESHOLD = 3.0       # 3x 20-day average
GAP_THRESHOLD = 0.05               # 5%
GAP_DOWN_KILL = -0.10              # ignore -10% or worse (panic, not opportunity)


class HeavyMovementIngestor(IngestorBase):
    name = "heavy_movement"
    interval_seconds = 300         # every 5 minutes during market hours

    def _is_market_hours(self) -> bool:
        # standard US market hours check, ET-anchored
        ...

    async def _build_watchlist(self, db) -> set[str]:
        """Tickers worth monitoring: open positions + recent high-score signals."""
        from sqlalchemy import select
        from src.db.models import Position, PositionStatus
        open_pos = (await db.execute(
            select(Position.ticker).where(Position.status == PositionStatus.OPEN)
        )).scalars().all()
        recent_high = (await db.execute(
            select(Signal.ticker).where(
                Signal.disclosed_at >= datetime.now(UTC) - timedelta(days=14),
                Signal.score >= 5.0,
            )
        )).scalars().all()
        return set(open_pos) | set(recent_high)

    async def run_once(self, db) -> int:
        if not self._is_market_hours():
            return 0
        watchlist = await self._build_watchlist(db)
        if not watchlist:
            return 0

        emitted = 0
        for ticker in watchlist:
            bars = await get_recent_bars(ticker, days=22)
            if not bars or len(bars) < 21:
                continue

            today = bars[-1]
            prior_20 = bars[-21:-1]
            avg_vol = sum(b.volume for b in prior_20) / 20
            prior_close = prior_20[-1].close
            high_52w = max(b.high for b in bars[-min(252, len(bars)):])

            volume_ratio = today.volume / avg_vol if avg_vol else 0
            gap_pct = (today.open / prior_close - 1) if prior_close else 0
            is_breakout = today.high >= high_52w

            # Hard exclusion — major gap down is panic, not corroboration
            if gap_pct <= GAP_DOWN_KILL:
                continue

            # Need at least one of the three triggers
            triggers = []
            if volume_ratio >= VOLUME_SPIKE_THRESHOLD:
                triggers.append(f"vol{volume_ratio:.1f}x")
            if abs(gap_pct) >= GAP_THRESHOLD:
                triggers.append(f"gap{gap_pct*100:+.1f}%")
            if is_breakout:
                triggers.append("52w_high")
            if not triggers:
                continue

            # Direction inferred from gap; breakout-only defaults to BUY
            action = Action.BUY if gap_pct >= 0 else Action.SELL

            sig = Signal(
                source=SignalSource.MARKET_MOVEMENT,
                source_external_id=f"{ticker}-{today.date.isoformat()}-{'-'.join(triggers)}",
                ticker=ticker,
                action=action,
                disclosed_at=datetime.now(UTC),
                trade_date_low=today.date,
                trade_date_high=today.date,
                size_low_usd=None,
                size_high_usd=None,
                status=SignalStatus.NEW,
                raw_payload={
                    "volume_ratio": volume_ratio,
                    "gap_pct": gap_pct,
                    "is_52w_breakout": is_breakout,
                    "triggers": triggers,
                },
            )
            db.add(sig)
            emitted += 1

        await db.commit()
        return emitted
```

**Add to enum** in `src/db/models.py::SignalSource`:

```python
class SignalSource(str, Enum):
    SEC_FORM4 = "sec_form4"
    SEC_13F = "sec_13f"
    QUIVER_CONGRESS = "quiver_congress"
    UW_FLOW = "uw_flow"
    UW_CONGRESS = "uw_congress"
    PUBLIC_STATEMENT = "public_statement"
    MARKET_MOVEMENT = "market_movement"     # ← NEW
```

**Update scorer** in `src/scoring/scorer.py` — `MARKET_MOVEMENT` cannot drive a draft on its own:

```python
async def score(self, signal, prior_signals_30d, indicators, trader_track_record=None):
    b = ScoreBreakdown()

    # Hard rule — market-movement signals never score above 0 in isolation.
    # They only count as corroboration for an existing fundamental signal.
    if signal.source == SignalSource.MARKET_MOVEMENT:
        # Was there any non-MARKET_MOVEMENT signal on this ticker in last 14d?
        cutoff = datetime.now(UTC) - timedelta(days=14)
        has_fundamental = any(
            s.source != SignalSource.MARKET_MOVEMENT and s.disclosed_at >= cutoff
            for s in prior_signals_30d
        )
        if not has_fundamental:
            return 0.0, b
        # If yes, treat as flow_corroboration with the same weight as UW flow
        b.flow_corroboration_market = self.weights["options_flow_corroboration"]
        return b.total, b

    # ... existing scoring logic for fundamental signals ...
```

**Cluster scoring update** — `_score_cluster` now distinguishes fundamental sources from corroboration:

```python
def _score_cluster(self, signal, prior):
    same_dir = [
        s for s in prior
        if s.action == signal.action
        and s.ticker == signal.ticker
        and s.source != SignalSource.MARKET_MOVEMENT  # don't double-count corroboration
    ]
    sources = {s.source for s in same_dir} - {signal.source}
    traders = {s.trader_id for s in same_dir if s.trader_id is not None}
    independent = len(sources) + max(0, len(traders) - len(sources))

    base = independent * self.weights["cluster_per_extra_source"]

    # Bonus: any MARKET_MOVEMENT corroboration in last 14d?
    has_movement = any(
        s.source == SignalSource.MARKET_MOVEMENT
        and s.action == signal.action
        and (datetime.now(UTC) - s.disclosed_at).days <= 14
        for s in prior
    )
    if has_movement:
        base += 0.5  # half a cluster point — additive, not multiplicative

    return min(base, self.weights["cluster_max"])
```

**Wire into worker** in `src/workers/ingest_worker.py` — register alongside existing ingestors via `IngestorRegistry`.

---

## V2.6 — Sector-correlation gate (N2)

**Why this exists:** v1 §10 specified a "max correlated exposure 0.7 portfolio beta" risk limit but the gate was stubbed (`gates.py:52` returns unconditional `blocked=False`). Bedcrock's universe is structurally cluster-prone — politicians on Armed Services committees buy defense; biotech insiders correlate with FDA cycles. Without this gate, "5 positions" can really be one bet.

**Implementation** in `src/scoring/gates.py`:

```python
SECTOR_ETF_MAP = {
    # Defense
    "LMT": "ITA", "RTX": "ITA", "NOC": "ITA", "GD": "ITA", "BA": "ITA",
    # Biotech
    "MRNA": "XBI", "BNTX": "XBI", "CRSP": "XBI", "VRTX": "XBI", "REGN": "XBI",
    # Mega-cap tech
    "NVDA": "XLK", "AAPL": "XLK", "MSFT": "XLK", "GOOGL": "XLK", "META": "XLK",
    "AMZN": "XLY", "TSLA": "XLY",
    # Energy
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE",
    # Financials
    "JPM": "XLF", "BAC": "XLF", "GS": "XLF",
    # ... extend as needed; fall back to "OTHER" for unmapped
}

SECTOR_CONCENTRATION_LIMIT = 0.25   # max 25% of equity in one sector

async def _gate_correlation(
    self, db: AsyncSession, signal: RawSignal, indicators: IndicatorSnapshot | None
) -> GateResult:
    if indicators is None or indicators.last_price is None:
        return GateResult(gate=GateName.CORRELATION, blocked=False)  # fail-open if no data

    proposed_sector = SECTOR_ETF_MAP.get(signal.ticker.upper(), "OTHER")

    # Sum existing exposure by sector
    open_positions = (await db.execute(
        select(Position).where(
            Position.mode == settings.mode,
            Position.status == PositionStatus.OPEN,
        )
    )).scalars().all()

    broker = get_broker()
    try:
        await broker.connect()
        account = await broker.get_account()
    finally:
        await broker.disconnect()

    if account.equity <= 0:
        return GateResult(gate=GateName.CORRELATION, blocked=False)

    sector_exposure: dict[str, Decimal] = {}
    for pos in open_positions:
        sec = SECTOR_ETF_MAP.get(pos.ticker, "OTHER")
        sector_exposure[sec] = sector_exposure.get(sec, Decimal(0)) + (
            pos.entry_price * pos.quantity
        )

    # Add the proposed position's worst-case exposure (use indicators.last_price * estimated qty)
    # Conservative: assume max position size (5% of equity per V2.7)
    estimated_proposed = account.equity * Decimal("0.05")
    projected = sector_exposure.get(proposed_sector, Decimal(0)) + estimated_proposed
    projected_pct = float(projected / account.equity)

    if projected_pct > SECTOR_CONCENTRATION_LIMIT:
        return GateResult(
            gate=GateName.CORRELATION,
            blocked=True,
            reason=(
                f"Sector {proposed_sector} would be {projected_pct*100:.1f}% of equity "
                f"(limit {SECTOR_CONCENTRATION_LIMIT*100:.0f}%). "
                f"Existing: ${sector_exposure.get(proposed_sector, 0):,.0f}"
            ),
            overrideable=True,  # human can override with thesis justification
        )
    return GateResult(gate=GateName.CORRELATION, blocked=False)
```

**Wire into** `GateEvaluator.evaluate` — replace the `GateName.CORRELATION` stub.

---

## V2.7 — Half-Kelly per-position size cap (N3)

**Why this exists:** v1's `OrderBuilder` uses pure risk-based sizing — `qty = (equity × risk_pct) / |entry - stop|`. With a tight stop (e.g. ATR is small, entry close to support), this can produce a position that's 20%+ of equity. The risk *per trade* is still 1%, but the position concentration becomes unsafe — a halt or earnings shock crosses the stop and you eat the *full* position size as the loss, not the planned 1%.

**Fix** — add a hard cap *after* the risk-based qty is computed:

```python
async def build_draft(...):
    ...
    risk_pct = Decimal(str(settings.risk_per_trade_pct))
    dollar_risk = account.equity * risk_pct / Decimal("100")
    qty_by_risk = (dollar_risk / risk).quantize(Decimal("1"))

    # Half-Kelly cap: never more than 5% of equity in one position,
    # regardless of how tight the stop is.
    max_position_pct = Decimal(str(settings.risk_max_position_size_pct))  # 0.05 default
    qty_by_concentration = (
        (account.equity * max_position_pct) / entry
    ).quantize(Decimal("1"))

    quantity = min(qty_by_risk, qty_by_concentration)

    if quantity <= 0:
        logger.info("size_zero", ticker=ticker)
        return None

    if quantity == qty_by_concentration and qty_by_concentration < qty_by_risk:
        logger.info(
            "position_size_capped_by_concentration",
            ticker=ticker,
            qty_by_risk=str(qty_by_risk),
            qty_by_concentration=str(qty_by_concentration),
            cap_pct=float(max_position_pct),
        )
    ...
```

**Add to** `src/config.py::Settings`:

```python
# --- Risk limits ---
risk_per_trade_pct: float = 1.0
risk_max_position_size_pct: float = 0.05   # NEW: half-Kelly cap (5% of equity)
risk_max_open_positions: int = 8
...
```

**Update v1 §10 risk table** — Live Phase 1 already says "Per-position size: 3% of equity"; that's tighter than the default. Set `risk_max_position_size_pct=0.03` for Live Phase 1 via `99 Meta/risk-limits.md` overrides.

---

## V2.8 — Mini-backtester for scoring-rule changes (N4)

**Why this exists:** v1's philosophy is "paper trading IS the backtest" — you only learn from real (paper) closed trades. That's defensible for a system whose signal universe (politician trades, insider buys) is hard to backtest faithfully. But the weekly-synthesis adoption flow proposes scoring-weight changes based on closed paper trades. With 50 trades, a 1-point weight change on `cluster_per_extra_source` is statistically indistinguishable from noise.

**Solution:** a *minimal* historical-replay tool that re-scores past Signals (already in the DB) under a proposed weight set, simulates entries/exits at OHLCV boundaries, and reports the Sharpe delta. Not a full backtest framework — just enough to evaluate weight changes before adoption.

**Structure** — new module `src/backtest/replay.py`:

```python
"""Replay historical signals under a proposed scoring rule set.

Inputs:
  - signals_query: SQLAlchemy filter (e.g. "all signals from last 90 days
    with score >= 5.0 under the OLD weights")
  - proposed_weights: dict to override current weights
  - sim_config: entry rule (e.g. "T+1 OPEN"), exit rule (e.g. "10% stop or
    1.5R target", same as live), slippage_bps, commission_per_share

Outputs:
  - per-signal table: re-score, would-have-entered, simulated PnL
  - aggregate report: Sharpe, win rate, profit factor, max DD, total return
  - Sharpe delta vs. baseline (current weights, same signal set)

Hard limits to prevent overfitting:
  - REQUIRES out-of-sample window (last 30 days of signals reserved)
  - Reports both in-sample and out-of-sample metrics
  - Refuses to recommend a weight change if out-of-sample Sharpe ≤ baseline
"""

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Signal
from src.scoring.scorer import Scorer
from src.ingestors.ohlcv import get_recent_bars


@dataclass
class SimTrade:
    ticker: str
    signal_date: date
    entry_date: date
    entry_price: Decimal
    exit_date: date
    exit_price: Decimal
    qty: int
    pnl_pct: float
    exit_reason: str  # stop | target | timeout


@dataclass
class ReplayReport:
    n_signals_in_scope: int
    n_signals_above_threshold: int
    n_trades_simulated: int
    in_sample_sharpe: float
    out_of_sample_sharpe: float
    win_rate: float
    profit_factor: float
    total_return_pct: float
    sharpe_delta_vs_baseline: float
    recommendation: str  # ADOPT | REJECT | INCONCLUSIVE


async def replay(
    db: AsyncSession,
    proposed_weights: dict,
    *,
    score_threshold: float = 7.0,
    stop_loss_pct: float = 0.10,
    target_r_multiple: float = 1.5,
    holding_days_max: int = 30,
    slippage_bps: float = 10,
    out_of_sample_days: int = 30,
) -> ReplayReport:
    """See module docstring."""
    # Pull signals
    cutoff = date.today() - timedelta(days=180)
    out_sample_start = date.today() - timedelta(days=out_of_sample_days)

    stmt = select(Signal).where(
        Signal.disclosed_at >= cutoff,
        Signal.gate_blocked.is_(False),
    )
    signals = (await db.execute(stmt)).scalars().all()

    proposed_scorer = Scorer(weights=proposed_weights)
    baseline_scorer = Scorer()  # current weights

    in_sample_trades: list[SimTrade] = []
    out_sample_trades: list[SimTrade] = []
    baseline_trades: list[SimTrade] = []

    for sig in signals:
        # Re-score under both
        proposed_score, _ = await _score_signal(proposed_scorer, sig, db)
        baseline_score, _ = await _score_signal(baseline_scorer, sig, db)

        # Did the proposed weights cross the threshold where the baseline didn't?
        # Or vice versa? Either way, simulate entry on T+1 open.
        for score, bucket in [
            (proposed_score, in_sample_trades if sig.disclosed_at.date() < out_sample_start else out_sample_trades),
            (baseline_score, baseline_trades),
        ]:
            if score < score_threshold:
                continue
            trade = await _simulate_trade(
                sig, score, stop_loss_pct, target_r_multiple,
                holding_days_max, slippage_bps,
            )
            if trade:
                bucket.append(trade)

    in_sharpe = _sharpe([t.pnl_pct for t in in_sample_trades])
    oos_sharpe = _sharpe([t.pnl_pct for t in out_sample_trades])
    baseline_sharpe = _sharpe([t.pnl_pct for t in baseline_trades])

    delta = oos_sharpe - baseline_sharpe
    rec = "ADOPT" if (oos_sharpe > baseline_sharpe and oos_sharpe > 1.0) else (
        "REJECT" if oos_sharpe < baseline_sharpe else "INCONCLUSIVE"
    )

    return ReplayReport(
        n_signals_in_scope=len(signals),
        n_signals_above_threshold=len(in_sample_trades) + len(out_sample_trades),
        n_trades_simulated=len(in_sample_trades) + len(out_sample_trades),
        in_sample_sharpe=in_sharpe,
        out_of_sample_sharpe=oos_sharpe,
        win_rate=sum(1 for t in out_sample_trades if t.pnl_pct > 0) / max(1, len(out_sample_trades)),
        profit_factor=_profit_factor(out_sample_trades),
        total_return_pct=sum(t.pnl_pct for t in out_sample_trades),
        sharpe_delta_vs_baseline=delta,
        recommendation=rec,
    )


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 5:
        return 0.0
    mean = statistics.mean(returns)
    sd = statistics.stdev(returns)
    if sd == 0:
        return 0.0
    # Annualized assuming ~50 trades/yr; adjust for actual cadence in v0.3
    return (mean / sd) * (50 ** 0.5)


def _profit_factor(trades: list[SimTrade]) -> float:
    wins = sum(t.pnl_pct for t in trades if t.pnl_pct > 0)
    losses = abs(sum(t.pnl_pct for t in trades if t.pnl_pct < 0))
    return wins / losses if losses else float("inf")


async def _simulate_trade(sig, score, stop_pct, r_mult, max_days, slip_bps):
    """T+1 OPEN entry; OCO stop at stop_pct below entry; target at r_mult * stop_distance.
    Exit at first hit or timeout."""
    bars = await get_recent_bars(sig.ticker, days=max_days + 5, end_date=sig.disclosed_at + timedelta(days=max_days+5))
    if not bars or len(bars) < 2:
        return None

    # Find T+1 bar
    t_plus_1 = next((b for b in bars if b.date > sig.disclosed_at.date()), None)
    if not t_plus_1:
        return None

    entry = t_plus_1.open * (1 + slip_bps / 10000)
    stop = entry * (1 - stop_pct)
    target = entry * (1 + stop_pct * r_mult)

    for b in bars[bars.index(t_plus_1):]:
        if b.low <= stop:
            return SimTrade(
                ticker=sig.ticker, signal_date=sig.disclosed_at.date(),
                entry_date=t_plus_1.date, entry_price=Decimal(str(entry)),
                exit_date=b.date, exit_price=Decimal(str(stop)), qty=1,
                pnl_pct=-stop_pct * 100, exit_reason="stop",
            )
        if b.high >= target:
            return SimTrade(
                ticker=sig.ticker, signal_date=sig.disclosed_at.date(),
                entry_date=t_plus_1.date, entry_price=Decimal(str(entry)),
                exit_date=b.date, exit_price=Decimal(str(target)), qty=1,
                pnl_pct=stop_pct * r_mult * 100, exit_reason="target",
            )
    # Timeout — exit at last close
    last = bars[-1]
    return SimTrade(
        ticker=sig.ticker, signal_date=sig.disclosed_at.date(),
        entry_date=t_plus_1.date, entry_price=Decimal(str(entry)),
        exit_date=last.date, exit_price=Decimal(str(last.close)), qty=1,
        pnl_pct=float((last.close / entry - 1) * 100), exit_reason="timeout",
    )
```

**Hooked into** the weekly-synthesis Cowork prompt — when a rule change is proposed in `99 Meta/scoring-rules-proposed.md`, an EOD worker job runs `replay()` against the proposal and writes the report to `06 Weekly/{date}-replay-{rule}.md`. Cowork's Sunday synthesis prompt reads that report and uses the `ADOPT/REJECT/INCONCLUSIVE` recommendation as input — but the human still confirms the change.

**Caveats** documented in the module: this replay uses *historical OHLCV*, not historical bid/ask depth. Slippage is a constant assumption. No survivorship-bias correction (yfinance bias acknowledged). It's a sanity check, not a Monte Carlo. Treat the recommendation as advisory.

---

## V2.9 — Updated configuration

**`src/config.py` additions** (incorporating F5, invariant 9, and N3):

```python
class Settings(BaseSettings):
    ...
    # --- Mode ↔ port coupling (invariant 9) ---
    @model_validator(mode="after")
    def _validate_mode_port(self) -> "Settings":
        valid_paper_ports = {4002, 7497}
        valid_live_ports = {4001, 7496}
        if self.mode == Mode.PAPER and self.ibkr_port not in valid_paper_ports:
            raise ValueError(
                f"MODE=paper requires IBKR_PORT in {valid_paper_ports}, got {self.ibkr_port}. "
                "4002 is IB Gateway paper, 7497 is TWS paper."
            )
        if self.mode == Mode.LIVE and self.ibkr_port not in valid_live_ports:
            raise ValueError(
                f"MODE=live requires IBKR_PORT in {valid_live_ports}, got {self.ibkr_port}. "
                "4001 is IB Gateway live, 7496 is TWS live."
            )
        return self

    # --- Risk additions ---
    risk_max_position_size_pct: float = 0.05  # half-Kelly cap (N3)

    # --- Heavy movement ingestor (N1) ---
    movement_volume_spike_threshold: float = 3.0
    movement_gap_threshold: float = 0.05
    movement_check_interval_seconds: int = 300

    # --- Sector correlation gate (N2) ---
    risk_sector_concentration_limit: float = 0.25
```

**`docs/ENV.md` additions:** `RISK_MAX_POSITION_SIZE_PCT`, `MOVEMENT_*`, `RISK_SECTOR_CONCENTRATION_LIMIT`.

---

## V2.10 — Updated phase gates with concrete acceptance checklists

The v1 phase model is unchanged. v2 adds **explicit acceptance checklists** between each phase — every box must tick before progressing.

### Paper Phase 1 → Paper Phase 2

- [ ] All v0.1 critical bugs fixed (F1–F6 landed in code)
- [ ] `tests/test_orders.py::test_bracket_children_are_gtc` passes
- [ ] `tests/test_orders.py::test_double_fill_idempotency` passes
- [ ] `tests/test_orders.py::test_startup_reconciles_orphan_ibkr_position` passes
- [ ] `tests/test_gates.py::test_daily_kill_switch_blocks_at_negative_threshold` passes
- [ ] `_reconcile_against_broker` has run on at least 5 startups with no false-positive alerts
- [ ] At least 10 closed paper trades
- [ ] No orphaned IBKR positions in any reconciliation run
- [ ] No duplicate Position rows in DB (verify by `SELECT broker_order_id, COUNT(*) FROM positions GROUP BY broker_order_id HAVING COUNT(*) > 1` returns empty)
- [ ] All bracket stops on the wire are GTC (verify by `audit_open_order_tifs()` returns empty list across 7 daily runs)
- [ ] IBC nightly logout survived ≥ 7 nights with auto-reconnect
- [ ] Heavy-movement ingestor (V2.5) running and emitting signals during market hours
- [ ] Sector-correlation gate (V2.6) has blocked at least one over-concentrated trade attempt

### Paper Phase 2 → Live Phase 1

- [ ] At least 90 calendar days in Paper Phase 2
- [ ] At least 50 closed paper trades
- [ ] Sharpe ratio > 1.0 over the full Phase 2 period
- [ ] **Deflated Sharpe Ratio** (Bailey & Lopez de Prado, 2014) > 0.95 — adjusts for the multiple weight tweaks
- [ ] **Profit factor > 1.5**
- [ ] Win rate stable: stdev across 4 consecutive 2-week buckets < 10pp
- [ ] Average excess return vs. SPY > 0 (computed against the SPY baseline portfolio, V2.4 of audit)
- [ ] Max drawdown < 15% of starting paper equity
- [ ] No more than 1 operational incident in last 30 days
- [ ] At least 3 signal types each with ≥ 10 closed trades
- [ ] Weekly synthesis has adopted at least 2 rule refinements via the v2.8 replay-validated flow
- [ ] At least one rule change was REJECTED by V2.8's out-of-sample test (proves the gate works)
- [ ] All four safety changes from v0.1 have been triggered at least once in paper:
   - GTC stop fired overnight
   - Idempotency check skipped a duplicate fill
   - Startup reconciler caught an orphan
   - Daily kill switch tripped (or dry-run forced it to)

### Live Phase 1 → Live Phase 2

- [ ] At least 60 calendar days in Live Phase 1
- [ ] At least 25 closed live trades
- [ ] Live Sharpe within 30% of Phase 2 paper Sharpe (otherwise the slippage model is wrong)
- [ ] Max drawdown stayed within `99 Meta/risk-limits.md` Live Phase 1 limits
- [ ] No operational incidents requiring manual broker intervention
- [ ] Paper kept running in parallel for the full 60 days (head-to-head data exists)

---

## V2.11 — Updated dependencies

```diff
# pyproject.toml
- "ib-insync>=0.9.86",
+ "ib_async==2.1.0",
```

No other dependency changes. `yfinance` stays (live indicator fallback only; if a future v0.3 backtester needs it for primary, swap to Norgate/CRSP per audit §3.7).

---

## V2.12 — Bug fixes summary table (for the migration PR)

The PR that lands v2 should contain these atomic commits in order:

| # | Commit | Files |
|---|---|---|
| 1 | `chore(deps): migrate ib_insync → ib_async` | `pyproject.toml`, `src/broker/ibkr.py`, `src/orders/monitor.py` (delete `_keep_alive` body) |
| 2 | `fix(broker): GTC + outsideRth on bracket children` | `src/broker/ibkr.py::submit_bracket` |
| 3 | `feat(safety): reconciler audits non-GTC child orders` | `src/safety/reconciler.py` (new), `src/workers/monitor_worker.py` |
| 4 | `fix(monitor): idempotency on _on_entry_fill` | `src/orders/monitor.py`, `alembic/versions/0002_*.py` |
| 5 | `feat(monitor): startup reconciliation against IBKR` | `src/orders/monitor.py`, `src/db/models.py` (CloseReason.EXTERNAL) |
| 6 | `feat(safety): mode↔port validator + connection retry` | `src/config.py`, `src/broker/ibkr.py::connect` |
| 7 | `feat(gates): wire daily_pnl_pct and trip daily kill switch` | `src/scoring/gates.py`, `src/db/models.py` (DailyState), `src/workers/monitor_worker.py` |
| 8 | `feat(scoring): half-Kelly position size cap` | `src/orders/builder.py`, `src/config.py` |
| 9 | `feat(gates): sector-correlation gate (real impl)` | `src/scoring/gates.py` |
| 10 | `feat(ingest): heavy-movement ingestor as corroboration source` | `src/ingestors/heavy_movement.py` (new), `src/db/models.py` (SignalSource.MARKET_MOVEMENT), `src/scoring/scorer.py`, `src/workers/ingest_worker.py` |
| 11 | `feat(backtest): mini-replay for scoring-rule changes` | `src/backtest/__init__.py` (new), `src/backtest/replay.py` (new), `src/workers/eod_worker.py` |
| 12 | `chore: delete duplicate scorers/payloads/breakdowns` | `src/schemas/signal.py`, `src/schemas/order.py`, dead `score_pending_signals` helper |
| 13 | `test: critical-path coverage for v2 invariants` | `tests/test_orders.py`, `tests/test_gates.py`, `tests/test_reconciler.py` (new) |
| 14 | `docs: v2 plan + AUDIT_2026-05-10 + ENV updates` | `bedcrock-plan-v2.md` (this file), `docs/ENV.md`, `docs/DEPLOYMENT.md` (IBC) |

---

## V2.13 — What v2 does NOT change

To prevent scope creep — these v1 elements remain exactly as written:

- The 7 vault folders (`00 Inbox` through `99 Meta`) and all frontmatter schemas
- The four Cowork prompts (morning heavy / intraday light / hourly closure / weekly synthesis)
- The 5-worker process model (ingest / monitor / bot / api / eod)
- Discord channel layout and slash commands
- The "humans confirm entries; machines manage exits" invariant
- Inbox-then-process write discipline
- Phase model (Paper-1 → Paper-2 → Live-1 → Live-2)
- The build order in v1 §11 — v2 fixes are inserted before resuming Paper Phase 1 work
- Out-of-scope items in v1 §13 (crypto, options trading by user, international, signal selling, tax)

---

*v2 — Bedcrock. Audit fixes + selective Proxy Bot port-overs. v1 spec remains the foundation; this document is the diff.*
