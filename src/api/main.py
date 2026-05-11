"""FastAPI app — health, confirm, skip.

The Discord bot calls this on the same host. Signed URLs use itsdangerous so
a /confirm link can't be forged externally — useful for mobile push.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import desc, func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker import BrokerError, get_broker
from src.config import settings
from src.db.models import (
    AuditLog,
    DailyState,
    DraftOrder,
    EarningsCalendar,
    IngestorHeartbeat,
    Indicators,
    OrderStatus,
    Position,
    PositionStatus,
    ScoringProposal,
    ScoringReplayReport,
    Signal,
)
from src.db.session import SessionLocal, dispose
from src.logging_config import configure_logging, get_logger
from src.orders.builder import BracketBuilder
from src.schemas import HealthResponse

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("api_starting", mode=settings.mode.value)
    yield
    await dispose()
    logger.info("api_stopped")


app = FastAPI(title="Bedcrock API", lifespan=lifespan)
signer = URLSafeTimedSerializer(settings.api_signing_secret.get_secret_value())


async def get_db():
    async with SessionLocal() as session:
        yield session


@app.get("/health", response_model=HealthResponse)
async def health(db: AsyncSession = Depends(get_db)) -> HealthResponse:
    db_ok = True
    broker_ok = False
    ingestor_status: dict[str, dict] = {}

    try:
        await db.execute(select(IngestorHeartbeat))
    except Exception:
        db_ok = False

    try:
        broker = get_broker()
        await broker.connect()
        broker_ok = await broker.healthcheck()
        await broker.disconnect()
    except Exception as e:
        logger.warning("health_broker_check_failed", error=str(e))

    if db_ok:
        rows = (await db.execute(select(IngestorHeartbeat))).scalars().all()
        for r in rows:
            ingestor_status[r.ingestor] = {
                "last_run_at": r.last_run_at.isoformat(),
                "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
                "last_error": r.last_error,
                "signals_in_last_run": r.signals_in_last_run,
            }

    return HealthResponse(
        status="ok" if (db_ok and broker_ok) else "degraded",
        mode=settings.mode,
        db_ok=db_ok,
        broker_ok=broker_ok,
        ingestors=ingestor_status,
    )


@app.post("/confirm/{draft_id}")
async def confirm(draft_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    draft = await _get_draft_or_404(db, draft_id)

    if draft.status != OrderStatus.DRAFT:
        raise HTTPException(409, f"Draft is in status {draft.status.value}")

    if draft.expires_at and draft.expires_at < datetime.now(UTC):
        draft.status = OrderStatus.EXPIRED
        await db.commit()
        raise HTTPException(410, "Draft expired")

    spec = BracketBuilder.spec_from_draft(draft)

    broker = get_broker()
    try:
        await broker.connect()
        submitted = await broker.submit_bracket(spec)
    except BrokerError as e:
        draft.status = OrderStatus.REJECTED
        draft.skip_reason = f"broker error: {e}"
        await db.commit()
        raise HTTPException(502, f"Broker error: {e}") from e
    finally:
        await broker.disconnect()

    draft.status = OrderStatus.SENT
    draft.broker_order_id = submitted.broker_order_id
    draft.confirmed_at = datetime.now(UTC)

    db.add(AuditLog(
        actor="api:confirm",
        action="order_confirmed",
        target_kind="draft_order",
        target_id=str(draft.id),
        details={"broker_order_id": submitted.broker_order_id, "ticker": draft.ticker},
    ))
    await db.commit()

    logger.info(
        "order_confirmed",
        draft_id=str(draft.id),
        broker_order_id=submitted.broker_order_id,
        ticker=draft.ticker,
    )
    return {
        "status": "sent",
        "draft_id": str(draft.id),
        "broker_order_id": submitted.broker_order_id,
        "ticker": draft.ticker,
        "side": draft.side.value,
        "quantity": str(draft.quantity),
    }


@app.post("/skip/{draft_id}")
async def skip(
    draft_id: UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    draft = await _get_draft_or_404(db, draft_id)
    if draft.status != OrderStatus.DRAFT:
        raise HTTPException(409, f"Draft is in status {draft.status.value}")

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    reason = body.get("reason", "") if isinstance(body, dict) else ""

    draft.status = OrderStatus.SKIPPED
    draft.skip_reason = reason

    db.add(AuditLog(
        actor="api:skip",
        action="order_skipped",
        target_kind="draft_order",
        target_id=str(draft.id),
        details={"reason": reason, "ticker": draft.ticker},
    ))
    await db.commit()
    return {"status": "skipped", "draft_id": str(draft.id)}


@app.get("/confirm-signed/{token}")
async def confirm_signed(token: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        payload = signer.loads(token, max_age=8 * 3600)
    except SignatureExpired:
        raise HTTPException(410, "Link expired") from None
    except BadSignature:
        raise HTTPException(403, "Bad signature") from None
    return await confirm(UUID(payload["draft_id"]), db)


@app.get("/skip-signed/{token}")
async def skip_signed(token: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        payload = signer.loads(token, max_age=8 * 3600)
    except SignatureExpired:
        raise HTTPException(410, "Link expired") from None
    except BadSignature:
        raise HTTPException(403, "Bad signature") from None
    return await skip(UUID(payload["draft_id"]), request, db)


def make_signed_link(draft_id: UUID, kind: str = "confirm") -> str:
    token = signer.dumps({"draft_id": str(draft_id)})
    return f"http://{settings.api_host}:{settings.api_port}/{kind}-signed/{token}"


async def _get_draft_or_404(db: AsyncSession, draft_id: UUID) -> DraftOrder:
    stmt = select(DraftOrder).where(DraftOrder.id == draft_id)
    draft = (await db.execute(stmt)).scalar_one_or_none()
    if not draft:
        raise HTTPException(404, "Draft not found")
    return draft


# ---------------------------------------------------------------------------
# Wave B4: dashboard read endpoints + scoring-proposals POST
# ---------------------------------------------------------------------------


def _expected_bearer() -> str:
    """The token required by /dashboard/* and /scoring-proposals.

    Prefers `api_bearer_token` if set; falls back to `api_signing_secret` so
    operators don't have to configure two secrets in dev environments.
    """
    tok = settings.api_bearer_token.get_secret_value()
    if tok:
        return tok
    return settings.api_signing_secret.get_secret_value()


async def require_bearer(authorization: str | None = Header(None)) -> str:
    """Require an `Authorization: Bearer <token>` header matching expected token."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    expected = _expected_bearer()
    if not token or token != expected:
        raise HTTPException(401, "Invalid bearer token")
    return token


def _position_summary(p: Position) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "ticker": p.ticker,
        "side": p.side.value if hasattr(p.side, "value") else p.side,
        "quantity": str(p.quantity),
        "entry_price": str(p.entry_price),
        "stop": str(p.stop),
        "target": str(p.target),
        "entry_at": p.entry_at.isoformat() if p.entry_at else None,
    }


def _signal_summary(s: Signal) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "ticker": s.ticker,
        "source": s.source.value if hasattr(s.source, "value") else s.source,
        "action": s.action.value if hasattr(s.action, "value") else s.action,
        "score": float(s.score) if s.score is not None else None,
        "disclosed_at": s.disclosed_at.isoformat() if s.disclosed_at else None,
        "gate_blocked": bool(s.gate_blocked),
        "gates_failed": list(s.gates_failed or []),
    }


@app.get("/dashboard/morning")
async def dashboard_morning(
    db: AsyncSession = Depends(get_db),
    _token: str = Depends(require_bearer),
) -> dict[str, Any]:
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)

    open_positions = (
        await db.execute(
            select(Position).where(
                Position.mode == settings.mode,
                Position.status == PositionStatus.OPEN,
            )
        )
    ).scalars().all()

    recent_signals = (
        await db.execute(
            select(Signal)
            .where(Signal.disclosed_at >= cutoff, Signal.score >= 5)
            .order_by(desc(Signal.disclosed_at))
            .limit(50)
        )
    ).scalars().all()

    watchlist = sorted({p.ticker for p in open_positions} | {s.ticker for s in recent_signals})
    today_earnings: list[dict[str, Any]] = []
    if watchlist:
        today_start = datetime.combine(date.today(), datetime.min.time(), tzinfo=UTC)
        today_end = today_start + timedelta(days=1)
        rows = (
            await db.execute(
                select(EarningsCalendar).where(
                    EarningsCalendar.ticker.in_(watchlist),
                    EarningsCalendar.earnings_date >= today_start,
                    EarningsCalendar.earnings_date < today_end,
                )
            )
        ).scalars().all()
        today_earnings = [
            {
                "ticker": e.ticker,
                "earnings_date": e.earnings_date.isoformat(),
                "when": e.when,
            }
            for e in rows
        ]

    yesterday = date.today() - timedelta(days=1)
    y_start = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC)
    y_end = y_start + timedelta(days=1)
    blocked_yesterday = (
        await db.execute(
            select(Signal).where(
                Signal.gate_blocked.is_(True),
                Signal.disclosed_at >= y_start,
                Signal.disclosed_at < y_end,
            )
        )
    ).scalars().all()
    gates_blocked: dict[str, int] = {}
    for s in blocked_yesterday:
        for g in (s.gates_failed or []):
            gates_blocked[g] = gates_blocked.get(g, 0) + 1

    daily_state_row = (
        await db.execute(
            select(DailyState).where(
                DailyState.date == date.today(),
                DailyState.mode == settings.mode,
            )
        )
    ).scalar_one_or_none()
    daily_state = (
        {
            "pnl_pct": float(daily_state_row.daily_pnl_pct),
            "equity_at_open": (
                float(daily_state_row.equity_at_open)
                if daily_state_row.equity_at_open is not None
                else None
            ),
        }
        if daily_state_row
        else {"pnl_pct": None, "equity_at_open": None}
    )

    # Pull most-recent indicator row to surface current regime, if any.
    regime_row = (
        await db.execute(
            select(Indicators).order_by(desc(Indicators.computed_at)).limit(1)
        )
    ).scalar_one_or_none()
    regime = regime_row.trend if regime_row else None

    return {
        "regime": regime,
        "open_positions": [_position_summary(p) for p in open_positions],
        "recent_signals": [_signal_summary(s) for s in recent_signals],
        "today_earnings": today_earnings,
        "gates_blocked_yesterday": gates_blocked,
        "daily_state": daily_state,
    }


@app.get("/dashboard/intraday")
async def dashboard_intraday(
    db: AsyncSession = Depends(get_db),
    _token: str = Depends(require_bearer),
) -> dict[str, Any]:
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=4)

    open_positions = (
        await db.execute(
            select(Position).where(
                Position.mode == settings.mode,
                Position.status == PositionStatus.OPEN,
            )
        )
    ).scalars().all()

    # Latest cached price per ticker via Indicators.
    pos_rows: list[dict[str, Any]] = []
    for p in open_positions:
        ind = (
            await db.execute(
                select(Indicators)
                .where(Indicators.ticker == p.ticker)
                .order_by(desc(Indicators.computed_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        current_price = ind.price if ind else None
        unrealized_pnl_pct: float | None = None
        distance_to_stop_pct: float | None = None
        if current_price is not None and p.entry_price:
            unrealized_pnl_pct = float((current_price - p.entry_price) / p.entry_price) * 100.0
            if p.stop:
                distance_to_stop_pct = float((current_price - p.stop) / current_price) * 100.0
        pos_rows.append({
            "ticker": p.ticker,
            "qty": str(p.quantity),
            "entry_price": str(p.entry_price),
            "current_price": str(current_price) if current_price is not None else None,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "distance_to_stop_pct": distance_to_stop_pct,
        })

    recent_alerts = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.action.like("position_alert%"),
                AuditLog.occurred_at >= cutoff,
            )
            .order_by(desc(AuditLog.occurred_at))
            .limit(50)
        )
    ).scalars().all()

    daily_state_row = (
        await db.execute(
            select(DailyState).where(
                DailyState.date == date.today(),
                DailyState.mode == settings.mode,
            )
        )
    ).scalar_one_or_none()
    daily_pnl_pct = (
        float(daily_state_row.daily_pnl_pct) if daily_state_row else 0.0
    )

    threshold = settings.risk_daily_loss_pct
    kill_switch_status = "halted" if daily_pnl_pct <= -threshold else "active"

    return {
        "open_positions": pos_rows,
        "recent_alerts": [
            {
                "occurred_at": a.occurred_at.isoformat() if a.occurred_at else None,
                "actor": a.actor,
                "action": a.action,
                "target_id": a.target_id,
                "details": a.details or {},
            }
            for a in recent_alerts
        ],
        "kill_switch_status": kill_switch_status,
        "daily_pnl_pct": daily_pnl_pct,
    }


@app.get("/dashboard/closures")
async def dashboard_closures(
    hours: int = 24,
    db: AsyncSession = Depends(get_db),
    _token: str = Depends(require_bearer),
) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    rows = (
        await db.execute(
            select(Position)
            .where(
                Position.mode == settings.mode,
                Position.status == PositionStatus.CLOSED,
                Position.exit_at >= cutoff,
            )
            .order_by(desc(Position.exit_at))
        )
    ).scalars().all()

    closures: list[dict[str, Any]] = []
    for p in rows:
        holding_minutes: float | None = None
        if p.entry_at and p.exit_at:
            holding_minutes = (p.exit_at - p.entry_at).total_seconds() / 60.0
        closures.append({
            "id": str(p.id),
            "ticker": p.ticker,
            "entry_price": str(p.entry_price),
            "exit_price": str(p.exit_price) if p.exit_price is not None else None,
            "pnl_usd": str(p.pnl_usd) if p.pnl_usd is not None else None,
            "pnl_pct": float(p.pnl_pct) if p.pnl_pct is not None else None,
            "close_reason": (
                p.close_reason.value if p.close_reason and hasattr(p.close_reason, "value")
                else p.close_reason
            ),
            "holding_minutes": holding_minutes,
            "source_signal_ids": list(p.source_signal_ids or []),
        })
    return {"closures": closures, "hours": hours}


@app.get("/dashboard/weekly")
async def dashboard_weekly(
    db: AsyncSession = Depends(get_db),
    _token: str = Depends(require_bearer),
) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(days=7)

    # trades_by_source: count signals per source that have a non-null score
    # (proxy for "considered for trade") in the last 7d.
    src_rows = (
        await db.execute(
            select(Signal.source, sa_func.count(Signal.id))
            .where(Signal.disclosed_at >= cutoff)
            .group_by(Signal.source)
        )
    ).all()
    trades_by_source = {
        (s.value if hasattr(s, "value") else str(s)): int(c)
        for s, c in src_rows
    }

    # win-rate per trader on closed positions in the last 7d.
    closed = (
        await db.execute(
            select(Position).where(
                Position.status == PositionStatus.CLOSED,
                Position.exit_at >= cutoff,
            )
        )
    ).scalars().all()

    by_trader: dict[str, dict[str, Any]] = {}
    for p in closed:
        # Resolve trader via source_signal_ids -> Signal.trader -> Trader.
        sigs = []
        if p.source_signal_ids:
            sigs = (
                await db.execute(
                    select(Signal).where(Signal.id.in_(p.source_signal_ids))
                )
            ).scalars().all()
        for sig in sigs:
            trader = sig.trader
            name = trader.display_name if trader else "(unknown)"
            d = by_trader.setdefault(
                name, {"trader_name": name, "n_trades": 0, "wins": 0, "pnl_sum": 0.0}
            )
            d["n_trades"] += 1
            if p.pnl_pct is not None and p.pnl_pct > 0:
                d["wins"] += 1
            if p.pnl_pct is not None:
                d["pnl_sum"] += float(p.pnl_pct)

    win_rate_per_trader = []
    for name, d in by_trader.items():
        n = d["n_trades"]
        win_rate_per_trader.append({
            "trader_name": name,
            "n_trades": n,
            "win_rate": (d["wins"] / n) if n else 0.0,
            "avg_pnl_pct": (d["pnl_sum"] / n) if n else 0.0,
        })

    from src.scoring.scorer import DEFAULT_WEIGHTS
    current_weights = dict(DEFAULT_WEIGHTS)

    pending = (
        await db.execute(
            select(ScoringProposal)
            .where(ScoringProposal.status == "pending")
            .order_by(desc(ScoringProposal.proposed_at))
        )
    ).scalars().all()
    pending_proposals = [
        {
            "id": str(pp.id),
            "weights": pp.weights or {},
            "rationale": pp.rationale,
            "proposed_at": pp.proposed_at.isoformat() if pp.proposed_at else None,
        }
        for pp in pending
    ]

    reports = (
        await db.execute(
            select(ScoringReplayReport)
            .order_by(desc(ScoringReplayReport.created_at))
            .limit(4)
        )
    ).scalars().all()
    recent_replay_reports = [
        {
            "id": str(r.id),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "proposal_id": str(r.proposal_id),
            "in_sample_sharpe": r.in_sample_sharpe,
            "out_of_sample_sharpe": r.out_of_sample_sharpe,
            "win_rate": r.win_rate,
            "profit_factor": r.profit_factor,
            "total_return_pct": r.total_return_pct,
            "sharpe_delta_vs_baseline": r.sharpe_delta_vs_baseline,
            "recommendation": r.recommendation,
        }
        for r in reports
    ]

    return {
        "trades_by_source": trades_by_source,
        "win_rate_per_trader": win_rate_per_trader,
        "current_weights": current_weights,
        "pending_proposals": pending_proposals,
        "recent_replay_reports": recent_replay_reports,
    }


@app.get("/dashboard/status")
async def dashboard_status(
    db: AsyncSession = Depends(get_db),
    _token: str = Depends(require_bearer),
) -> dict[str, Any]:
    today_start = datetime.combine(date.today(), datetime.min.time(), tzinfo=UTC)

    open_positions_count = (
        await db.execute(
            select(sa_func.count(Position.id)).where(
                Position.mode == settings.mode,
                Position.status == PositionStatus.OPEN,
            )
        )
    ).scalar() or 0

    today_drafts_count = (
        await db.execute(
            select(sa_func.count(DraftOrder.id)).where(
                DraftOrder.mode == settings.mode,
                DraftOrder.created_at >= today_start,
            )
        )
    ).scalar() or 0

    today_signals_count = (
        await db.execute(
            select(sa_func.count(Signal.id)).where(
                Signal.disclosed_at >= today_start,
            )
        )
    ).scalar() or 0

    daily_state_row = (
        await db.execute(
            select(DailyState).where(
                DailyState.date == date.today(),
                DailyState.mode == settings.mode,
            )
        )
    ).scalar_one_or_none()
    equity = (
        float(daily_state_row.equity_at_open)
        if daily_state_row and daily_state_row.equity_at_open is not None
        else None
    )
    daily_pnl_pct = (
        float(daily_state_row.daily_pnl_pct) if daily_state_row else 0.0
    )

    return {
        "equity": equity,
        "daily_pnl_pct": daily_pnl_pct,
        "open_positions_count": int(open_positions_count),
        "today_drafts_count": int(today_drafts_count),
        "today_signals_count": int(today_signals_count),
        "mode": settings.mode.value,
    }


class ScoringProposalRequest(BaseModel):
    weights: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    source: str = "weekly-synthesis"


@app.post("/scoring-proposals")
async def create_scoring_proposal(
    body: ScoringProposalRequest,
    db: AsyncSession = Depends(get_db),
    _token: str = Depends(require_bearer),
) -> dict[str, str]:
    proposal = ScoringProposal(
        weights=body.weights,
        rationale=body.rationale,
        source=body.source,
        status="pending",
    )
    db.add(proposal)
    await db.commit()
    return {"id": str(proposal.id)}
