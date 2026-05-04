"""Discord integration — webhooks for posts, bot for slash commands."""
from src.discord_bot.webhooks import (
    post_firehose,
    post_high_score,
    post_position_alert,
    post_system_health,
)

__all__ = [
    "post_firehose",
    "post_high_score",
    "post_position_alert",
    "post_system_health",
]
