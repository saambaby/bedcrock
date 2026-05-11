# Deployment

The system is designed to run on a small VPS (1–2 vCPU, 2 GB RAM is plenty).
Postgres and four Python workers run there. Your laptop does not need to be
on for the system to work. The reasoning layer (Claude Code Routines) runs
cloud-side on Anthropic infrastructure — not on the VPS.

## Reference architecture

```
                 internet
                    │
    ┌───────────────┴───────────────┐
    │   VPS (Linux)                 │
    │   ┌─────────────────┐         │
    │   │ Postgres 16     │         │
    │   └─────────────────┘         │
    │   ┌─────────────────┐         │
    │   │ ct-ingest       │ ←── data sources (HTTP)
    │   │ ct-monitor      │ ←── broker (WebSocket)
    │   │ ct-bot          │ ←── Discord bot gateway
    │   │ ct-api          │ ←── FastAPI :8080 (bearer-auth read layer)
    │   └─────────────────┘         │
    └───────────────┬───────────────┘
                    │
        ┌───────────┴────────────┐
        │                        │
   Discord (webhooks       Claude Code Routines
   + bot gateway)          (cloud-hosted, call the
        │                  FastAPI read layer over
        │                  HTTPS with a bearer token)
        └─── you (mobile)
```

## VPS setup

### 1. Pick a provider

Any of these work fine: Hetzner CX22 (€4/mo), DigitalOcean Basic Droplet
($6/mo), Vultr Cloud Compute ($6/mo), AWS Lightsail ($5/mo). The system is
not latency-sensitive — pick a region close to you for SSH speed; the
upstream APIs are all global.

### 2. Base packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv build-essential \
    postgresql-16 postgresql-client-16 \
    git curl ca-certificates
```

### 3. Postgres

```bash
sudo -u postgres psql <<'SQL'
CREATE USER bedcrock WITH PASSWORD 'change-me-strong-password';
CREATE DATABASE bedcrock OWNER bedcrock;
GRANT ALL PRIVILEGES ON DATABASE bedcrock TO bedcrock;
SQL
```

Update `/etc/postgresql/16/main/pg_hba.conf` to allow local password auth
for the `bedcrock` user, then `sudo systemctl restart postgresql`.

### 4. Application user

```bash
sudo useradd -m -s /bin/bash bedcrock
sudo -u bedcrock -i
git clone <your-repo> /home/bedcrock/bedcrock
cd /home/bedcrock/bedcrock
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 5. Configuration

```bash
cp .env.example .env
# Edit .env — every key documented inline. Required:
#   DATABASE_URL  (must use postgresql+asyncpg://)
#   IBKR_ACCOUNT (your IBKR paper/live account ID)
#   QUIVER_API_KEY  UNUSUAL_WHALES_API_KEY  FINNHUB_API_KEY
#   SEC_USER_AGENT (your name + email — required by SEC)
#   DISCORD_WEBHOOK_*  DISCORD_BOT_TOKEN  DISCORD_GUILD_ID
#   API_SIGNING_SECRET (generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))')
#   API_BEARER_TOKEN   (same generator — used by Claude Code Routines to auth
#                       to the FastAPI read layer; falls back to API_SIGNING_SECRET
#                       if unset)
```

### 6. Database migration

```bash
alembic upgrade head
```

### 7. Smoke test

```bash
# Verify config loads + DB reachable
python -c "from src.config import settings; print(settings.mode)"

# Run one manual ingest pass
python -c "import asyncio; from src.workers.ingest_worker import run_ingestors; asyncio.run(run_ingestors())"

# Healthcheck
python -m src.workers.healthcheck
```

## systemd

