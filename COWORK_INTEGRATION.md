# Cowork Integration

This system is designed for **human ↔ Claude Cowork** collaboration over a
shared Obsidian vault. The backend writes raw observations; Cowork reasons
over them on a schedule; you review and act through Discord.

## How it fits together

```
[ingestors] → [DB cache] → [vault writer] → 00 Inbox/*.md
                                                  │
                                                  ▼
                                       [Cowork scheduled task]
                                                  │
                                                  ▼
                                  01 Watchlist / 02 Open / 03 Closed
                                                  │
                                                  ▼
                                              you (mobile)
                                                  │
                                                  ▼
                                       Discord /confirm /skip
                                                  │
                                                  ▼
                                            [broker]
```

The backend NEVER writes outside `00 Inbox/` and `02 Open Positions/`.
Cowork OWNS the rest of the vault. This is the source-of-truth boundary.

## Cowork desktop setup

Cowork is the file-aware desktop product. It reads and writes local files
with permission you grant it.

### 1. Install Cowork

Get it from <https://www.anthropic.com/claude/cowork>. It's a desktop app
for macOS / Windows / Linux.

### 2. Point it at the synced vault

In Cowork, grant access to the folder where Syncthing mirrors the VPS
vault — typically `~/Obsidian/Trading/` or similar. Cowork can now read
and write the .md files directly.

### 3. Schedule the four tasks

Cowork supports scheduled / recurring tasks. Create four:

| Task name           | Schedule                       | Prompt file                        |
|---------------------|--------------------------------|------------------------------------|
| Morning Heavy       | Mon–Fri 06:30 ET               | `cowork-prompts/morning-heavy.md`   |
| Intraday Light A    | Mon–Fri 12:00 ET               | `cowork-prompts/intraday-light.md`  |
| Intraday Light B    | Mon–Fri 14:00 ET               | `cowork-prompts/intraday-light.md`  |
| Hourly Closure      | Every hour 10:00–16:00 ET, M–F | `cowork-prompts/hourly-closure.md`  |
| Weekly Synthesis    | Sunday 19:00 ET                | `cowork-prompts/weekly-synthesis.md`|

Paste the entire content of each prompt file into Cowork's task config.
The prompts already specify which folders Cowork should read and write.

### 4. Verify with a dry run

Before turning on the schedules:

```bash
# On the VPS, manually create one inbox file:
cat > /home/bedcrock/vault/Trading/00\ Inbox/test-signal.md << 'EOF'
---
type: signal
status: new
ticker: AAPL
source: manual
action: buy
disclosed_at: 2026-05-03T12:00:00Z
score: 7.0
---
# AAPL — manual test
EOF
```

Wait for Syncthing to sync to your laptop. Run the morning prompt manually
in Cowork. Confirm:

- It reads the test-signal.md
- It creates or updates `01 Watchlist/AAPL.md`
- It writes `00 Inbox/<today>-morning-summary.md`

If that works, enable the schedules.

## What Cowork should NOT do

Cowork is the analyst, not the trader. It must NEVER:

- Write to `02 Open Positions/` (that's the live monitor's territory)
- Edit anything in `99-Meta/scoring-rules.md` directly (proposals only)
- Send Discord messages directly (the backend does that)
- Submit broker orders (you do that with `/confirm`)

The prompt files reinforce this. If you customize the prompts, keep these
lanes clear.

## What you do

Daily, on phone:

1. Glance at #high-score in Discord — any drafts to confirm or skip?
2. Open the synced vault in Obsidian — read the morning summary
3. Tap `/confirm <id>` or `/skip <id> reason: ...` for any drafts
4. (Optional) Open `Dashboard.md` in Obsidian for at-a-glance dataview

Weekly, on laptop:

1. Read Sunday's weekly synthesis proposal in `00 Inbox/`
2. Decide which proposed changes to adopt
3. Edit `99-Meta/scoring-rules.md` and `99-Meta/risk-limits.md` accordingly
4. Mark the synthesis file `status: processed`

The backend re-reads the meta files on every scoring pass, so changes take
effect within ~15 minutes.

## Tuning the prompts

The prompts are starting points. Things to adjust as you learn what works:

- **Scope**: in the morning run, are you triaging too many or too few signals?
  Cap by score threshold or by source.
- **Pattern vocabulary**: the hourly closure prompt mentions a fixed list of
  pattern labels. Add patterns that recur in your trading; remove ones you
  never see.
- **Proposal aggressiveness**: if the weekly synthesis keeps proposing weight
  changes that you reject, soften the language: "propose only changes you'd
  bet on with 70%+ confidence."

Keep changes in the changelog (`99-Meta/changelog.md`) so the synthesis run
can see prior decisions.
