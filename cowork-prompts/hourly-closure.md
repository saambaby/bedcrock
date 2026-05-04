# Cowork Hourly Closure Run — post-mortem on closed positions

**Schedule:** Every hour during market hours (10:00–16:00 ET).
**Effort budget:** ~2 min per closure.
**Vault root:** `~/Obsidian/Trading/`.

---

You write post-mortems on closed positions. The backend writes a closure
event to `00 Inbox/<date>-<ticker>-closure.md` whenever a stop or target hits.
Your job is to convert that into a `03 Closed/<date>-<ticker>.md` post-mortem
**while the trade is fresh**.

## Step 1 — Find new closures

Read `00 Inbox/*-closure.md` files where `status: new`.

For each one, do steps 2–4.

## Step 2 — Pull context

Read:

- The position's frontmatter (entry price, stop, target, indicators_at_entry,
  source signals, setup_at_entry)
- The watchlist note for the ticker (`01 Watchlist/<TICKER>.md`) — the
  thesis at entry time
- The original signal files (linked via `source_signal_ids`)

## Step 3 — Write the post-mortem

Create `03 Closed/<date>-<ticker>.md` from the closed.md template:

```markdown
---
type: closed
status: closed
ticker: <TICKER>
entry_at:
entry_price:
exit_at:
exit_price:
pnl_usd:
pnl_pct:
close_reason: stop_hit | target_hit | discretionary
quantity:
side:
setup_at_entry:
position_id:
---

# <TICKER> — closed <date>

## Outcome
- Hold: <N>d
- P&L: $<X> (<Y>%)
- Reason: <reason>

## What I expected
(the thesis at entry, in 2-3 sentences. Be honest — what was the actual
expectation, not a sanitized version.)

## What happened
(the actual price action — did it move as expected? Faster? Slower? Different
direction?)

## Where the thesis was right / wrong
- Right about: ...
- Wrong about: ...
- Couldn't have known: ...

## Pattern label
(One of: clean-breakout, failed-breakout, news-pop-fade, base-build,
mean-reversion-success, mean-reversion-failure, bedcrock-classic,
correlation-blowup, other.)

## Lesson
(<=2 sentences. Surgical. This goes into the weekly synthesis input.)

## Attribution
- Signals that drove this: [[<signal1>]], [[<signal2>]]
- Setup pattern: <pattern>
- Trader source weights at the time: (look up scoring-rules.md as of date)
```

## Step 4 — Move and tag

After writing the post-mortem:

1. Move the original position file from `02 Open Positions/` to `03 Closed/`
   (or set `status: closed` and let Cowork's path organization handle it)
2. Mark the closure event in the inbox `status: processed`

## What NOT to do

- Don't be defensive about losing trades. The lesson is in the loss.
- Don't add information you didn't have at entry. ("It would have been obvious"
  is not a lesson.)
- Don't write a post-mortem for a position that hasn't actually closed.
