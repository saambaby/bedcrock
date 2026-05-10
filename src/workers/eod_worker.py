"""End-of-session worker.

Runs once per US trading day, after the close (default 16:30 ET):

  1. Fetch broker account snapshot (equity, cash, positions value)
  2. Compute daily P&L delta from yesterday's snapshot
  3. Persist EquitySnapshot row
  4. Compute SPY benchmark return (for that day) — for relative comparison
  5. Write 05 Daily/{YYYY-MM-DD}.md with positions + P&L + signal cluster
  6. Post #system-health summary to Discord

Cron: 30 16 * * 1-5 (4:30 PM ET, weekdays). Or run via `python -m src.workers.eod_worker`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.backtest.replay import ReplayReport, replay
from src.broker import get_broker
from src.config import settings
from src.db.models import (
    EquitySnapshot,
    Position,
    PositionStatus,
    Signal,
)
from src.db.session import SessionLocal, dispose
from src.discord_bot.webhooks import COLOR_INFO, post_system_health
from src.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


async def main() -> None:
    configure_logging()
    today = datetime.now(UTC).date()
    logger.info("eod_worker_starting", date=today.isoformat(), mode=settings.mode.value)

    broker = get_broker()
    try:
        await broker.connect()
        account = await broker.get_account()
    except Exception as e:
        logger.error("eod_get_account_failed", error=str(e))
        await broker.disconnect()
        await dispose()
        return
    finally:
        await broker.disconnect()

    today_dt = datetime.combine(today, datetime.min.time(), tzinfo=UTC)

    async with SessionLocal() as db:
        # Look up yesterday's snapshot for delta
        prev_stmt = (
            select(EquitySnapshot)
            .where(
                EquitySnapshot.mode == settings.mode,
                EquitySnapshot.snapshot_date < today_dt,
            )
            .order_by(EquitySnapshot.snapshot_date.desc())
            .limit(1)
        )
        prev = (await db.execute(prev_stmt)).scalar_one_or_none()

        if prev is not None:
            daily_pnl = account.equity - prev.equity
            daily_pnl_pct = (daily_pnl / prev.equity * 100) if prev.equity > 0 else Decimal("0")
        else:
            daily_pnl = Decimal("0")
            daily_pnl_pct = Decimal("0")

        # Upsert today's snapshot
        stmt = pg_insert(EquitySnapshot).values(
            mode=settings.mode,
            snapshot_date=today_dt,
            equity=account.equity,
            cash=account.cash,
            positions_value=account.positions_value,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
        ).on_conflict_do_update(
            index_elements=["mode", "snapshot_date"],
            set_={
                "equity": account.equity,
                "cash": account.cash,
                "positions_value": account.positions_value,
                "daily_pnl": daily_pnl,
                "daily_pnl_pct": daily_pnl_pct,
            },
        )
        await db.execute(stmt)
        await db.commit()

        # Pull today's open positions and signals for the daily note
        open_positions = (await db.execute(
            select(Position).where(
                Position.status == PositionStatus.OPEN,
                Position.mode == settings.mode,
            )
        )).scalars().all()

        # Signals from today (UTC day)
        today_signals = (await db.execute(
            select(Signal).where(
                Signal.disclosed_at >= today_dt,
            )
        )).scalars().all()

    write_daily_note(
        date=today,
        equity=account.equity,
        cash=account.cash,
        daily_pnl=daily_pnl,
        daily_pnl_pct=daily_pnl_pct,
        open_positions=open_positions,
        signal_count=len(today_signals),
    )

    color = COLOR_INFO
    desc = (
        f"**Equity:** ${account.equity:,.2f}\n"
        f"**Daily P&L:** ${daily_pnl:,.2f} ({daily_pnl_pct:.2f}%)\n"
        f"**Open positions:** {len(open_positions)}\n"
        f"**New signals today:** {len(today_signals)}\n"
    )
    await post_system_health(
        title=f"📊 EOD — {today.isoformat()} ({settings.mode.value})",
        description=desc,
        color=color,
    )

    # Sunday 17:00 ET (UTC weekday 6) — run mini-backtester replay against any
    # proposed scoring-rule weight sets in the vault, write reports for the
    # weekly synthesis to consume.
    if datetime.now(UTC).weekday() == 6:
        try:
            await run_weekly_replay(today)
        except Exception as e:
            logger.error("weekly_replay_failed", error=str(e))

    await dispose()
    logger.info("eod_worker_done")


async def run_weekly_replay(today) -> None:
    """Read 99 Meta/scoring-rules-proposed.md, run replay() per proposal,
    write 06 Weekly/{date}-replay-{rule_name}.md for each."""
    proposals_path = settings.vault_path / "99 Meta" / "scoring-rules-proposed.md"
    if not proposals_path.exists():
        logger.info("weekly_replay_no_proposals", path=str(proposals_path))
        return

    proposals = _parse_proposed_rules(proposals_path)
    if not proposals:
        logger.info("weekly_replay_empty_proposals")
        return

    out_dir = settings.vault_path / "06 Weekly"
    out_dir.mkdir(parents=True, exist_ok=True)

    async with SessionLocal() as db:
        for rule_name, weights in proposals.items():
            logger.info("weekly_replay_running", rule=rule_name)
            report = await replay(db, weights)
            out_path = out_dir / f"{today.isoformat()}-replay-{rule_name}.md"
            out_path.write_text(_format_replay_note(rule_name, weights, report), encoding="utf-8")
            logger.info(
                "weekly_replay_written",
                rule=rule_name,
                path=str(out_path),
                recommendation=report.recommendation,
            )


def _parse_proposed_rules(path: Path) -> dict[str, dict]:
    """Parse the proposals markdown file. Supports either:

    1. A YAML frontmatter block with `proposals: {name: {weights}}`, or
    2. One or more fenced ```yaml blocks, each containing
       `name: <rule_name>` and `weights: {...}`.

    Returns {rule_name: weights_dict}. Empty dict if nothing parseable.
    """
    text = path.read_text(encoding="utf-8")
    proposals: dict[str, dict] = {}

    # Frontmatter form
    if text.startswith("---\n"):
        try:
            _, fm, _ = text.split("---\n", 2)
            data = yaml.safe_load(fm) or {}
            for name, cfg in (data.get("proposals") or {}).items():
                weights = cfg.get("weights") if isinstance(cfg, dict) else None
                if isinstance(weights, dict):
                    proposals[str(name)] = {k: float(v) for k, v in weights.items()}
        except Exception as e:
            logger.warning("weekly_replay_frontmatter_parse_failed", error=str(e))

    # Fenced YAML blocks
    in_block = False
    buf: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("```yaml") or line.strip() == "```yml":
            in_block = True
            buf = []
            continue
        if in_block and line.strip() == "```":
            in_block = False
            try:
                data = yaml.safe_load("\n".join(buf)) or {}
                name = data.get("name")
                weights = data.get("weights")
                if name and isinstance(weights, dict):
                    proposals[str(name)] = {k: float(v) for k, v in weights.items()}
            except Exception as e:
                logger.warning("weekly_replay_block_parse_failed", error=str(e))
            continue
        if in_block:
            buf.append(line)

    return proposals