Copy unit files and enable:

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ct-ingest ct-monitor ct-bot ct-api
sudo systemctl status ct-ingest
```

Logs:

```bash
journalctl -u ct-ingest -f
journalctl -u ct-monitor -f
```

## Reasoning layer setup (Claude Code Routines)

The five skills in `.claude/skills/` are the reasoning surface of Bedcrock.
They run as **cloud-hosted Claude Code Routines** on Anthropic
infrastructure — not on the VPS — and consume your Claude Pro/Max
subscription, not an API key. The VPS only needs to expose the FastAPI read
layer (port 8080) over HTTPS with a bearer token.

### 1. Install Claude Code locally

On any development machine (your laptop is fine — it does not need to be
running for the Routines to fire), install Claude Code and clone the repo:

```bash
# Install per https://docs.anthropic.com/claude-code
git clone <your-repo> ~/code/bedcrock
cd ~/code/bedcrock
claude
```

The `.claude/skills/` directory in the repo is auto-discovered. Verify with
`/skills` inside the session.

### 2. Schedule each skill as a Routine

From inside the `claude` session, register one Routine per skill via
`/schedule`. Times are in ET; adjust the cron expressions if you live in a
different zone:

```
/schedule "every weekday at 06:30 ET, run /morning-analyze"
/schedule "every weekday at 12:00 ET, run /intraday-check"
/schedule "every weekday at 14:00 ET, run /intraday-check"
/schedule "every weekday hourly from 10:00 to 16:00 ET, run /hourly-closure"
/schedule "every Sunday at 19:00 ET, run /weekly-synthesis"
```

The `status` skill is on-demand only — no schedule.

### 3. Set Routine environment variables

Routine secrets are managed in the Claude Code dashboard at
[claude.ai/code/routines](https://claude.ai/code/routines), not in the repo.
Set the following on each Routine:

| Variable | Value |
|---|---|
| `API_BASE_URL` | Public HTTPS URL of the FastAPI deployment, e.g. `https://api.bedcrock.example.com` |
| `API_BEARER_TOKEN` | Must match `settings.api_bearer_token` on the server (or `api_signing_secret` if the bearer token is unset — the server falls back) |
| `DISCORD_WEBHOOK_HIGH_SCORE` | Webhook for morning/intraday plan embeds |
| `DISCORD_WEBHOOK_POSITIONS` | Webhook for hourly closure decisions |
| `DISCORD_WEBHOOK_SYSTEM_HEALTH` | Webhook for skill failures and weekly synthesis |

### 4. Verify

Before turning the schedules loose, run each skill interactively at least
once:

```
/morning-analyze
```

A successful run posts a single embed to `$DISCORD_WEBHOOK_HIGH_SCORE`. A
failure posts an error to `$DISCORD_WEBHOOK_SYSTEM_HEALTH`. If neither shows
up, check that `API_BASE_URL` is reachable from the public internet and that
the bearer token round-trips against `/healthz`.

## Updating

```bash
sudo systemctl stop ct-ingest ct-monitor ct-bot ct-api
sudo -u bedcrock -i
cd bedcrock
git pull
source .venv/bin/activate
pip install -e .  # picks up new deps
alembic upgrade head
exit
sudo systemctl start ct-ingest ct-monitor ct-bot ct-api
```

Skill changes (anything under `.claude/skills/`) are picked up by Routines
on next invocation — no server restart needed, since Routines pull skill
definitions from your repo.

## Backup

Postgres is the canonical store. Daily dump:

```bash
# Daily cron on the VPS:
0 4 * * * sudo -u postgres pg_dump bedcrock | gzip > /backups/bedcrock-$(date +%F).sql.gz
```

## IB Gateway operational notes

IB Gateway is not designed to be a 24/7 daemon. Running it reliably as a service requires a few non-obvious things — get these wrong and the bot silently goes offline at 23:45 ET every night.

- **Nightly logout window.** IBKR forces a logout for every gateway session between **23:45 and 00:45 ET**. The session must be re-authenticated before the bot can submit orders again. The bot's reconciler tolerates a 5-minute disconnect on reconnect (broker truth wins on conflict — invariant 7), but anything longer needs operator intervention unless IBC handles re-login automatically.
- **IBC is mandatory for headless Linux.** Use [`IbcAlpha/IBC`](https://github.com/IbcAlpha/IBC) to drive the Java UI for login + auto-relogin. There is no truly headless mode for IB Gateway — it always renders a window.
- **Set `AutoRestartTime=23:45` in `IBC/config.ini`.** This causes IBC to re-login right after IBKR's nightly logout. Token-based AutoRestart (the alternative) is **broken on unfunded paper accounts** ([IBC issue #345](https://github.com/IbcAlpha/IBC/issues/345)) — use the time-based variant.
- **Xvfb is required.** IB Gateway needs an X display even in headless deployment. Run under `xvfb-run` or a persistent `Xvfb :1` and `DISPLAY=:1`.
- **Easiest path: Docker.** [`gnzsnz/ib-gateway-docker`](https://github.com/gnzsnz/ib-gateway-docker) bundles IBC + Xvfb + IB Gateway into one image with sane defaults. Recommended for new deployments — saves a day of yak-shaving.
- **Sunday weekly re-auth.** IBKR runs an additional weekly maintenance window starting around **00:01 ET on Sunday**. Same 5-minute reconciler tolerance applies; nothing operator-side to do.

## Troubleshooting

- **Healthcheck FAIL on an ingestor:** check `/var/log/bedcrock/<name>.log`
  via `journalctl`. Most common: stale API key or rate limit.
- **No Discord posts:** verify webhooks via `curl -X POST -H 'Content-Type: application/json' -d '{"content":"test"}' <webhook_url>`.
- **Drafts never confirm:** check `ct-bot` is running and the bot is in your
  guild. `/heartbeat` slash command is the simplest probe.
- **Routine ran but nothing posted:** check the Routine run log at
  claude.ai/code/routines. Most common: `API_BASE_URL` not reachable from
  the public internet, or `API_BEARER_TOKEN` mismatch with the server.
