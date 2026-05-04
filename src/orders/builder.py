"""Order builder.

Takes a ScoredSignal (or a Cowork ACT-TODAY entry) and constructs a DraftOrder
row in the DB. The order is NOT sent to the broker yet — that happens via
/confirm in api/main.py or the Discord bot.

Position sizing: risk-based. We risk `risk_per_trade_pct` of equity per trade,
and the stop distance determines the share count.

  size = (equity * risk_pct/100) / |entry - stop|

This means a tighter stop = larger size (same dollar risk). The ATR-floored
stop in the IndicatorSnapshot prevents pathological cases — if the proposed
stop is closer than 1.5x ATR, we widen it to that floor.

Reward:risk gating: if (target - entry)/(entry - stop) < MIN_RR_RATIO, the
draft is rejected before being persisted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker import BrokerError, get_broker
from src.config import settings
from src.db.models import (
    Action,
    AuditLog,
    DraftOrder,
    OrderStatus,
)
from src.logging_config import get_logger
from src.schemas import BracketOrderSpec, IndicatorSnapshot

logger = get_logger(__name__)

MIN_RR_RATIO = 1.5
ATR_STOP_FLOOR_MULT = Decimal("1.5")
DRAFT_TTL_HOURS = 8


class OrderBuilder:
    """Builds DraftOrder rows. Does NOT send to broker — that's confirm_draft()."""

    @staticmethod
    def spec_from_draft(draft: DraftOrder) -> BracketOrderSpec:
        """Convert a persisted DraftOrder into the broker-facing BracketOrderSpec."""
        return BracketOrderSpec(
            mode=draft.mode,
            ticker=draft.ticker,
            side=draft.side,
            quantity=draft.quantity,
            entry_limit=draft.entry_limit,
            stop=draft.stop,
            target=draft.target,
            time_in_force="day",
            setup=draft.setup,
        )

    @staticmethod
    def _atr_floored_stop(
        side: Action, entry: Decimal, stop: Decimal, indicators: IndicatorSnapshot | None
    ) -> Decimal:
        """If the proposed stop is closer than 1.5x ATR, widen it to that floor."""
        if indicators is None or indicators.atr_20 is None:
            return stop
        floor_distance = indicators.atr_20 * ATR_STOP_FLOOR_MULT
        actual_distance = abs(entry - stop)
        if actual_distance >= floor_distance:
            return stop
        # Widen
        return entry - floor_distance if side == Action.BUY else entry + floor_distance

    async def build_draft(
        self,
        *,
        ticker: str,
        side: Action,
        entry_zone_low: Decimal,
        entry_zone_high: Decimal,
        stop: Decimal,
        target: Decimal,
        setup: str | None,
        score: float | None,
        source_signal_ids: list[uuid.UUID],
        indicators: IndicatorSnapshot | None,
        db: AsyncSession,
    ) -> DraftOrder | None:
        """Build a draft bracket. Persists if valid; returns the row or None."""
        ticker = ticker.upper()
        entry = (entry_zone_low + entry_zone_high) / Decimal("2")

        # Validation 1: stop on correct side
        if side == Action.BUY and stop >= entry:
            logger.warning("invalid_stop_side", ticker=ticker, side="buy", entry=str(entry), stop=str(stop))
            return None
        if side == Action.SELL and stop <= entry:
            logger.warning("invalid_stop_side", ticker=ticker, side="sell", entry=str(entry), stop=str(stop))
            return None

        # Validation 2: ATR floor
        stop = self._atr_floored_stop(side, entry, stop, indicators)

        # Validation 3: R:R
        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk == 0:
            return None
        rr = float(reward / risk)
        if rr < MIN_RR_RATIO:
            logger.info("rr_too_low", ticker=ticker, rr=rr)
            return None

        # Position sizing — fetch live equity from broker
        broker = get_broker()
        try:
            await broker.connect()
            account = await broker.get_account()
        except (BrokerError, Exception) as e:
            logger.error("get_account_failed", error=str(e))
            await broker.disconnect()
            return None

        risk_pct = Decimal(str(settings.risk_per_trade_pct))
        dollar_risk = account.equity * risk_pct / Decimal("100")
        quantity = (dollar_risk / risk).quantize(Decimal("1"))
        await broker.disconnect()

        if quantity <= 0:
            logger.info("size_zero", ticker=ticker)
            return None

        # Persist
        draft = DraftOrder(
            mode=settings.mode,
            ticker=ticker,
            side=side,
            quantity=quantity,
            entry_limit=entry,
            stop=stop,
            target=target,
            setup=setup,
            score_at_creation=score,
            source_signal_ids=[str(s) for s in source_signal_ids],
            status=OrderStatus.DRAFT,
            expires_at=datetime.now(UTC) + timedelta(hours=DRAFT_TTL_HOURS),
        )
        db.add(draft)

        db.add(AuditLog(
            actor="order_builder",
            action="draft_order_created",
            target_kind="draft_order",
            target_id=str(draft.id),
            details={
                "ticker": ticker,
                "side": side.value,
                "qty": str(quantity),
                "entry": str(entry),
                "stop": str(stop),
                "target": str(target),
                "rr": rr,
                "risk_usd": str(dollar_risk),
                "score": score,
            },
        ))
        await db.commit()
        return draft


