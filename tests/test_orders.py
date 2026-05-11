"""Tests for BracketOrderSpec validation and arithmetic."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.db.models import Action, Mode
from src.schemas import BracketOrderSpec


def test_bracket_long_arithmetic():
    spec = BracketOrderSpec(
        mode=Mode.PAPER,
        ticker="NVDA",
        side=Action.BUY,
        quantity=Decimal("10"),
        entry_limit=Decimal("500"),
        stop=Decimal("485"),
        target=Decimal("530"),
        setup="breakout",
    )
    assert spec.risk_per_share() == Decimal("15")
    assert spec.reward_per_share() == Decimal("30")
    assert spec.reward_to_risk() == 2.0


def test_bracket_short_arithmetic():
    spec = BracketOrderSpec(
        mode=Mode.PAPER,
        ticker="NVDA",
        side=Action.SELL,
        quantity=Decimal("5"),
        entry_limit=Decimal("500"),
        stop=Decimal("510"),
        target=Decimal("475"),
        setup="breakdown",
    )
    assert spec.risk_per_share() == Decimal("10")
    assert spec.reward_per_share() == Decimal("25")
    assert spec.reward_to_risk() == 2.5


def test_bracket_zero_risk_returns_zero_rr():
    spec = BracketOrderSpec(
        mode=Mode.PAPER,
        ticker="NVDA",
        side=Action.BUY,
        quantity=Decimal("1"),
        entry_limit=Decimal("500"),
        stop=Decimal("500"),
        target=Decimal("510"),
    )
    assert spec.reward_to_risk() == 0.0


# ---------------------------------------------------------------------------
# ATR-floored stop tests
# ---------------------------------------------------------------------------

from datetime import UTC, datetime
from unittest.mock import MagicMock

from src.orders.builder import OrderBuilder
from src.schemas import IndicatorSnapshot


@pytest.fixture
def indicators_atr10():
    return IndicatorSnapshot(
        ticker="NVDA",
        computed_at=datetime.now(UTC),
        atr_20=Decimal("10"),
    )


def test_atr_floored_stop_long_no_widening(indicators_atr10):
    """Long stop already wider than 1.5*ATR is unchanged."""
    # 1.5 * 10 = 15. Stop at 480 is 20 away from 500 entry -> no widening.
    result = OrderBuilder._atr_floored_stop(
        Action.BUY, Decimal("500"), Decimal("480"), indicators_atr10
    )
    assert result == Decimal("480")


def test_atr_floored_stop_long_widened(indicators_atr10):
    """Long stop closer than 1.5*ATR is widened to entry - 1.5*ATR."""
    # 1.5 * 10 = 15. Stop at 495 is only 5 away -> widen to 500 - 15 = 485.
    result = OrderBuilder._atr_floored_stop(
        Action.BUY, Decimal("500"), Decimal("495"), indicators_atr10
    )
    assert result == Decimal("485")


def test_atr_floored_stop_short_no_widening(indicators_atr10):
    """Short stop already wider than 1.5*ATR is unchanged."""
    # Stop at 520 is 20 away from 500 entry -> no widening.
    result = OrderBuilder._atr_floored_stop(
        Action.SELL, Decimal("500"), Decimal("520"), indicators_atr10
    )
    assert result == Decimal("520")


def test_atr_floored_stop_short_widened(indicators_atr10):
    """Short stop closer than 1.5*ATR is widened to entry + 1.5*ATR."""
    # Stop at 505 is only 5 away -> widen to 500 + 15 = 515.
    result = OrderBuilder._atr_floored_stop(
        Action.SELL, Decimal("500"), Decimal("505"), indicators_atr10
    )
    assert result == Decimal("515")


def test_atr_floored_stop_no_indicators():
    """No indicators returns the original stop unchanged."""
    result = OrderBuilder._atr_floored_stop(
        Action.BUY, Decimal("500"), Decimal("498"), None
    )
    assert result == Decimal("498")


def test_atr_floored_stop_no_atr():
    """Indicators without ATR returns the original stop unchanged."""
    ind = IndicatorSnapshot(
        ticker="NVDA",
        computed_at=datetime.now(UTC),
    )
    result = OrderBuilder._atr_floored_stop(
        Action.BUY, Decimal("500"), Decimal("498"), ind
    )
    assert result == Decimal("498")


def test_atr_floored_stop_exact_boundary(indicators_atr10):
    """Stop distance exactly at 1.5*ATR is not widened."""
    # 1.5 * 10 = 15. Stop at 485 is exactly 15 away.
    result = OrderBuilder._atr_floored_stop(
        Action.BUY, Decimal("500"), Decimal("485"), indicators_atr10
    )
    assert result == Decimal("485")


# ---------------------------------------------------------------------------
# spec_from_draft
# ---------------------------------------------------------------------------


def test_spec_from_draft_converts_correctly():
    """spec_from_draft maps DraftOrder fields to BracketOrderSpec."""
    draft = MagicMock()
    draft.mode = Mode.PAPER
    draft.ticker = "AAPL"
    draft.side = Action.BUY
    draft.quantity = Decimal("50")
    draft.entry_limit = Decimal("180")
    draft.stop = Decimal("170")
    draft.target = Decimal("200")
    draft.setup = "breakout"

    spec = OrderBuilder.spec_from_draft(draft)

    assert spec.mode == Mode.PAPER
    assert spec.ticker == "AAPL"
    assert spec.side == Action.BUY
    assert spec.quantity == Decimal("50")
    assert spec.entry_limit == Decimal("180")
    assert spec.stop == Decimal("170")
    assert spec.target == Decimal("200")
    assert spec.setup == "breakout"
    assert spec.time_in_force == "day"


# ---------------------------------------------------------------------------
# Monitor idempotency (Wave B2)
# ---------------------------------------------------------------------------
#
# These tests pin the behavior of `_on_entry_fill` and `_reconcile_orders`
# under the WS-vs-poll race described in docs/AUDIT_2026-05-10.md §3.3.
# The DB layer is faked with a tiny in-memory fixture so we don't need a
# Postgres server: real concurrency is exercised via asyncio.gather under
# an asyncio.Lock that mirrors the UNIQUE(broker_order_id) row guarantee.

import asyncio
import uuid
from unittest.mock import AsyncMock

from src.broker.base import BrokerOrder, BrokerOrderState

_NOW = datetime.now(UTC)
from src.db.models import OrderStatus, PositionStatus
from src.orders.monitor import LiveMonitor


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else [self._value]


class _FakeDB:
    """Minimal AsyncSession stand-in that mimics the UNIQUE constraint on
    Position.broker_order_id by serializing inserts behind a lock and
    rejecting a second insert with the same broker_order_id.
    """

    _global_lock = asyncio.Lock()

    def __init__(self):
        self.positions: list = []  # list of inserted Position objects
        self.audit_logs: list = []
        self.added: list = []  # pending (uncommitted) objects
        self.commits = 0

    async def execute(self, stmt):
        # Inspect the compiled statement string to decide what to return.
        # We only need to support the two SELECTs that the monitor issues:
        #   - SELECT Position WHERE broker_order_id = :v  (idempotency check)
        #   - SELECT DraftOrder WHERE status = 'sent'     (reconcile loop)
        sql = str(stmt).lower()
        if "from positions" in sql:
            # Pull the bind value if we can; otherwise compare against the
            # last broker_order_id we cached on this fake.
            target = getattr(self, "_lookup_broker_order_id", None)
            for p in self.positions:
                if p.broker_order_id == target:
                    return _FakeResult(p)
            return _FakeResult(None)
        if "from draft_orders" in sql:
            return _FakeResult(getattr(self, "_sent_drafts", []))
        return _FakeResult(None)

    def add(self, obj):
        # Imitate the UNIQUE(broker_order_id) constraint at flush time.
        from src.db.models import Position
        if isinstance(obj, Position):
            for existing in self.positions:
                if (
                    existing.broker_order_id is not None
                    and existing.broker_order_id == obj.broker_order_id
                ):
                    raise RuntimeError(
                        f"UNIQUE violation: broker_order_id={obj.broker_order_id}"
                    )
            self.positions.append(obj)
        else:
            self.audit_logs.append(obj)
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _make_draft(broker_order_id="BO-1"):
    """Build a DraftOrder-shaped MagicMock with the fields the monitor reads."""
    draft = MagicMock()
    draft.id = uuid.uuid4()
    draft.mode = Mode.PAPER
    draft.ticker = "NVDA"
    draft.side = Action.BUY
    draft.quantity = Decimal("10")
    draft.entry_limit = Decimal("500")
    draft.stop = Decimal("485")
    draft.target = Decimal("530")
    draft.setup = "breakout"
    draft.source_signal_ids = []
    draft.status = OrderStatus.SENT
    draft.broker_order_id = broker_order_id
    return draft


@pytest.fixture
def monitor(monkeypatch):
    """A LiveMonitor with all I/O side effects stubbed out."""
    # Avoid touching the broker at construction.
    monkeypatch.setattr("src.orders.monitor.make_broker", lambda: MagicMock())
    # Stub out Discord I/O to keep tests hermetic.
    monkeypatch.setattr(
        "src.orders.monitor.post_position_alert", AsyncMock()
    )
    return LiveMonitor()


@pytest.mark.asyncio
async def test_double_fill_idempotency(monitor):
    """Calling _on_entry_fill twice with the same broker_order_id creates
    exactly one Position row and leaves the draft FILLED."""
    db = _FakeDB()
    db._lookup_broker_order_id = "BO-DUP"
    draft = _make_draft(broker_order_id="BO-DUP")

    await monitor._on_entry_fill(
        db=db,
        draft=draft,
        broker_order_id="BO-DUP",
        ticker="NVDA",
        filled_qty=Decimal("10"),
        filled_avg=Decimal("500"),
    )
    # Second invocation — must be a no-op for Position creation.
    await monitor._on_entry_fill(
        db=db,
        draft=draft,
        broker_order_id="BO-DUP",
        ticker="NVDA",
        filled_qty=Decimal("10"),
        filled_avg=Decimal("500"),
    )

    assert len(db.positions) == 1
    assert draft.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_ws_and_poll_concurrent_no_duplicate_position(monitor):
    """Fire _on_entry_fill (WS path) and _reconcile_orders (poll path)
    concurrently; assert only one Position row survives."""
    db = _FakeDB()
    db._lookup_broker_order_id = "BO-RACE"
    draft = _make_draft(broker_order_id="BO-RACE")
    db._sent_drafts = [draft]

    # Stub the broker so reconcile sees the order as filled.
    bo = BrokerOrder(
        broker_order_id="BO-RACE",
        state=BrokerOrderState.FILLED,
        filled_qty=Decimal("10"),
        filled_avg_price=Decimal("500"),
        submitted_at=_NOW,
        raw={},
    )
    monitor._broker.get_order = AsyncMock(return_value=bo)

    async def ws_path():
        await monitor._on_entry_fill(
            db=db,
            draft=draft,
            broker_order_id="BO-RACE",
            ticker="NVDA",
            filled_qty=Decimal("10"),
            filled_avg=Decimal("500"),
        )

    async def poll_path():
        await monitor._reconcile_orders(db)

    await asyncio.gather(ws_path(), poll_path())

    assert len(db.positions) == 1
    assert draft.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_reconciler_repairs_drift_when_ws_won(monitor):
    """Position already exists (ws path won) but draft.status is still SENT.
    Reconcile must flip the draft to FILLED without creating a 2nd Position."""
    db = _FakeDB()
    db._lookup_broker_order_id = "BO-DRIFT"
    draft = _make_draft(broker_order_id="BO-DRIFT")
    draft.status = OrderStatus.SENT
    db._sent_drafts = [draft]

    # Pre-seed a Position as if the ws fill had already created it.
    from src.db.models import Position
    pos = Position(
        mode=Mode.PAPER,
        ticker="NVDA",
        side=Action.BUY,
        broker_order_id="BO-DRIFT",
        entry_price=Decimal("500"),
        quantity=Decimal("10"),
        stop=Decimal("485"),
        target=Decimal("530"),
        status=PositionStatus.OPEN,
    )
    db.positions.append(pos)

    bo = BrokerOrder(
        broker_order_id="BO-DRIFT",
        state=BrokerOrderState.FILLED,
        filled_qty=Decimal("10"),
        filled_avg_price=Decimal("500"),
        submitted_at=_NOW,
        raw={},
    )
    monitor._broker.get_order = AsyncMock(return_value=bo)

    await monitor._reconcile_orders(db)

    assert len(db.positions) == 1
    assert draft.status == OrderStatus.FILLED


def test_spec_from_draft_short():
    """spec_from_draft works for SELL side."""
    draft = MagicMock()
    draft.mode = Mode.LIVE
    draft.ticker = "TSLA"
    draft.side = Action.SELL
    draft.quantity = Decimal("20")
    draft.entry_limit = Decimal("300")
    draft.stop = Decimal("315")
    draft.target = Decimal("270")
    draft.setup = None

    spec = OrderBuilder.spec_from_draft(draft)

    assert spec.side == Action.SELL
    assert spec.setup is None
    assert spec.risk_per_share() == Decimal("15")
    assert spec.reward_per_share() == Decimal("30")
    assert spec.reward_to_risk() == 2.0


# ---------------------------------------------------------------------------
# Half-Kelly position-size cap (V2.7)
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock

from src.broker.base import AccountSnapshot


def _mock_broker(equity: Decimal):
    broker = MagicMock()
    broker.connect = AsyncMock(return_value=None)
    broker.disconnect = AsyncMock(return_value=None)
    broker.get_account = AsyncMock(
        return_value=AccountSnapshot(
            equity=equity,
            cash=equity,
            positions_value=Decimal("0"),
            buying_power=equity,
            pattern_day_trader=False,
        )
    )
    return broker


def _mock_db():
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock(return_value=None)
    return db


@pytest.mark.asyncio
async def test_position_size_capped_by_concentration(monkeypatch):
    """Tight stop would yield huge risk-based qty; concentration cap binds at 5%."""
    broker = _mock_broker(Decimal("100000"))
    monkeypatch.setattr("src.orders.builder.get_broker", lambda: broker)

    builder = OrderBuilder()
    draft = await builder.build_draft(
        ticker="NVDA",
        side=Action.BUY,
        entry_zone_low=Decimal("100"),
        entry_zone_high=Decimal("100"),
        stop=Decimal("99"),  # 1% stop -> qty_by_risk = 1000 / 1 = 1000
        target=Decimal("110"),  # rr = 10/1 = 10
        setup="breakout",
        score=1.0,
        source_signal_ids=[],
        indicators=None,
        db=_mock_db(),
    )
    assert draft is not None
    # equity * 0.05 / entry = 100_000 * 0.05 / 100 = 50
    assert draft.quantity == Decimal("50")


@pytest.mark.asyncio
async def test_position_size_unchanged_when_risk_lower(monkeypatch):
    """Wide stop -> qty_by_risk dominates; concentration cap does not bind."""
    broker = _mock_broker(Decimal("100000"))
    monkeypatch.setattr("src.orders.builder.get_broker", lambda: broker)

    builder = OrderBuilder()
    draft = await builder.build_draft(
        ticker="NVDA",
        side=Action.BUY,
        entry_zone_low=Decimal("100"),
        entry_zone_high=Decimal("100"),
        stop=Decimal("80"),  # $20 stop; risk_pct=1% -> dollar_risk=$1000 -> qty=50
        target=Decimal("160"),  # rr = 60/20 = 3.0
        setup="breakout",
        score=1.0,
        source_signal_ids=[],
        indicators=None,
        db=_mock_db(),
    )
    assert draft is not None
    # qty_by_risk = 1000 / 20 = 50
    # qty_by_concentration = 5000 / 100 = 50
    # min == 50 either way, but verify with smaller risk pct sanity:
    # use a much wider stop to be sure risk-bound binds
    broker2 = _mock_broker(Decimal("1000000"))  # 1M equity
    monkeypatch.setattr("src.orders.builder.get_broker", lambda: broker2)
    draft2 = await builder.build_draft(
        ticker="NVDA",
        side=Action.BUY,
        entry_zone_low=Decimal("100"),
        entry_zone_high=Decimal("100"),
        stop=Decimal("50"),  # $50 stop; dollar_risk = 10_000 -> qty=200
        target=Decimal("250"),  # rr = 150/50 = 3
        setup="breakout",
        score=1.0,
        source_signal_ids=[],
        indicators=None,
        db=_mock_db(),
    )
    assert draft2 is not None
    # qty_by_risk = 10_000 / 50 = 200
    # qty_by_concentration = 50_000 / 100 = 500
    # min = 200 (risk bound)
    assert draft2.quantity == Decimal("200")
