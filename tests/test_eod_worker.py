"""Tests for `src.workers.eod_worker`.

We exercise the v0.3 contract:

  * EOD posts a `system_health` Discord embed (no vault writes).
  * Sunday replay reads pending `ScoringProposal` rows, runs `replay()`, and
    persists a `ScoringReplayReport` row linked back to the proposal — no
    files written.

All DB / broker / webhook / replay boundaries are mocked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# `src.workers.eod_worker` pulls in `src.db.session`, which builds the async
# engine at import time and therefore needs `asyncpg`. Skip the module
# cleanly if the dev environment is missing the driver.
pytest.importorskip("asyncpg")

from src.backtest.replay import ReplayReport
from src.broker.base import AccountSnapshot
from src.db.models import ScoringProposal


def _account(equity: Decimal = Decimal("100000")) -> AccountSnapshot:
    return AccountSnapshot(
        equity=equity,
        cash=equity,
        positions_value=Decimal("0"),
        buying_power=equity,
        pattern_day_trader=False,
    )


def _result(value):
    """Make a Result-like mock whose `.scalar_one_or_none()` and
    `.scalars().all()` both yield the supplied value (single or list)."""
    r = MagicMock()
    if isinstance(value, list):
        r.scalar_one_or_none.return_value = value[0] if value else None
        scalars = MagicMock()
        scalars.all.return_value = value
        r.scalars.return_value = scalars
    else:
        r.scalar_one_or_none.return_value = value
        scalars = MagicMock()
        scalars.all.return_value = [value] if value is not None else []
        r.scalars.return_value = scalars
    return r


def _db_with_results(results: list):
    """An AsyncSession-like mock whose successive `execute()` calls return
    the supplied list of results, in order."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=results)
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    return db


@pytest.mark.asyncio
async def test_eod_summary_posts_to_discord(monkeypatch):
    """`main()` should post a system-health embed summarising EOD state and
    do no file/vault writes."""
    broker = MagicMock()
    broker.connect = AsyncMock()
    broker.disconnect = AsyncMock()
    broker.get_account = AsyncMock(return_value=_account(Decimal("101000")))

    # Results: prev snapshot lookup, open positions, today signals, today drafts.
    db = _db_with_results([
        _result(None),          # no prior snapshot
        _result(None),          # upsert (return value unused)
        _result([]),            # open_positions
        _result([]),            # today_signals
        _result([]),            # today_drafts
    ])

    SessionLocal = MagicMock(return_value=db)
    posted: dict = {}

    async def fake_post_system_health(*, title, description=None, body=None, color=None, ok=True):
        posted["title"] = title
        posted["description"] = description if description is not None else body
        posted["color"] = color

    from src.workers import eod_worker

    monkeypatch.setattr(eod_worker, "get_broker", lambda: broker)
    monkeypatch.setattr(eod_worker, "SessionLocal", SessionLocal)
    monkeypatch.setattr(eod_worker, "post_system_health", fake_post_system_health)
    monkeypatch.setattr(eod_worker, "dispose", AsyncMock())

    # Force non-Sunday so replay path is skipped.
    fake_dt = MagicMock(wraps=datetime)
    fake_now = datetime(2026, 5, 11, tzinfo=UTC)  # Monday
    fake_dt.now = MagicMock(return_value=fake_now)
    fake_dt.combine = datetime.combine
    fake_dt.min = datetime.min
    monkeypatch.setattr(eod_worker, "datetime", fake_dt)

    await eod_worker.main()

    assert "EOD" in posted["title"]
    assert "Equity" in posted["description"]
    assert "Open positions" in posted["description"]
    assert "Drafts" in posted["description"]


@pytest.mark.asyncio
async def test_replay_persists_to_db_not_vault(monkeypatch, tmp_path):
    """`run_weekly_replay()` should call `replay()` for each pending proposal,
    persist a `ScoringReplayReport` row, link it back, and write zero files."""
    proposal = ScoringProposal(
        id=uuid.uuid4(),
        weights={"sma": 0.5, "rsi": 0.5},
        status="pending",
    )

    report = ReplayReport(
        n_signals_in_scope=10,
        n_signals_above_threshold=5,
        n_trades_simulated=4,
        in_sample_sharpe=1.2,
        out_of_sample_sharpe=0.9,
        win_rate=0.6,
        profit_factor=1.5,
        total_return_pct=3.4,
        sharpe_delta_vs_baseline=0.1,
        recommendation="ADOPT",
    )

    db = _db_with_results([_result([proposal])])
    SessionLocal = MagicMock(return_value=db)

    from src.workers import eod_worker

    monkeypatch.setattr(eod_worker, "SessionLocal", SessionLocal)
    monkeypatch.setattr(
        eod_worker, "replay", AsyncMock(return_value=report)
    )

    # Snapshot tmp_path file count before/after to prove no files written.
    before = list(tmp_path.rglob("*"))

    await eod_worker.run_weekly_replay()

    after = list(tmp_path.rglob("*"))
    assert before == after, "run_weekly_replay must not write any files"

    # Exactly one ScoringReplayReport row should have been added + flushed.
    db.add.assert_called_once()
    added = db.add.call_args.args[0]
    from src.db.models import ScoringReplayReport
    assert isinstance(added, ScoringReplayReport)
    assert added.proposal_id == proposal.id
    assert added.recommendation == "ADOPT"
    assert added.in_sample_sharpe == pytest.approx(1.2)

    # Proposal was linked + stamped.
    assert proposal.replay_report_id == added.id
    assert proposal.evaluated_at is not None

    db.commit.assert_awaited()
