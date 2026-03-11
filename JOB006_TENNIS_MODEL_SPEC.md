# JOB-006 // Tennis Model Spec
## Sets & Games Prediction — ELO-Anchored Monte Carlo

> All synthetic line results labelled **PROVISIONAL** until real Betfair odds collected.

---

## Overview

Three pre-match markets. One simulation run per match. Surface-specific ELO is the primary signal — everything else is a small capped correction.

**Two-component structure** (mirrors darts/snooker Phase 1):

```
expected_games = expected_sets_played(ELO_gap, format)
               × expected_games_per_set(surface, ELO_gap, style)
```

ELO already encodes: recent results, opponent quality, surface performance, player trajectory.  
It does **not** encode: serve/return style interaction on this surface, tournament tiebreak rules, fatigue.  
Those three get small adjustments. Everything else gets nothing.

---

## Signal Weights

| Signal | Weight | Notes |
|---|---|---|
| `surface_elo_gap` | **0.60** | Primary predictor — drives all downstream outputs |
| `surface_form` | 0.15 | Rolling games % on this surface, last 10 matches |
| `style_interaction` | 0.12 | Serve dominance vs return quality × surface modifier |
| `tournament_round` | 0.06 | Format escalation — BO3 vs BO5, prestige multiplier |
| `tiebreak_rule` | 0.04 | Tournament-specific rule — RG advantage set is biggest outlier |
| `days_rest` | 0.02 | Fatigue proxy — only material below 2 days or above 10 |
| `h2h` | 0.01 | Near-noise — only apply if 10+ same-surface matches in 24 months |

Maximum shift on `p_win_A` from all non-ELO signals combined: **±0.07**. ELO cannot be overridden.

---

## Signal Definitions

### 3.1 Surface ELO Gap (0.60)

