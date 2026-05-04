"""Discord webhook posters.

Three channels:
  - #signals-firehose    every raw signal (browse-only)
  - #high-score          signals above threshold (the channel you watch)
  - #position-alerts     entries, closes, urgent events
  - #system-health       per-ingestor heartbeats, errors

Webhooks are fire-and-forget — no auth, just POST to the URL Discord gave
you when you created the webhook.

For interactive features (slash commands), see src/discord_bot/bot.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from src.config import settings
from src.logging_config import get_logger

logger = get_logger(__name__)

# Threshold above which a signal is interesting enough to surface in #high-score
# and to trigger draft-order construction. Tunable via 99-Meta/scoring-rules.md.
HIGH_SCORE_THRESHOLD = 6.0

# Discord embed color palette
COLOR_INFO = 0x6366F1      # indigo
COLOR_SUCCESS = 0x22C55E   # green
COLOR_WARN = 0xF59E0B      # amber
COLOR_ERROR = 0xEF4444     # red
COLOR_NEUTRAL = 0x64748B   # slate


async def _post(url: str, payload: dict[str, Any]) -> None:
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                logger.warning("discord_webhook_status", status=r.status_code, body=r.text[:200])
    except Exception as e:
        logger.warning("discord_webhook_failed", error=str(e))


def _embed(
    title: str,
    description: str | None = None,
    color: int = COLOR_INFO,
    fields: list[dict[str, Any]] | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    e: dict[str, Any] = {"title": title[:256], "color": color}
    if description:
        e["description"] = description[:4096]
    if fields:
        e["fields"] = fields[:25]
    if url:
        e["url"] = url
    return e


# ---- Firehose: every signal, browse-only ----

async def post_firehose(
    *,
    ticker: str,
    action: str,
    source: str,
    score: float | None = None,
    trader: str | None = None,
    gate_blocked: bool = False,
) -> None:
    color = COLOR_NEUTRAL if not gate_blocked else COLOR_WARN
    desc = f"Source: `{source}`"
    if trader:
        desc += f" via {trader}"
    if score is not None:
        desc += f"\nScore: **{score:.2f}**"
    if gate_blocked:
        desc += " ⛔ blocked"
    payload = {"embeds": [_embed(title=f"{ticker} — {action.upper()}", description=desc, color=color)]}
    await _post(settings.discord_webhook_firehose, payload)


# Backward-compatible name for callers that prefer it
post_firehose_signal = post_firehose


# ---- High score: actionable, with breakdown and draft id ----

async def post_high_score(
    *,
    ticker: str,
    action: str,
    source: str,
    score: float,
    trader: str | None = None,
    breakdown: dict[str, float] | None = None,
    draft_id: str | None = None,
) -> None:
    side_emoji = "🟢" if action.lower() == "buy" else "🔴"
    color = COLOR_SUCCESS if action.lower() == "buy" else COLOR_ERROR
    fields: list[dict[str, Any]] = [
        {"name": "Side", "value": f"{side_emoji} {action.upper()}", "inline": True},
        {"name": "Score", "value": f"{score:.2f}", "inline": True},
        {"name": "Source", "value": source, "inline": True},
    ]
    if trader:
        fields.append({"name": "Trader", "value": trader, "inline": True})
    if breakdown:
        for k, v in breakdown.items():
            if abs(float(v or 0)) >= 0.01:
                fields.append({"name": k, "value": f"{v:+.2f}", "inline": True})

    description = ""
    if draft_id:
        description = (
            f"**Confirm:** `/confirm {draft_id}`\n"
            f"**Skip:** `/skip {draft_id} reason: <why>`"
        )

    payload = {
        "embeds": [
            _embed(
                title=f"⭐ {ticker} — score {score:.2f}",
                description=description,
                color=color,
                fields=fields[:25],
            )
        ]
    }
    await _post(settings.discord_webhook_high_score, payload)


# ---- Position alerts ----

async def post_position_alert(
    *,
    title: str,
    description: str,
    color: int = COLOR_INFO,
) -> None:
    payload = {"embeds": [_embed(title=title, description=description, color=color)]}
    await _post(settings.discord_webhook_positions, payload)


# ---- Draft order embed (used by tests / direct call paths) ----

async def post_draft_order(
    *,
    draft_id: str,
    ticker: str,
    side: str,
    qty: float,
    entry: float,
    stop: float,
    target: float,
    rr: float,
    risk_pct: float,
    score: float | None,
    setup: str | None,
) -> None:
    side_emoji = "🟢" if side.lower() == "buy" else "🔴"
    color = COLOR_SUCCESS if side.lower() == "buy" else COLOR_ERROR
    fields = [
        {"name": "Side", "value": f"{side_emoji} {side.upper()}", "inline": True},
        {"name": "Quantity", "value": f"{qty:g}", "inline": True},
        {"name": "Score", "value": f"{score:.2f}" if score is not None else "—", "inline": True},
        {"name": "Entry", "value": f"${entry:.2f}", "inline": True},
        {"name": "Stop", "value": f"${stop:.2f}", "inline": True},
        {"name": "Target", "value": f"${target:.2f}", "inline": True},
        {"name": "R:R", "value": f"{rr:.2f}", "inline": True},
        {"name": "Risk", "value": f"{risk_pct:.2f}% of equity", "inline": True},
        {"name": "Setup", "value": setup or "none", "inline": True},
    ]
    description = (
        f"**Confirm:** `/confirm {draft_id}`\n"
        f"**Skip:** `/skip {draft_id} reason: <why>`\n"
    )
    payload = {
        "embeds": [
            _embed(
                title=f"📋 DRAFT — {ticker}",
                description=description,
                color=color,
                fields=fields,
            )
        ]
    }
    await _post(settings.discord_webhook_high_score, payload)


# ---- System health ----

async def post_system_health(
    *,
    title: str,
    body: str | None = None,
    description: str | None = None,
    ok: bool = True,
    color: int | None = None,
) -> None:
    if color is None:
        color = COLOR_INFO if ok else COLOR_ERROR
    desc = body if body is not None else description
    payload = {"embeds": [_embed(title=title, description=desc, color=color)]}
    await _post(settings.discord_webhook_system_health, payload)


def fire_and_forget(coro):
    """Schedule a coroutine without awaiting it. Use sparingly."""
    asyncio.get_event_loop().create_task(coro)
