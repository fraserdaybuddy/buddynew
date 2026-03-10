# Sports Totals Betting Model
## Problem, Strategy & Proposal — External Review Document

---

## The Problem

Bookmakers price sports betting markets primarily around **who wins**. Match winner
odds are highly efficient — sharp money, sophisticated models, and decades of data
mean there is very little edge available for a retail bettor in win markets.

However, bookmakers also offer **totals markets** — over/under on specific in-match
statistics. For example:

- Darts: total 180s scored in a match (e.g. over/under 5.5)
- Snooker: total century breaks in a match (e.g. over/under 3.5)
- Tennis: total aces in a match (e.g. over/under 12.5)

These markets are priced using **blended season averages**. The bookmaker looks at
how many 180s Player A averages per match and how many Player B averages, adds them
together, and sets a line. This is a fundamentally flawed approach because it ignores
the two structural factors that actually determine the total:

1. **How many scoring opportunities the match will contain** (legs played, frames played,
   service games played) — which is driven by the skill gap between players
2. **How efficiently each player converts those opportunities** — their individual rate per unit

The bookmaker conflates both into one average number. We separate them.

---

## The Core Thesis

```
Expected total events = Opportunity count × Event rate

Darts:   Expected 180s   = legs played         × 180s per leg
Snooker: Expected cents  = frames played        × centuries per frame
Tennis:  Expected aces   = service games played × aces per service game
```

This creates two predictable structural edges:

### Edge A — Mismatch Under
When there is a large skill gap between players:
- The dominant player wins units (legs/frames/games) quickly
- The match ends earlier than a parity match in the same format
- The weaker player is denied visits / table time / service games
- Total events are compressed **below** where the market has set the line
- **Bet: UNDER**

### Edge B — Parity Over
When two similarly-skilled players meet in a long format at a late stage:
- Every unit is contested — the match goes deep
- Both players accumulate events throughout
- Total events exceed the market line
- **Bet: OVER**

The market prices these incorrectly because it uses season averages that blend all
opponent types together. A 180-rate computed against elite opposition, journeymen, and
qualifiers alike will overestimate what happens when that player faces an elite opponent
(who compresses the match) and underestimate what happens when they face a peer (who
extends it).

---

## The Data

Three sports are in scope. All historical data has been collected:

| Sport | Matches | Key stat | Coverage |
|-------|---------|----------|----------|
| Darts (PDC) | 1,230 | 180s per match | 69% of matches |
| Snooker (WST) | 2,185 | Centuries per match | 98% of matches |
| Tennis (ATP) | 5,632 | Aces per match | 98% of matches |

Data spans 2024–2025. All data lives in a single SQLite database (`universe.db`).

Additional fields available in raw staging tables (not yet promoted to main table):
- Darts: 3-dart average, 140+ scores, checkout %, score (to compute legs played)
- Snooker: frames won per player (to compute total frames played)
- Tennis: service points played, return points won %

---

## What We Have Already Built

**Infrastructure (complete):**
- Full SQLite schema with player identity resolution
- Scrapers for all three sports (darts24.com, cuetracker.net, Sackmann ATP dataset)
- Sportmarket API execution layer (bet broker routing to Betfair, Smarkets, Matchbook)
- Paper / live mode toggle with full audit trail
- Kelly stake sizing with circuit breakers

**Analytics (in progress):**
- Initial signal test: round (as mismatch proxy) vs stat count
  - Darts: Spearman ρ = +0.15 — weak signal present
  - Snooker: Spearman ρ = +0.31 — meaningful signal present
  - Tennis: Spearman ρ = +0.04 — round is wrong variable for tennis

---

## Current Blockers

Three things prevent the full model from running:

**1. Skill gap variable not in the main table**
Skill gap is defined as expected dominance in the unit that controls match length.
Per sport:
- Darts: 3-dart avg differential (in staging, not yet in matches table)
- Snooker: weighted composite of log(ranking pts) + frame win rate + century rate per frame
  (frame win rate computable now; ranking points not yet sourced — snooker.org API)
- Tennis: serve/return strength differential (serve_pts_won ≈ 100 − opp_ret_pts_won_pct,
  surface-specific — data available in staging, not yet in matches table)
Fix: schema migration + promote from staging + source snooker rankings.

**2. Opportunity count not computed**
Legs played (darts) and service games (tennis) need to be derived from raw data.
Frames played (snooker) is available. Fix: parse from existing score fields.

