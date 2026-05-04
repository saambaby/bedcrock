# Deployment

The system is designed to run on a small VPS (1–2 vCPU, 2 GB RAM is plenty).
Postgres and four Python workers run there. Your laptop does not need to be
on for the system to work.

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
    │   │ ct-api          │ ←── FastAPI :8080
    │   └─────────────────┘         │
    │   ┌─────────────────┐         │
    │   │ Syncthing       │ ── sync vault folder ──→ laptop / phone
    │   └─────────────────┘         │
    └───────────────────────────────┘
                    │
                Discord (webhooks + bot gateway)
                    │
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
#   VAULT_PATH    (absolute path; we'll create it next)
#   ALPACA_API_KEY / ALPACA_API_SECRET
#   QUIVER_API_KEY  UNUSUAL_WHALES_API_KEY  FINNHUB_API_KEY
#   SEC_USER_AGENT (your name + email — required by SEC)
#   DISCORD_WEBHOOK_*  DISCORD_BOT_TOKEN  DISCORD_GUILD_ID
#   API_SIGNING_SECRET (generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))')
```

Create the vault directory:

```bash
mkdir -p /home/bedcrock/vault/Trading
cp -r vault-templates/* /home/bedcrock/vault/Trading/
```

### 6. Database migration

```bash
alembic upgrade head
```

### 7. Smoke test

```bash
# Verify config loads + DB reachable
python -c "from src.config import settings; print(settings.mode, settings.vault_path)"

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

## Syncthing — vault to laptop

On the VPS:

```bash
sudo apt install -y syncthing
sudo systemctl enable --now syncthing@bedcrock
# Tunnel port 8384 to your laptop to reach the Web UI:
ssh -L 8384:localhost:8384 bedcrock@vps
# Then open http://localhost:8384
```

Add `/home/bedcrock/vault/Trading/` as a shared folder. On your laptop,
install Syncthing and accept the share. Point Obsidian at the synced
folder. The Cowork desktop app can read directly from the same folder.

**Conflict policy:** Syncthing will create `<file>.sync-conflict-...` if
both sides edit the same file. The backend only writes to `00 Inbox/`;
Cowork only writes outside the inbox. The conflict surface is small.

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

## Backup

The vault is the source of truth — Syncthing already gives you 2 copies
(VPS + laptop). For DB backup:

```bash
# Daily cron on the VPS:
0 4 * * * sudo -u postgres pg_dump bedcrock | gzip > /backups/bedcrock-$(date +%F).sql.gz
```

The DB can be rebuilt from the vault if needed (rehydrate worker — TODO v0.2).

## Troubleshooting

- **Healthcheck FAIL on an ingestor:** check `/var/log/bedcrock/<name>.log`
  via `journalctl`. Most common: stale API key or rate limit.
- **No Discord posts:** verify webhooks via `curl -X POST -H 'Content-Type: application/json' -d '{"content":"test"}' <webhook_url>`.
- **Drafts never confirm:** check `ct-bot` is running and the bot is in your
  guild. `/heartbeat` slash command is the simplest probe.
- **Vault not syncing:** Syncthing Web UI shows per-folder sync status. The
  most common issue is a `.stignore` mistakenly matching `.md`.