Source: [tennisabstract.com](https://tennisabstract.com) — clay, hard, grass ELO maintained separately. Updated after every match.

```python
elo_gap = player_A.elo[surface] - player_B.elo[surface]
p_win_A = 1 / (1 + 10 ** (-elo_gap / 400))
```

**Classification thresholds** (calibrate from Sackmann data):

| ELO gap | Signal | Action |
|---|---|---|
| ≥ 150 | MISMATCH | High conviction UNDER |
| 50–149 | NEUTRAL | Require 8%+ edge |
| < 50 | PARITY | OVER signal in late rounds / BO5 |

---

### 3.2 Surface Form (0.15)

Last 10 surface-specific matches. Games won % not W/L — a 6-0 6-1 win and a 6-4 7-5 win are both wins but signal different quality gaps.

```python
weights = [0.22, 0.18, 0.15, 0.12, 0.10, 0.08, 0.06, 0.04, 0.03, 0.02]
form_score = sum(w * games_pct for w, games_pct in zip(weights, matches))
```

**Gates:**
- Minimum 3 matches on surface → else flag `LOW_SAMPLE`, reduce confidence tier
- Stale if most recent match > 60 days ago → skip bet entirely
- Maximum shift on `p_win_A`: **±0.04**

---

### 3.3 Style Interaction (0.12)

The one signal ELO does not fully capture — ELO records outcomes, not how points were won.

```python
serve_score  = ace_rate*0.40 + first_serve_pct*0.25 + first_won*0.25 + second_won*0.10
return_score = return_pts_won*0.50 + break_pts_converted*0.30 + first_return_won*0.20

SURFACE_SERVE_WEIGHT = { 'grass': 1.35, 'hard': 1.00, 'clay': 0.72 }

serve_effectiveness = (serve_A / return_B) * surface_modifier
break_rate          = 1 / (1 + serve_effectiveness)   # per service game
tiebreak_prob       = (1 - break_rate_A) * (1 - break_rate_B) * surface_modifier
```

**Key scenarios:**
- Big server on clay vs strong returner → serve neutralised → high break rate → sets end fast → **UNDER**
- Two big servers on grass → both hold easily → tiebreaks likely → **OVER bias**
- Maximum shift on `p_win_A`: **±0.03**

---

### 3.4 Tournament Round (0.06)

```python
ROUND_FORMAT = {
    'R128': 'BO3', 'R64': 'BO3', 'R32': 'BO3', 'R16': 'BO3',
    'QF': 'BO5', 'SF': 'BO5', 'F': 'BO5'
}

PRESTIGE = {
    'R128': 0.85, 'R64': 0.88, 'R32': 0.91, 'R16': 0.94,
    'QF': 1.00, 'SF': 1.05, 'F': 1.10
}
```

Prestige multiplier applied to `p_win` shift only — not to raw ELO.

---

### 3.5 Tiebreak Rule (0.04)

| Tournament | Deciding set rule | Games adjustment |
|---|---|---|
| Roland Garros | Advantage set | **+3.2** (biggest outlier — fat tail on OVER) |
| Wimbledon | Tiebreak at 12-12 | +1.8 |
| Australian Open | Super tiebreak | -0.8 |
| Standard | Tiebreak at 6-6 | 0.0 |

RG advantage set rule is the single biggest structural misprice — books underestimate the tail because it's rare but large.

---

### 3.6 Days Rest / Fatigue (0.02)

```python
if days_rest <= 2:    fatigue_factor = 0.94   # tired
elif days_rest <= 5:  fatigue_factor = 1.00   # optimal
elif days_rest <= 10: fatigue_factor = 0.98   # slight rust
else:                 fatigue_factor = 0.95   # match sharpness risk

# Heavy schedule: 6+ matches in last 14 days
if recent_match_count >= 6:
    fatigue_factor *= 0.96
```

Maximum combined shift on `p_win_A`: **±0.02**

---

### 3.7 H2H (0.01)

Only compute if: 10+ matches on same surface in last 24 months.  
Maximum shift on `p_win_A`: **±0.01**.  
Most matchups return neutral multiplier 1.0 — do not compute for those.

---

## Prediction Flow

```python
def predict_match(player_A, player_B, surface,
                  round_code, tournament_id, match_date, db):

    # Step 1 — ELO anchors p_win (60% of signal)
    elo_gap = player_A.elo[surface] - player_B.elo[surface]
    p_win_A = 1 / (1 + 10 ** (-elo_gap / 400))

    # Step 2 — Small corrections, each capped
    p_win_A += np.clip(form_delta * 0.15,  -0.04, +0.04)
    p_win_A += np.clip(style_mod  * 0.12,  -0.03, +0.03)
    p_win_A *= (fatigue_A / fatigue_B)                # ≈ ±0.02
    p_win_A *= h2h_multiplier                         # ≈ ±0.01
    p_win_A  = np.clip(p_win_A, 0.05, 0.95)

    # Step 3 — 10,000 simulations from p_win_A
    sets_dist, games_dist, p_first_set_A = simulate(
        p_win_A, format_, tiebreak_rule, n=10000, seed=match_seed
    )

    # Step 4 — Output fair lines for all three markets
    return ModelOutput(
        sets_fair_line  = np.median(sets_dist),
        p_over_sets     = np.mean(sets_dist > 2.5),
        games_fair_line = np.median(games_dist),
        p_over_games    = {line: np.mean(games_dist > line) for line in LINES},
        p_first_set_A   = p_first_set_A,
    )
```

---

## Target Markets

| Market | Line structure | Betfair liquidity | Primary signal |
|---|---|---|---|
| Total Sets O/U | BO3 = 2.5, BO5 = 3.5/4.5 | £500–5,000 at Slams | Straight sets vs deciding set |
| Total Games O/U | Scan 18.5–28.5 | £2,000–50,000 QF+ | **Primary target — highest edge** |
| First Set Winner | Binary | £500–3,000 pre-match | Often mispriced vs match odds |

### Highest-conviction scenarios

**UNDER total games**
- Clay + ELO gap ≥ 150 + R1–R3
- Model: ~18 games | Book line: ~23.5 | Edge: +20%+

**OVER total games**
- Grass/RG + ELO gap < 50 + QF/SF/F BO5
- Model: ~46 games | Book line: ~38.5 | Edge: +15%+

**First set — favourite**
- Clay + ELO gap ≥ 100 + any round
- Model p: ~0.74 | Market implied: ~0.62 | Edge: +12%

---

## Edge Detection & Kelly Staking

```python
CONFIDENCE_TIERS = {
    'HIGH':   { 'min_elo_gap': 150, 'kelly_fraction': 0.50 },
    'MEDIUM': { 'min_elo_gap': 100, 'kelly_fraction': 0.35 },
    'LOW':    { 'min_elo_gap':  70, 'kelly_fraction': 0.20 },
    'SKIP':   { 'min_elo_gap':   0, 'kelly_fraction': 0.00 },
}

# Minimum edge threshold: 8% across all markets
# Hard cap: 5% of bankroll per bet
stake = min(bankroll * kelly * confidence, bankroll * 0.05)
```

**Confidence nudges** (cannot change tier, only nudge within):
- Strong form confirmation: +0.05
- Low sample form: -0.05
- Stale data: skip entirely

---

## Validation Gates

Run on existing **5,632 Sackmann matches** before writing model code. All four must pass.

| Gate | Test | Minimum threshold |
|---|---|---|
| 1 | Surface ELO gap predicts sets played | R² ≥ 0.15 |
| 2 | Style interaction predicts games/set on clay | p < 0.05 |
| 3 | Combined model beats naive surface mean | 10% MAE improvement |
| 4 | `p_first_set` beats market odds historically | Brier score < 0.23 |

If any gate fails — investigate before proceeding. Do not write production code against a failing gate.

---

## Files to Build

Build and test in this order:

```
elo_loader.py     Surface ELO fetch from tennisabstract.com → universe.db
simulate.py       10k Monte Carlo from p_win_A → sets, games, first set distributions
backtest.py       Walk-forward validation — 4 gates on 5,632 matches
form_builder.py   Rolling surface form, games %, decay weights, staleness check
style.py          Serve/return interaction matrix, break rate, tiebreak probability
edge.py           Kelly staking, ELO-based confidence tiers, edge scan across lines
scraper.py        Betfair API wrapper — polls markets pre-match, stores in bookie_lines
bot.py            Telegram relay — signal alert, bet recommendation, Y/N confirmation
```

---

## Telegram Alert Format

```
🎾 TENNIS SIGNAL — Roland Garros R1

[1] UNDER 21.5 games — Alcaraz vs Qualifier
Surface ELO gap: +312 (clay)  →  MISMATCH
Form: 0.81 / 0.51 (clay, last 8 matches)
Style: High break rate expected (clay + return dominance)
Model: 17.8 games expected  |  Line: 21.5
Model p_under: 76%  |  Market: 54%  |  Edge: +22%
Stake: £412  |  Odds: 1.87  |  Tier: HIGH

[2] FIRST SET — Alcaraz to win
Model p: 81%  |  Market implied: 67%  |  Edge: +14%
Stake: £280  |  Odds: 1.48

Reply: 1Y 2Y / 1N 2N / ALL / NONE
```

---

## Open Blockers

| Blocker | Type | Fix |
|---|---|---|
| ELO data not loaded | Data | Scrape tennisabstract.com, store in universe.db |
| Tennis score strings not parsed | Data | Parse Sackmann — extract sets_played + total_games |
| Betfair lines empty | Execution | Line logger on server arrival (~March 15) |
| WST rankings not sourced | Data | snooker.org — needed for snooker Claim 1 |
| Snooker shrinkage prior too high | Model | Tier by tour avg vs qualifier avg |
| UK broker access uncertain | Execution | Multi-exchange direct: Betfair + Smarkets + Matchbook + Betdaq |

---

## What Is Not Being Modelled

**Injuries** — unmodellable systematically pre-match. Flag confirmed injuries manually. High-risk players tagged in `universe.db`.

**Football corners** — evaluated and rejected. Edge ~2–4% pre-match, market too well-covered, game state is in-play information not available pre-match.

---

## Project Context

This is the tennis module of JOB-006, which also covers:

- **Darts** — direct regression on 180s, total legs market. Walk-forward validated. Production ready.
- **Snooker** — NegBin rate×units, total frames market. Long-format SF/F OVER is strongest signal in dataset.
- **Tennis** — this document. ELO-anchored, three markets.

Master blueprint: `JOB006_MASTER_BLUEPRINT.md`  
Database: `~/sports-betting/data/universe.db`  
Models: `~/sports-betting/src/model/`
