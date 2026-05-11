"""Wave B4: dashboard read endpoints + scoring-proposals POST.

These tests use FastAPI's dependency overrides to swap in a fake AsyncSession
that returns canned rows, so the suite stays hermetic (no Postgres needed).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from src.api.main import app, get_db
from src.config import settings
from src.db.models import (
    Action,
    AuditLog,
    CloseReason,
    DailyState,
    Indicators,
    Mode,
    Position,
    PositionStatus,
    ScoringProposal,
    ScoringReplayReport,
    Signal,
    SignalSource,
    SignalStatus,
)


# ---------------------------------------------------------------------------
# Fake DB
# ---------------------------------------------------------------------------


class _Result:
    """Mimics sqlalchemy Result with .scalar_one_or_none / .scalars().all() / .scalar() / .all()."""

    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = rows  # for .all() returning tuples

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        if self._rows is not None:
            return self._rows
        if self._value is None:
            return []
        return self._value if isinstance(self._value, list) else [self._value]


class FakeDB:
    """Routes execute() calls to canned results based on the table name in the SQL."""

    def __init__(self, table_results: dict | None = None):
        # table_results: keyed by lowercase substring matched against str(stmt)
        self.table_results = table_results or {}
        self.added: list = []
        self.commits = 0

    async def execute(self, stmt):
        sql = str(stmt).lower()
        # Group-by aggregate query for trades_by_source returns tuples in .all()
        if "group by signals.source" in sql or "group by signal" in sql and "count" in sql:
            res = self.table_results.get("signals_grouped")
            if res is not None:
                return _Result(rows=res)
        # Count(*) queries -> .scalar()
        if "count(" in sql:
            if "from positions" in sql:
                return _Result(value=self.table_results.get("positions_count", 0))
            if "from draft_orders" in sql:
                return _Result(value=self.table_results.get("drafts_count", 0))
            if "from signals" in sql:
                return _Result(value=self.table_results.get("signals_count", 0))
            return _Result(value=0)
        # Plain SELECT queries
        if "from positions" in sql:
            # closures filter on exit_at in WHERE; open queries do not.
            where_part = sql.split("where", 1)[-1] if "where" in sql else ""
            if "exit_at" in where_part:
                return _Result(value=self.table_results.get("closed_positions", []))
            return _Result(value=self.table_results.get("open_positions", []))
        if "from signals" in sql:
            where_part = sql.split("where", 1)[-1] if "where" in sql else ""
            if "gate_blocked" in where_part:
                return _Result(value=self.table_results.get("blocked_signals", []))
            return _Result(value=self.table_results.get("recent_signals", []))
        if "from earnings_calendar" in sql:
            return _Result(value=self.table_results.get("earnings", []))
        if "from indicators" in sql:
            return _Result(value=self.table_results.get("indicator", None))
        if "from audit_log" in sql:
            return _Result(value=self.table_results.get("alerts", []))
        if "from daily_state" in sql:
            return _Result(value=self.table_results.get("daily_state", None))
        if "from scoring_proposals" in sql:
            return _Result(value=self.table_results.get("pending_proposals", []))
        if "from scoring_replay_reports" in sql:
            return _Result(value=self.table_results.get("replay_reports", []))
        if "from draft_orders" in sql:
            return _Result(value=self.table_results.get("drafts", []))
        return _Result(value=None)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bearer_token():
    return settings.api_signing_secret.get_secret_value()


@pytest.fixture
def auth_headers(bearer_token):
    return {"Authorization": f"Bearer {bearer_token}"}


@pytest.fixture
def client_factory():
    """Yields a builder that returns a TestClient with an injected FakeDB."""
    def _build(table_results: dict | None = None) -> tuple[TestClient, FakeDB]:
        db = FakeDB(table_results or {})

        async def _override_get_db():
            yield db

        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        return client, db

    yield _build
    app.dependency_overrides.clear()


def _make_position(ticker="NVDA", status=PositionStatus.OPEN, **kw):
    p = Position(
        mode=Mode.PAPER,
        ticker=ticker,
        side=Action.BUY,
        broker_order_id=f"BO-{ticker}-{uuid.uuid4().hex[:6]}",
        entry_price=Decimal("100"),
        quantity=Decimal("10"),
        stop=Decimal("95"),
        target=Decimal("110"),
        status=status,
        source_signal_ids=[],
        **kw,
    )
    p.id = uuid.uuid4()
    p.entry_at = datetime.now(UTC) - timedelta(hours=2)
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dashboard_morning_happy(client_factory, auth_headers):
    pos = _make_position("NVDA")
    sig = Signal(
        mode=Mode.PAPER,
        source=SignalSource.SEC_FORM4,
        source_external_id="ext-1",
        ticker="NVDA",
        action=Action.BUY,
        disclosed_at=datetime.now(UTC) - timedelta(hours=1),
        score=Decimal("7.5"),
        gate_blocked=False,
        gates_failed=[],
        status=SignalStatus.NEW,
    )
    sig.id = uuid.uuid4()
    daily = DailyState(
        date=datetime.now(UTC).date(),
        mode=settings.mode,
        daily_pnl_pct=Decimal("0.5"),
        equity_at_open=Decimal("100000"),
    )
    ind = Indicators(ticker="SPY", computed_at=datetime.now(UTC), trend="uptrend")
    ind.id = uuid.uuid4()

    client, _db = client_factory({
        "open_positions": [pos],
        "recent_signals": [sig],
        "earnings": [],
        "blocked_signals": [],
        "daily_state": daily,
        "indicator": ind,
    })
    r = client.get("/dashboard/morning", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["regime"] == "uptrend"
    assert len(body["open_positions"]) == 1
    assert body["open_positions"][0]["ticker"] == "NVDA"
    assert len(body["recent_signals"]) == 1
    assert body["daily_state"]["pnl_pct"] == 0.5
    assert body["gates_blocked_yesterday"] == {}


def test_dashboard_intraday_happy(client_factory, auth_headers):
    pos = _make_position("AAPL")
    ind = Indicators(ticker="AAPL", computed_at=datetime.now(UTC), price=Decimal("105"))
    ind.id = uuid.uuid4()
    audit = AuditLog(
        actor="monitor",
        action="position_alert_stop_proximity",
        target_kind="position",
        target_id=str(pos.id),
        details={"distance_pct": 1.2},
    )
    audit.id = uuid.uuid4()
    audit.occurred_at = datetime.now(UTC)
    daily = DailyState(
        date=datetime.now(UTC).date(),
        mode=settings.mode,
        daily_pnl_pct=Decimal("-0.5"),
    )

    client, _db = client_factory({
        "open_positions": [pos],
        "indicator": ind,
        "alerts": [audit],
        "daily_state": daily,
    })
    r = client.get("/dashboard/intraday", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kill_switch_status"] == "active"
    assert body["daily_pnl_pct"] == -0.5
    assert len(body["open_positions"]) == 1
    p = body["open_positions"][0]
    assert p["current_price"] == "105"
    # 5% gain from 100 -> 105
    assert abs(p["unrealized_pnl_pct"] - 5.0) < 1e-6
    assert len(body["recent_alerts"]) == 1


def test_dashboard_closures_happy(client_factory, auth_headers):
    closed = _make_position("MSFT", status=PositionStatus.CLOSED)
    closed.exit_at = datetime.now(UTC) - timedelta(hours=1)
    closed.exit_price = Decimal("108")
    closed.pnl_usd = Decimal("80")
    closed.pnl_pct = Decimal("8.0")
    closed.close_reason = CloseReason.TARGET_HIT

    client, _db = client_factory({"closed_positions": [closed]})
    r = client.get("/dashboard/closures?hours=24", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hours"] == 24
    assert len(body["closures"]) == 1
    c = body["closures"][0]
    assert c["ticker"] == "MSFT"
    assert c["exit_price"] == "108"
    assert c["close_reason"] == "target_hit"
    assert c["holding_minutes"] is not None and c["holding_minutes"] > 0


def test_dashboard_weekly_happy(client_factory, auth_headers):
    proposal = ScoringProposal(
        weights={"cluster_per_extra_source": 1.5},
        rationale="bump cluster weight",
        source="weekly-synthesis",
        status="pending",
    )
    proposal.id = uuid.uuid4()
    proposal.proposed_at = datetime.now(UTC)
    report = ScoringReplayReport(
        proposal_id=proposal.id,
        in_sample_sharpe=1.4,
        out_of_sample_sharpe=1.1,
        win_rate=0.55,
        profit_factor=1.6,
        total_return_pct=12.3,
        sharpe_delta_vs_baseline=0.2,
        recommendation="adopt",
    )
    report.id = uuid.uuid4()
    report.created_at = datetime.now(UTC)

    client, _db = client_factory({
        "signals_grouped": [(SignalSource.SEC_FORM4, 5), (SignalSource.UW_FLOW, 3)],
        "closed_positions": [],
        "pending_proposals": [proposal],
        "replay_reports": [report],
    })
    r = client.get("/dashboard/weekly", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trades_by_source"].get("sec_form4") == 5
    assert body["trades_by_source"].get("uw_flow") == 3
    assert body["win_rate_per_trader"] == []
    assert "cluster_per_extra_source" in body["current_weights"]
    assert len(body["pending_proposals"]) == 1
    assert body["pending_proposals"][0]["rationale"] == "bump cluster weight"
    assert len(body["recent_replay_reports"]) == 1


def test_dashboard_status_happy(client_factory, auth_headers):
    daily = DailyState(
        date=datetime.now(UTC).date(),
        mode=settings.mode,
        daily_pnl_pct=Decimal("1.25"),
        equity_at_open=Decimal("100000"),
    )
    client, _db = client_factory({
        "positions_count": 3,
        "drafts_count": 2,
        "signals_count": 17,
        "daily_state": daily,
    })
    r = client.get("/dashboard/status", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["open_positions_count"] == 3
    assert body["today_drafts_count"] == 2
    assert body["today_signals_count"] == 17
    assert body["equity"] == 100000.0
    assert body["daily_pnl_pct"] == 1.25
    assert body["mode"] in {"paper", "live", "baseline"}


def test_scoring_proposals_post_happy(client_factory, auth_headers):
    client, db = client_factory({})
    payload = {
        "weights": {"cluster_per_extra_source": 1.5, "trend_alignment": 1.2},
        "rationale": "Tighten trend alignment",
        "source": "weekly-synthesis",
    }
    r = client.post("/scoring-proposals", json=payload, headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "id" in body
    # Verify a ScoringProposal row was added.
    assert any(isinstance(o, ScoringProposal) for o in db.added)
    proposal = next(o for o in db.added if isinstance(o, ScoringProposal))
    assert proposal.status == "pending"
    assert proposal.rationale == "Tighten trend alignment"
    assert db.commits == 1


def test_dashboard_requires_bearer(client_factory):
    client, _db = client_factory({})
    # No auth header at all -> 401
    r = client.get("/dashboard/status")
    assert r.status_code == 401
    # Wrong scheme -> 401
    r = client.get("/dashboard/status", headers={"Authorization": "Basic abc"})
    assert r.status_code == 401
    # Bad token -> 401
    r = client.get("/dashboard/status", headers={"Authorization": "Bearer wrongtoken"})
    assert r.status_code == 401