**3. No historical market lines**
The betfair_markets table is empty. Without historical lines we can validate the model
internally (does it predict actuals?) but cannot measure actual edge vs market.
Fix: scrape Oddsportal for historical lines, and/or start logging Sportmarket lines
for all upcoming matches from now.

---

## The Test Plan

Before building the full simulation model, we validate the two core claims:

**Claim 1 — Skill gap predicts opportunity count**
Does a larger skill gap produce fewer legs/frames/service games in the same format?
If this doesn't hold in the data, the mismatch under thesis is wrong.

**Claim 2 — Player rates × opportunity count predicts total events**
Does `expected_units × player_rate` produce a more accurate prediction than a naive
season average?
If it doesn't beat the naive baseline, the model adds no value.

**Claim 3 — The prediction diverges from the market line**
Does our prediction systematically differ from where the bookmaker sets the line?
If the market already prices this correctly, there is no edge regardless of model accuracy.

Only if all three claims hold do we proceed to full simulation and live betting.

---

## The Variable Structure (Once Claims Validated)

Variables are weighted in four tiers. Only Tier A is needed for Phase 1 validation.
Tiers B–D are added when building the production model.

**Tier A — Core drivers (60–70%)**
Expected units played, player event rates, skill gap, format, surface (tennis).
All directly measurable from existing data once schema migration is complete.

**Tier B — Context modifiers (20–30%)**
Tournament stage, recency trend (slope of rolling rate window), style interaction
(snooker: attacking vs safety player classification). All computable.

**Tier C — Situational signals (5–10%)**
Head-to-head history for the specific player pairing (if ≥ 5 H2H matches exist).
Breakout/sparse player handling via Bayesian shrinkage toward comparable player prior.

**Tier D — Risk filters (always applied)**
Stale data (> 30 days), low sample (< 3 matches), missing stats, low liquidity.
These govern betting decisions, not model predictions.

**Excluded entirely:** fatigue, venue effects, table conditions, pressure index,
tournament-specific rates. Not measurable reliably from available data.

---

## The Model (Once Claims Validated)

A three-step process for each match:

**Step 1 — Signal classification**
Compute skill gap. Classify as MISMATCH_UNDER, PARITY_OVER, or NO_SIGNAL.
Only proceed on the first two. Everything else is passed.

**Step 2 — Monte Carlo simulation (10,000 runs)**
```
For each simulation:
  → Simulate units played (legs/frames/games) given skill gap + format
  → For each unit: sample 180s/centuries/aces from player's rate distribution
  → Sum to get total events for this simulation

Output: full probability distribution over all possible totals
```

**Step 3 — Edge calculation**
```
edge = model_probability(under line) − market_implied_probability(under line)
       [model_p_under]               [1 / bookmaker_under_odds]

Bet if edge > 5% (Tier 1 player, full data)
         > 7% (Tier 2 player, partial data)
         > 9% (Tier 3 player, emerging / sparse data)
```

---

## Player Data Model

Form is built from a rolling window, not season averages:

```
Horizon A: rolling 24-month dataset       — structural analysis, model training
Horizon B: last 7–15 matches per player   — current form, hot/cold status
Horizon C: career data (weak prior)       — player style, sparse player stabilisation
```

Players are tiered by data availability:
- Tier 1 (10+ recent matches): full model, normal thresholds
- Tier 2 (3–9 matches): shrink rates toward comparable player prior, higher edge threshold
- Tier 3 (0–2 matches): Bayesian shrinkage dominant, conservative stakes

This handles the Luke Littler problem: an elite emerging player with minimal data
history should not be modelled the same as a veteran with 200 matches on record.

---

## What We Are Asking For

External review on three specific questions:

**1. Is the thesis sound?**
Does the two-component model (opportunity count × event rate) represent a genuine
structural inefficiency, or is the market already pricing this correctly?

**2. Is the test plan sufficient?**
Are Claims 1, 2, and 3 the right things to validate? Is there a simpler or more
rigorous way to prove or disprove the thesis before building the full simulation?

**3. Are there obvious blind spots?**
Variables, market dynamics, or execution realities that would invalidate the edge
even if the model predicts correctly (e.g., line movement, low liquidity, market
suspension rules for these stat markets)?

---

## Summary

| Item | Status |
|------|--------|
| Thesis | Defined — two-component model |
| Data | Collected — 9,047 matches across 3 sports |
| Infrastructure | Built — scraping, DB, execution layer |
| Analytics | Partial — signal test run, full EDA blocked pending schema work |
| Model | Not yet built — pending claims validation |
| Market lines | Not yet sourced — critical blocker for edge calculation |
| Live betting | Not yet — paper trade first, live only after backtest clears |