# Backward-compat alias
BracketBuilder = OrderBuilder


async def confirm_draft(draft_id: uuid.UUID, actor: str, db: AsyncSession) -> str | None:
    """Send a draft to the broker. Returns broker_order_id or None on failure.

    Idempotency: client_order_id = draft.id. If submission fails partway through,
    re-running confirm() returns the same broker order rather than duplicating.
    """
    draft = (await db.execute(select(DraftOrder).where(DraftOrder.id == draft_id))).scalar_one_or_none()
    if draft is None:
        return None
    if draft.status == OrderStatus.SENT and draft.broker_order_id:
        return draft.broker_order_id  # idempotent
    if draft.status != OrderStatus.DRAFT:
        logger.warning("confirm_wrong_state", draft_id=str(draft_id), status=draft.status.value)
        return None
    if draft.expires_at and datetime.now(UTC) > draft.expires_at:
        draft.status = OrderStatus.EXPIRED
        await db.commit()
        return None

    spec = OrderBuilder.spec_from_draft(draft)
    # Inject client_order_id for broker-side dedupe.
    spec.client_order_id = str(draft.id)

    broker = get_broker()
    try:
        await broker.connect()
        order = await broker.submit_bracket(spec)
    except BrokerError as e:
        logger.error("confirm_failed", draft_id=str(draft_id), error=str(e))
        draft.status = OrderStatus.REJECTED
        draft.skip_reason = f"broker error: {e}"
        await db.commit()
        return None
    finally:
        await broker.disconnect()

    draft.status = OrderStatus.SENT
    draft.broker_order_id = order.broker_order_id
    draft.confirmed_at = datetime.now(UTC)

    db.add(AuditLog(
        actor=actor,
        action="order_confirmed",
        target_kind="draft_order",
        target_id=str(draft.id),
        details={"broker_order_id": order.broker_order_id, "broker": broker.name},
    ))
    await db.commit()
    return order.broker_order_id


async def skip_draft(draft_id: uuid.UUID, actor: str, reason: str | None, db: AsyncSession) -> bool:
    draft = (await db.execute(select(DraftOrder).where(DraftOrder.id == draft_id))).scalar_one_or_none()
    if draft is None or draft.status != OrderStatus.DRAFT:
        return False
    draft.status = OrderStatus.SKIPPED
    draft.skip_reason = reason
    db.add(AuditLog(
        actor=actor,
        action="order_skipped",
        target_kind="draft_order",
        target_id=str(draft.id),
        details={"reason": reason or ""},
    ))
    await db.commit()
    return True

# Compatibility alias — older code uses BracketBuilder
BracketBuilder = OrderBuilder
