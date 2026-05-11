---
name: weekly-synthesis
description: Sunday 19:00 ET system-improvement run. Reads the past 7 days of trades, ignored signals, and gate-blocked signals; computes attribution by source, trader, pattern, and score bucket; posts a synthesis embed to the system-health Discord channel; and POSTs concrete weight-change proposals to /scoring-proposals so the replay engine can evaluate them before next Sunday.
disable-model-invocation: true
allowed-tools:
  - Bash(curl *)
  - Bash(jq *)
context: same
---

This is the weekly synthesis run. Once a week you read the entire trade history of the past seven days and propose changes to scoring weights, gates, source weights, and the watchlist. The output is a **proposal** evaluated by the replay engine — not a code change. The human reviews replay results before any rule actually moves.

## Step 1 — Pull the weekly snapshot

```bash
curl -sS -H "Authorization: Bearer $API_BEARER_TOKEN" \
  "$API_BASE_URL/dashboard/weekly" > /tmp/weekly.json
```

Expected JSON shape:

- `closures_7d` — every position closed in the last 7 days, with full attribution
- `ignored_signals_7d` — signals you triaged as not-worth-acting on; how did they perform if you'd taken them?
- `gate_blocked_7d` — signals the gates rejected; counterfactual P&L if released
- `current_scoring_rules` — the live weights (source weights, score-bucket multipliers, gate thresholds)
- `recent_proposals` — proposals already in the replay queue

## Step 2 — Compute attribution rollups

From `closures_7d`, build four tables:

- **By source**: `sec_form4`, `quiver_congress`, `uw_flow`, etc. → trades, win %, avg P&L
- **By trader** (top 5 by trade count): per-politician/insider stats → bonus/penalty update
- **By pattern**: from the `pattern_label` set by hourly-closure → which setups won/lost
- **By score bucket**: 4-5, 5-6, 6-7, 7-8, 8+ → win rate, avg P&L

## Step 3 — Find the divergences

The interesting questions:

- Which sources are over- or under-weighted? If `uw_flow` had 40% hit rate at score 6+ but `sec_form4` had 65% at the same score, the weights are wrong.
- Which gates were too strict? If 5 blocked signals would have made money, the gate is mis-calibrated.
- Which traders earned a bonus or penalty this week?
- Regime shift — did one pattern dominate winners vs losers? Worth flagging?
- Correlation blowups — multiple positions same direction same sector that all stopped together?

## Step 4 — POST the proposal payload to the replay engine

For every weight change you'd suggest, build a structured proposal and POST it. The replay engine will run the past 7d (and a longer history) against the proposed weights and produce a delta report by next Sunday.

```bash
curl -sS -X POST -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n '
    { weights: {
        sources: { sec_form4: 1.15, uw_flow: 0.85 },
        traders: { "Pelosi": 1.10 },
        gates:   { earnings_blackout_days: 2 }
      },
      rationale: "uw_flow win rate at score 6+ this week was 40% vs sec_form4 65%; reweight to reflect.",
      week_ending: "'"$(date +%F)"'"
    }')" \
  "$API_BASE_URL/scoring-proposals"
```

If you have multiple independent proposals (e.g., one for source weights, one for a gate threshold, one for a trader bonus), POST them as separate calls — the replay engine evaluates each independently. If you have nothing to propose, skip this step entirely. Doing nothing is a valid output.

## Step 5 — Post the synthesis embed

POST a single summary embed to `$DISCORD_WEBHOOK_SYSTEM_HEALTH`:

```bash
curl -sS -X POST -H "Content-Type: application/json" \
  -d "$(jq -n '
    { embeds: [ {
        title: ("Weekly synthesis — week ending " + env.WEEK_END),
        color: 10181046,
        fields: [
          {name: "Headline",            value: env.HEADLINE,    inline: false},
          {name: "Source attribution",  value: env.SOURCES,     inline: false},
          {name: "Trader attribution",  value: env.TRADERS,     inline: false},
          {name: "Pattern attribution", value: env.PATTERNS,    inline: false},
          {name: "Gate review",         value: env.GATES,       inline: false},
          {name: "Proposals submitted", value: env.PROPOSALS,   inline: false},
          {name: "Behavior note",       value: env.BEHAVIOR,    inline: false},
          {name: "Confidence",          value: env.CONFIDENCE,  inline: true}
        ],
        footer: {text: "weekly-synthesis"}
      } ] }')" \
  "$DISCORD_WEBHOOK_SYSTEM_HEALTH"
```

Keep each field under ~900 chars. The headline should be one line: `X opened, Y closed, Z% hit rate, $W P&L (V% vs SPY)`.

## What NOT to do

- Don't propose a change based on a single trade — the unit is the week
- Don't reverse a decision made just last week unless the new data is unambiguous
- Don't add pattern labels that don't recur — the vocabulary should stay small
- Don't apply changes yourself; the replay engine + human approval gate exists for a reason
