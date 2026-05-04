"""FastAPI app — health, confirm, skip.

The Discord bot calls this on the same host. Signed URLs use itsdangerous so
a /confirm link can't be forged externally — useful for mobile push.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker import BrokerError, get_broker
from src.config import settings
from src.db.models import AuditLog, DraftOrder, IngestorHeartbeat, OrderStatus
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
