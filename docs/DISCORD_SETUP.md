# Discord Setup

You'll have:
- 4 webhooks (one per channel) for posting messages
- 1 bot user for slash commands

## Step 1 — Create a Discord server

If you don't already have one. Mobile or desktop, doesn't matter.

## Step 2 — Create channels

Create four text channels:

- `#signals-firehose` — every signal, browse-only
- `#high-score` — score ≥ 6, where draft orders get posted
- `#position-alerts` — entries, exits, urgent
- `#system-health` — heartbeats, errors

## Step 3 — Webhooks (per channel)

For each channel:

1. Right-click → Edit Channel → Integrations → Webhooks
2. New Webhook → name it `bedcrock-<channel>`
3. Copy the Webhook URL
4. Paste into `.env`:

```ini
DISCORD_WEBHOOK_FIREHOSE=https://discord.com/api/webhooks/...
DISCORD_WEBHOOK_HIGH_SCORE=https://discord.com/api/webhooks/...
DISCORD_WEBHOOK_POSITIONS=https://discord.com/api/webhooks/...
DISCORD_WEBHOOK_SYSTEM_HEALTH=https://discord.com/api/webhooks/...
```

## Step 4 — Create the bot

For slash commands (`/confirm`, `/skip`, `/positions`, etc.):

1. Open <https://discord.com/developers/applications>
2. **New Application** → name it `bedcrock-bot`
3. **Bot** tab → **Reset Token** → copy the token
4. Paste into `.env`:

   ```ini
   DISCORD_BOT_TOKEN=<token>
   ```

5. **Bot** tab → enable **Message Content Intent** (optional; not needed for
   slash commands but useful if you add text features later)
6. **OAuth2 → URL Generator** →
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Messages/View Channels`
7. Open the generated URL → invite the bot to your server

## Step 5 — Get your guild ID

Discord → User Settings → Advanced → enable **Developer Mode**.
Then right-click your server icon → **Copy Server ID**. Paste into `.env`:

```ini
DISCORD_GUILD_ID=123456789012345678
```

(If you skip this, slash commands register globally and take ~1 hour to
propagate. With a guild ID they're available immediately in your server.)

## Step 6 — Start the bot

On the VPS:

```bash
sudo systemctl start ct-bot
journalctl -u ct-bot -f
```

You should see `discord_bot_synced` in the logs. In your server, type `/`
and the commands should show up.

## Slash commands reference

| Command       | Args                       | What it does                                  |
|---------------|----------------------------|-----------------------------------------------|
| `/confirm`    | `draft_id`                 | Send draft order to broker                    |
| `/skip`       | `draft_id [reason]`        | Mark draft as skipped                         |
| `/positions`  | —                          | List open positions                           |
| `/pnl`        | —                          | Today's P&L summary + 7d rolling              |
| `/thesis`     | `ticker`                   | Show the watchlist note for a ticker          |
| `/snooze`     | `ticker duration`          | Block ticker from triggering (e.g. `7d`, `24h`)|
| `/heartbeat`  | —                          | Show ingestor heartbeats                      |

## Mobile workflow

The whole point of using Discord is **one tap on your phone**. The high-score
embed includes the draft ID — long-press to copy, then paste into `/confirm`.
Or use the **signed deep links** the API generates (see `docs/DEPLOYMENT.md`)
which let you tap a link in the embed to confirm without typing.

## Troubleshooting

- **Slash commands don't appear:** verify the bot has `applications.commands`
  scope. Re-invite if needed. Wait up to 1 hour for global registration.
- **Webhook returns 401:** the URL has been deleted/regenerated. Get a new URL
  in channel settings.
- **Bot offline in member list:** `ct-bot` service isn't running; check
  `journalctl -u ct-bot`.
- **Commands run but nothing happens:** check `ct-bot` logs and the FastAPI
  service. Confirm/skip flows through the bot directly (not through the API
  unless you're using deep links).