def _format_replay_note(rule_name: str, weights: dict, report: ReplayReport) -> str:
    fm = {
        "type": "replay-report",
        "rule_name": rule_name,
        "recommendation": report.recommendation,
        "in_sample_sharpe": report.in_sample_sharpe,
        "out_of_sample_sharpe": report.out_of_sample_sharpe,
        "sharpe_delta_vs_baseline": report.sharpe_delta_vs_baseline,
        "n_trades_simulated": report.n_trades_simulated,
        "win_rate": report.win_rate,
        "profit_factor": report.profit_factor,
        "total_return_pct": report.total_return_pct,
    }
    body = (
        f"# Replay — {rule_name}\n\n"
        f"**Recommendation:** {report.recommendation}\n\n"
        f"## Proposed weights\n\n```yaml\n"
        f"{yaml.safe_dump(weights, sort_keys=False)}```\n\n"
        f"## Metrics\n\n"
        f"- Signals in scope: {report.n_signals_in_scope}\n"
        f"- Above threshold (proposed): {report.n_signals_above_threshold}\n"
        f"- Trades simulated: {report.n_trades_simulated}\n"
        f"- In-sample Sharpe: {report.in_sample_sharpe:.3f}\n"
        f"- Out-of-sample Sharpe: {report.out_of_sample_sharpe:.3f}\n"
        f"- Sharpe Δ vs baseline: {report.sharpe_delta_vs_baseline:+.3f}\n"
        f"- Win rate (OOS): {report.win_rate:.1%}\n"
        f"- Profit factor (OOS): {report.profit_factor:.2f}\n"
        f"- Total return % (OOS, qty=1): {report.total_return_pct:+.2f}\n\n"
        f"_Advisory only. See `src/backtest/replay.py` module docstring for caveats._\n"
    )
    return "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body


