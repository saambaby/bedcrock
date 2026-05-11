"""Discord slash-command bot.

Commands:
  /confirm <draft_id>    — send a draft bracket to the broker
  /skip <draft_id> [reason] — discard a draft
  /positions             — list open positions
  /pnl [period]          — equity & P&L summary
  /snooze <ticker> <days> [reason] — temporarily ignore signals on a ticker
  /thesis <position_id>  — show the latest analyst thesis for an open position

The bot defers heavy work to the API layer (calls /confirm, /skip endpoints
internally). This keeps Discord responses snappy and centralises broker
interaction in one place.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import discord
import httpx
from discord import app_commands
from sqlalchemy import select

from src.config import settings
from src.db.models import EquitySnapshot, Position, PositionStatus, Snooze
from src.db.session import SessionLocal, dispose
from src.logging_config import get_logger

logger = get_logger(__name__)

API_INTERNAL_URL = f"http://{settings.api_host}:{settings.api_port}"


class CTBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        if settings.discord_guild_id:
            guild = discord.Object(id=settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        logger.info("discord_bot_ready", user=str(self.user))


bot = CTBot()


# ---------- /confirm ----------

@bot.tree.command(name="confirm", description="Send a draft order to the broker")
async def cmd_confirm(interaction: discord.Interaction, draft_id: str) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        UUID(draft_id)
    except ValueError:
        await interaction.followup.send(f"❌ Invalid draft ID: `{draft_id}`", ephemeral=True)
        return

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(f"{API_INTERNAL_URL}/confirm/{draft_id}")
        if resp.status_code == 200:
            data = resp.json()
            await interaction.followup.send(
                f"✅ **{data.get('ticker')}** sent to broker — order `{data.get('broker_order_id')}`",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"❌ API returned {resp.status_code}: `{resp.text[:200]}`",
                ephemeral=True,
            )
    except Exception as e:
        await interaction.followup.send(f"❌ {type(e).__name__}: {e}", ephemeral=True)


# ---------- /skip ----------

@bot.tree.command(name="skip", description="Skip a draft order")
async def cmd_skip(
    interaction: discord.Interaction, draft_id: str, reason: str = ""
) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        UUID(draft_id)
    except ValueError:
        await interaction.followup.send("❌ Invalid draft ID", ephemeral=True)
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(
                f"{API_INTERNAL_URL}/skip/{draft_id}",
                json={"reason": reason},
            )
        if resp.status_code == 200:
            await interaction.followup.send("⏭ Skipped", ephemeral=True)
        else:
            await interaction.followup.send(
                f"❌ {resp.status_code}: {resp.text[:200]}", ephemeral=True
            )
    except Exception as e:
        await interaction.followup.send(f"❌ {e}", ephemeral=True)


# ---------- /positions ----------

@bot.tree.command(name="positions", description="List open positions")
async def cmd_positions(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    async with SessionLocal() as db:
        stmt = select(Position).where(
            Position.mode == settings.mode,
            Position.status == PositionStatus.OPEN,
        ).order_by(Position.entry_at.desc())
        rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        await interaction.followup.send("No open positions.", ephemeral=True)
        return
    lines = [
        f"**{p.ticker}** {p.side.value} {p.quantity} @ ${p.entry_price} "
        f"(stop ${p.stop} / target ${p.target}) — opened {p.entry_at.date().isoformat()}"
        for p in rows
    ]
    await interaction.followup.send("\n".join(lines), ephemeral=True)


# ---------- /pnl ----------

@bot.tree.command(name="pnl", description="Equity and P&L summary")
async def cmd_pnl(interaction: discord.Interaction, days: int = 30) -> None:
    await interaction.response.defer(ephemeral=True)
    async with SessionLocal() as db:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = (
            select(EquitySnapshot)
            .where(
                EquitySnapshot.mode == settings.mode,
                EquitySnapshot.snapshot_date >= cutoff,
            )
            .order_by(EquitySnapshot.snapshot_date)
        )
        snaps = (await db.execute(stmt)).scalars().all()

    if not snaps:
        await interaction.followup.send("No equity history yet.", ephemeral=True)
        return

    first = snaps[0]
    last = snaps[-1]
    total_pnl = last.equity - first.equity
    total_pct = (total_pnl / first.equity * 100) if first.equity else 0
    msg = (
        f"**Mode:** {settings.mode.value}\n"
        f"**Period:** {first.snapshot_date.date()} → {last.snapshot_date.date()} ({days}d window)\n"
        f"**Equity:** ${last.equity}\n"
        f"**P&L:** ${total_pnl} ({total_pct:.2f}%)"
    )
    await interaction.followup.send(msg, ephemeral=True)


# ---------- /snooze ----------

@bot.tree.command(name="snooze", description="Ignore signals on a ticker for N days")
async def cmd_snooze(
    interaction: discord.Interaction, ticker: str, days: int, reason: str = ""
) -> None:
    await interaction.response.defer(ephemeral=True)
    until = datetime.now(UTC) + timedelta(days=days)
    async with SessionLocal() as db:
        existing_stmt = select(Snooze).where(Snooze.ticker == ticker.upper())
        existing = (await db.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            existing.snoozed_until = until
            existing.reason = reason
        else:
            db.add(Snooze(ticker=ticker.upper(), snoozed_until=until, reason=reason))
        await db.commit()
    await interaction.followup.send(
        f"💤 Snoozed **{ticker.upper()}** until {until.date().isoformat()}",
        ephemeral=True,
    )


# ---------- entry point ----------

async def run() -> None:
    token = settings.discord_bot_token.get_secret_value()
    if not token:
        logger.error("discord_bot_token_missing")
        return
    try:
        await bot.start(token)
    finally:
        await dispose()


if __name__ == "__main__":
    asyncio.run(run())


async def run() -> None:
    """Entry point for the bot worker. Reads token from settings and runs the client."""
    from src.config import settings
    token = settings.discord_bot_token.get_secret_value()
    if not token:
        logger.warning("discord_bot_no_token")
        return
    await bot.start(token)