def write_daily_note(
    *,
    date,
    equity: Decimal,
    cash: Decimal,
    daily_pnl: Decimal,
    daily_pnl_pct: Decimal,
    open_positions: list,
    signal_count: int,
) -> Path:
    """Write the 05 Daily/{date}.md note. Cowork's morning prompt reads this."""
    base = settings.vault_path / "05 Daily"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{date.isoformat()}.md"

    fm = {
        "type": "daily",
        "mode": settings.mode.value,
        "date": date.isoformat(),
        "equity": float(equity),
        "cash": float(cash),
        "daily_pnl": float(daily_pnl),
        "daily_pnl_pct": float(daily_pnl_pct),
        "open_positions_count": len(open_positions),
        "signal_count": signal_count,
    }
    body = (
        f"# {date.isoformat()} — Daily ({settings.mode.value})\n\n"
        f"**Equity:** ${equity:,.2f}\n"
        f"**Daily P&L:** ${daily_pnl:,.2f} ({daily_pnl_pct:.2f}%)\n\n"
        f"## Open Positions ({len(open_positions)})\n\n"
    )
    for p in open_positions:
        body += f"- [[02 Open Positions/{p.ticker}-{p.entry_at.strftime('%Y-%m-%d')}|{p.ticker}]] — {p.side.value} @ ${p.entry_price}\n"
    body += f"\n## Signals today: {signal_count}\n"

    path.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body,
        encoding="utf-8",
    )
    return path


async def write_start_of_day_snapshot() -> None:
    """Write a pre-open `EquitySnapshot` for today so `update_daily_pnl`
    has a baseline.

    Idempotent: if a snapshot already exists for today (e.g. EOD ran for
    a prior session and rolled forward, or this was already invoked),
    it is left untouched. Schedule via cron at ~09:25 ET on weekdays:

        25 9 * * 1-5  python -m src.workers.eod_worker --sod
    """
    configure_logging()
    today = datetime.now(UTC).date()
    today_dt = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    logger.info("sod_snapshot_starting", date=today.isoformat(), mode=settings.mode.value)

    broker = get_broker()
    try:
        await broker.connect()
        account = await broker.get_account()
    except Exception as e:
        logger.error("sod_snapshot_get_account_failed", error=str(e))
        await broker.disconnect()
        await dispose()
        return
    finally:
        await broker.disconnect()

    async with SessionLocal() as db:
        existing = (await db.execute(
            select(EquitySnapshot).where(
                EquitySnapshot.mode == settings.mode,
                EquitySnapshot.snapshot_date == today_dt,
            )
        )).scalar_one_or_none()
        if existing is not None:
            logger.info("sod_snapshot_already_present", date=today.isoformat())
            await dispose()
            return

        stmt = pg_insert(EquitySnapshot).values(
            mode=settings.mode,
            snapshot_date=today_dt,
            equity=account.equity,
            cash=account.cash,
            positions_value=account.positions_value,
            daily_pnl=Decimal("0"),
            daily_pnl_pct=Decimal("0"),
        ).on_conflict_do_nothing(index_elements=["mode", "snapshot_date"])
        await db.execute(stmt)
        await db.commit()

    await dispose()
    logger.info("sod_snapshot_done", equity=str(account.equity))


if __name__ == "__main__":
    import sys
    if "--sod" in sys.argv:
        asyncio.run(write_start_of_day_snapshot())
    else:
        asyncio.run(main())
