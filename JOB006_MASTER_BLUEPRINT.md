# JOB-006 Master Blueprint
## Sports Betting Model — Complete Agent-Ready Specification
**Version:** 1.6 | **Status:** STAGE 2 COMPLETE — Sportmarket adapter built and tested | **Date:** 2026-03-10

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-03-08 | Initial blueprint — darts + snooker only |
| 1.2 | 2026-03-08 | Added Sportmarket API, Telegram workflow, multi-sport framing |
| 1.3 | 2026-03-08 | Added tennis. Replaced urllib with Crawl4AI. Stage 1 built and tested. |
| 1.4 | 2026-03-08 | **Full metric set per sport. Corrected tennis thesis (duration not style). Outlier-robust form builder (trimmed mean, 7-match window). Fractional Kelly confidence-tiered staking replacing fixed % model.** |
| 1.5 | 2026-03-09 | **Data source archaeology complete. dartsdatabase.py DEPRECATED (no per-match 180s). darts24.com confirmed as primary darts source — provides 180s, 3-dart avg, 140+, 100+, checkout %, highest checkout per match. New darts24.py scraper built and tested. 16 major tournaments (2024+2025) mapped.** |
| 1.6 | 2026-03-10 | **Stage 2 complete. Sportmarket adapter built: sportmarket.py (API client), governor.py (Kelly staking + circuit breaker), ledger_writer.py (pre-placement + settlement writes). 21/21 integration tests pass. Darts scraper resumed — 7 remaining 2024 tournaments running.** |

---

## Contents
1. [Blueprint Index & Critical Rules](#blueprint-index)
2. [Data Architecture & Schema](#data-architecture)
3. [Model Specification](#model-specification)
4. [Backtesting Framework](#backtesting-framework)
5. [Agent Task Definitions](#agent-task-definitions)
6. [Scraping Infrastructure — Crawl4AI](#scraping-infrastructure)
7. [Build Status](#build-status)

---

<a name="blueprint-index"></a>
# PART 1 — Blueprint Index
## Sports Betting Model — Agent-Ready Foundation

---

## Critical Instruction for Agents

> **You are receiving a specification, not a permission to fabricate.**
>
> Every document in this blueprint contains explicit data requirements, acceptance criteria, and verification gates. You MUST NOT:
> - Simulate, synthesise, or generate fake data to satisfy a gate
> - Report a task as complete if the output file does not exist on disk
> - Skip a validation step because "it is likely to pass"
> - Proceed past a HARD GATE without explicit human confirmation
>
> If real data cannot be obtained, you report BLOCKED with the specific reason. You do not substitute.

---

## Strategy Summary (The Edge Thesis)

### Core theory
In skill-mismatched matches, one player drives a specific stat while the other fails to suppress it. Markets price who wins. They underprice what happens during the match.

### Two-way alpha across three sports

| Sport | Stat | Mismatch Under | Parity Over |
|-------|------|----------------|-------------|
| Darts (PDC) | 180s | Dominant player compresses legs → fewer visits → fewer 180s | Elite vs elite, long format → extended match → more 180s |
| Snooker (WST) | Centuries | Dominant player clears frames → opponent starved of table time | Elite vs elite, long format → more centuries |
| Tennis (ATP/WTA) | Aces | Big server vs passive returner → server swings freely → ace volume spikes | — |

### Unified edge formula
```
Edge = f(style_mismatch, stat_generation_rate, market_blindspot)

Market blindspot is always the same:
  Books price WHO WINS
  They underprice WHAT HAPPENS DURING THE MATCH
```

---

## Sports in Scope

### Phase 1 (Build now)
```
Darts (PDC)      Core thesis, proven 3/3, best data availability
Snooker (WST)    Same model structure, large calendar, CueTracker data
Tennis (ATP/WTA) Aces model, Jeff Sackmann free dataset, excellent coverage
```

### Phase 2 (After Phase 1 backtest validates)
```
MMA (UFC)    Strike volume model, ufcstats.com data
Boxing       Punch totals, CompuBox expensive
```

### Does NOT fit
Golf (field event), Cricket/Football (team format)

---

## Data Sources — Final Decisions

| Sport | Primary Data Source | Cost | Notes |
|-------|--------------------|----|-------|
| Darts | darts24.com (FlashScore) | Free | **PRIMARY** — per-match 180s, avg, 140+, 100+, checkout %. dartsdatabase.co.uk REJECTED (no per-match 180s). |
| Snooker | CueTracker.net | Free | Centuries + break data |
| Tennis | github.com/JeffSackmann/tennis_atp | Free | CSV back to 1968, aces columns included |
| Odds | Betfair Historical Data API | User has access | Canonical closing prices |
| Live execution | Sportmarket API | Per bet | Winners always welcome policy |

**Sportradar:** Not used in Phase 1. £500–£1,000+/month per sport. Revisit after Phase 1 proves profitable.

---

## Execution Platform: Sportmarket

**URL:** https://www.sportmarket.com | **API:** https://api.sportmarket.com/docs/

Sportmarket is a bet broker — one account routes across Betfair, Smarkets, Matchbook, Molly Exchange, PS3838, Betdaq, SBO simultaneously. "Winners always welcome" — no account restrictions.

### Key API endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/orders/` | POST | Place single order |
| `/v1/orders/batch/` | POST | Up to 200 orders at once |
| `/v1/orders/<id>/` | GET | Poll status / settlement |
| `/v1/orders/close_all/` | POST | Emergency kill switch |

### Critical execution details
- `request_uuid` prevents duplicate orders — use `hash(match_id + direction + run_id)`
- Placement is async — place then poll for fill
- `profit_loss` on settled bets = canonical P&L source
- `exchange_mode: "dark"` hides order size for larger stakes

---

## Telegram Approval Workflow

```
Market opens on Sportmarket
→ Harvester detects eligible betslip
→ Model runs (Poisson simulation)
→ Edge check + sniper filter
→ Telegram batch report sent to human
→ Human replies: "1Y 2N 3Y" or "ALL" or "NONE" or "ALL H"
→ Adapter places approved bets via Sportmarket API
→ Confirmation per placed bet
→ Settlement message post-match with P&L
→ DB updated overnight with results
```

### Reply commands

| Command | Action |
|---------|--------|
| `1Y 2N 3Y` | Explicit approve/reject per bet |
| `1H 2Y 3N` | Half stake on bet 1 |
| `ALL` | Approve everything full stake |
| `NONE` | Skip everything |
| `ALL H` | Approve everything half stake |

T-10 minutes cutoff — unapproved bets auto-expire.

### Telegram report format

```
🎯 SNIPER REPORT — PDC Pro Tour
Session: Afternoon │ 14:00-18:00
Bankroll: £3,000   │ 3 bets found
─────────────────────────────────
[1] UNDER — Humphries vs Doets
Gap: 16.4pts │ BO11 │ 14:30
Model: 3.2   │ Line: 5.5 │ Edge: +12%
Stake: £45   │ Odds: 1.83
─────────────────────────────────
[2] UNDER — Littler vs Qualifier
Gap: 14.1pts │ BO11 │ 15:45
Model: 3.8   │ Line: 5.5 │ Edge: +8%
Stake: £35   │ Odds: 1.85
─────────────────────────────────
Reply: 1Y 2Y 3N (Y/N/H per bet)
Or ALL / NONE / ALL H
```

---

## Build Sequence

```
[COMPLETE] Stage 1: Python package + database layer + scrapers
  ✓ src/database.py    — full SQLite schema, init, backup, hard gate queries
  ✓ src/resolver.py    — player identity resolution with confidence tiers
  ✓ src/config.py      — sport config registry (darts / snooker / tennis)
  ✓ src/scrapers/darts/darts24.py       — PRIMARY darts scraper (darts24.com, 180s confirmed)
  ✗ src/scrapers/darts/dartsdatabase.py — DEPRECATED (no per-match 180s, wrong source)
  ✓ src/scrapers/snooker/cuetrackeR.py
  ✓ src/scrapers/tennis/sackmann.py     — COMPLETE, 5632 ATP matches in DB

[COMPLETE] Stage 1b: Data source archaeology (2026-03-09)
  ✓ dartsdatabase.co.uk — investigated, REJECTED (tournament-level only, no per-match 180s)
  ✓ PDC API — investigated, provides match results + player data, NO 180s (Sportradar locked)
  ✓ darts24.com — CONFIRMED primary source
      URL: /match/{p1-slug}/{p2-slug}/summary/stats/?mid={id}
      Stats: 3-dart avg, 180s, 140+, 100+, checkout % (hits/att), highest checkout
      Coverage: 2017+, all PDC majors, 16 tournaments mapped for 2024+2025

[COMPLETE] Stage 1c: Darts data collection (2026-03-10)
  ✓ darts24.py scraped 10/16 tournaments overnight (818 rows in staging_darts, PENDING)
  ⚠ Scraper stopped at PDC Premier League 2024 match 19/35 — resumed this session
  → Remaining 7 tournaments running in background now

[COMPLETE] Stage 1d: All data collected and promoted (2026-03-10)
  ✓ Darts:   1,230 matches in DB (16 tournaments, 2024+2025)
  ✓ Snooker: 2,185 matches in DB (26 tournaments, 2022-2025)
  ✓ Tennis:  5,632 matches in DB
  ✓ alias_review_queue: 0 unresolved
  Bug fixed: resolver.py — ResolutionQueued raised inside with block caused silent rollback

[COMPLETE] Stage 2: Sportmarket adapter (2026-03-10)
  ✓ src/execution/sportmarket.py  — API client: betslip fetch, place, poll, close_all
  ✓ src/execution/governor.py     — Kelly stake sizing, circuit breaker, LIVE_MODE gate
  ✓ src/execution/ledger_writer.py — pre-placement write, order_placed update, settlement
  ✓ src/execution/integration_test.py — 21/21 tests pass
  Acceptance criteria met:
    ✓ Paper mode places 0 real orders, writes full ledger row per intended bet
    ✓ request_uuid prevents duplicate placement on retry
    ✓ Emergency kill switch confirmed safe in paper mode
    ✓ Ledger entry written BEFORE polling for settlement

[NEXT]    Stage 3: Poisson model + calibration runner
[PENDING] Stage 4: Report generator + Telegram formatter
[PENDING] Stage 5: Reply parser + approval router
[PENDING] Stage 6: Settlement tracker
[COMPLETE] Stage 7: Crawl4AI upgrade — darts24.py uses Crawl4AI + BeautifulSoup
```

---

## Known Risks (From Prior Run — Do Not Repeat)

| Risk | What Happened Before | Mitigation |
|------|---------------------|------------|
| Synthetic data substitution | Agent generated fake match data | Hard gates require file existence + row count verified by human |
| Identity resolver failure | Only handled top 10 players; qualifiers null | Resolver handles 100% of players, all routes through staging |
| Fabricated ROI | ROI ran against synthetic patterns | Backtest must reference real Betfair closing prices only |
| Liquidity mismatch | Assumed infinite liquidity | Stake capped at 10% of matched volume |
| Averages as lagging indicators | Used season averages | Form window = last 5 matches with date-decay weighting |

---

<a name="data-architecture"></a>
# PART 2 — Data Architecture & Schema

---

## 1. Overview

All data lives in a single SQLite database: `universe.db`. This is the single source of truth.

**Location:** `~/sports-betting/data/universe.db`
**Backup:** Automatic timestamped backup before every write operation
**Immutability:** Raw source tables are append-only. Derived tables are rebuilt, never patched.
**Staging:** All scraped data enters a sport-specific staging table first. Promotion to `matches` only after resolver runs successfully.

---

## 2. Full Schema (v1.3)

### 2.1 `players`
```sql
CREATE TABLE players (
    player_id       TEXT PRIMARY KEY,   -- {TOUR}-{SURNAME}-{INITIAL} e.g. PDC-HUMPHRIES-L
    tour            TEXT NOT NULL,      -- PDC | WST | ATP | WTA
    full_name       TEXT NOT NULL,
    nationality     TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**ID convention:**
```
PDC-HUMPHRIES-L    → Luke Humphries
PDC-MVGERWEN-M     → Michael van Gerwen
WST-OSULLIVAN-R    → Ronnie O'Sullivan
ATP-DJOKOVIC-N     → Novak Djokovic
WTA-SWIATEK-I      → Iga Swiatek
Collision:         PDC-SMITH-M-1990 (append birth year)
```

---

### 2.2 `player_aliases`
```sql
CREATE TABLE player_aliases (
    alias_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_name        TEXT NOT NULL,
    player_id       TEXT NOT NULL REFERENCES players(player_id),
    source          TEXT NOT NULL,   -- dartsdatabase | cuetrackeR | sackmann_atp | manual
    confidence      REAL NOT NULL,   -- 0.0 – 1.0
    status          TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | ACCEPTED | REJECTED
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(raw_name, source)
);
```

**Confidence tiers:**
- `>= 0.95` → auto-accept
- `0.80–0.94` → queue for human review
- `< 0.80` → create new player entry

---

### 2.3 `alias_review_queue`
```sql
CREATE TABLE alias_review_queue (
    queue_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_name        TEXT NOT NULL,
    suggested_id    TEXT REFERENCES players(player_id),
    confidence      REAL NOT NULL,
    source          TEXT NOT NULL,
    context         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT,
    resolution      TEXT   -- ACCEPTED | REJECTED | NEW_PLAYER
);
```

**HARD GATE:** Queue must be empty before Phase 1 model build.

---

### 2.4 `tournaments`
```sql
CREATE TABLE tournaments (
    tournament_id   TEXT PRIMARY KEY,  -- PDC-2024-WORLDS | WST-2024-WC | ATP-2024-WIMBLEDON
    sport           TEXT NOT NULL,     -- darts | snooker | tennis
    tour            TEXT NOT NULL,     -- PDC | WST | ATP | WTA
    name            TEXT NOT NULL,
    year            INTEGER NOT NULL,
    start_date      TEXT,
    end_date        TEXT,
    venue           TEXT,
    surface         TEXT,              -- tennis only: hard | clay | grass
    prize_fund_gbp  REAL,
    source_url      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
```

---

### 2.5 `matches` (unified — all sports in one table)
```sql
CREATE TABLE matches (
    match_id        TEXT PRIMARY KEY,  -- sha256(tournament_id|round|p1|p2|date)[:16]
    tournament_id   TEXT NOT NULL REFERENCES tournaments(tournament_id),
    sport           TEXT NOT NULL,     -- darts | snooker | tennis
    round           TEXT NOT NULL,     -- R1 | R2 | QF | SF | F | RR
    match_date      TEXT NOT NULL,     -- YYYY-MM-DD
    player1_id      TEXT NOT NULL REFERENCES players(player_id),
    player2_id      TEXT NOT NULL REFERENCES players(player_id),
    winner_id       TEXT REFERENCES players(player_id),
    format          TEXT NOT NULL,     -- BO11 | BO13 | BO19 | SETS_3 | SETS_5 etc.
    legs_sets_total INTEGER,

    -- Darts-specific
    p1_180s         INTEGER,
    p2_180s         INTEGER,
    total_180s      INTEGER GENERATED ALWAYS AS (
                        CASE WHEN p1_180s IS NOT NULL AND p2_180s IS NOT NULL
                        THEN p1_180s + p2_180s ELSE NULL END
                    ) STORED,

    -- Snooker-specific
    p1_centuries    INTEGER,
    p2_centuries    INTEGER,
    total_centuries INTEGER GENERATED ALWAYS AS (
                        CASE WHEN p1_centuries IS NOT NULL AND p2_centuries IS NOT NULL
                        THEN p1_centuries + p2_centuries ELSE NULL END
                    ) STORED,

    -- Tennis-specific
    p1_aces         INTEGER,
    p2_aces         INTEGER,
    total_aces      INTEGER GENERATED ALWAYS AS (
                        CASE WHEN p1_aces IS NOT NULL AND p2_aces IS NOT NULL
                        THEN p1_aces + p2_aces ELSE NULL END
                    ) STORED,
    p1_return_pts_won_pct   REAL,  -- % of opponent serve points won (return pressure)
    p2_return_pts_won_pct   REAL,
    p1_second_serve_pts_won REAL,
    p2_second_serve_pts_won REAL,

    -- Provenance
    data_source     TEXT NOT NULL,   -- dartsdatabase | cuetrackeR | sackmann_atp | manual
    source_url      TEXT,
    data_quality    TEXT NOT NULL DEFAULT 'UNVERIFIED',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**NULL policy:** Nullable sport-specific columns (e.g. p1_180s on a tennis match) are always NULL. Never 0, never estimated.

---

### 2.6 `player_form`
```sql
CREATE TABLE player_form (
    form_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       TEXT NOT NULL REFERENCES players(player_id),
    sport           TEXT NOT NULL,
    as_of_date      TEXT NOT NULL,
    matches_counted INTEGER NOT NULL,
    avg_180s_per_leg        REAL,   -- darts
    avg_centuries_per_frame REAL,   -- snooker
    avg_aces_per_match      REAL,   -- tennis
    avg_return_pts_won      REAL,   -- tennis style score
    form_score      REAL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(player_id, sport, as_of_date)
);
```

**Staleness gate:** Form > 30 days old → player flagged DATA_STALE → not eligible for model.

---

### 2.7 `betfair_markets`

The exchange reference. Closing prices only — these are the canonical odds for calibration and backtest.

```sql
CREATE TABLE betfair_markets (
    market_id       TEXT PRIMARY KEY,
    match_id        TEXT REFERENCES matches(match_id),
    sport           TEXT NOT NULL,
    market_type     TEXT NOT NULL,    -- total_180s | total_centuries | total_aces
    line            REAL NOT NULL,    -- the O/U line: 4.5, 5.5, 6.5 etc.
    over_odds       REAL,
    under_odds      REAL,
    total_matched   REAL,
    settled_result  TEXT,             -- OVER | UNDER | VOID
    actual_total    INTEGER,          -- actual 180s/centuries/aces in the match
    data_source     TEXT NOT NULL,    -- betfair_historical_api | manual
    verified        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**HARD RULE:** Closing odds from real Betfair data only. Never estimated, never synthetic.

---

### 2.7b `bookie_lines`

Tracks all available lines from all sources for a given match — exchange AND fixed-odds bookies. This is the line-shopping table. Different bookies set different lines on the same market; the variance in line (not just odds price) is often where most of the value sits.

```sql
CREATE TABLE bookie_lines (
    line_id         TEXT PRIMARY KEY,   -- hash(match_id + bookie + market_type + line)
    match_id        TEXT NOT NULL REFERENCES matches(match_id),
    sport           TEXT NOT NULL,
    market_type     TEXT NOT NULL,      -- total_180s | total_centuries | total_aces
    bookie          TEXT NOT NULL,      -- betfair | smarkets | bet365 | hills | paddy | sportmarket
    line            REAL NOT NULL,      -- the specific O/U line this bookie is offering
    over_odds       REAL,
    under_odds      REAL,
    available_volume REAL,              -- matched volume (exchanges) or NULL (fixed odds)
    captured_at     TEXT NOT NULL,      -- when this price was observed (pre-match)
    is_closing      INTEGER DEFAULT 0,  -- 1 = closing price snapshot
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**Why this table matters:** If the model says expected 180s = 3.8 and:
- Betfair is offering Under 5.5 at 1.85 (market p_under implied = 54%)
- Bet365 is offering Under 4.5 at 1.75 (market p_under implied = 57%)

The model's p_under at 5.5 ≈ 72%, edge = +18%. At 4.5 it's ≈ 86%, edge = +29%. The 4.5 line is dramatically better — the extra whole number moves the outcome probability by 14 percentage points. Line variance across bookies is often larger than odds variance on the same line.

---

### 2.7c `profile_ou_rates`

Empirical O/U rates by match profile and line. Built from settled `betfair_markets` rows. This is the ground truth calibration anchor — what actually happened historically for matches fitting each profile at each line.

```sql
CREATE TABLE profile_ou_rates (
    profile_id      TEXT PRIMARY KEY,   -- hash(sport + gap_bucket + stage + format_type + line)
    sport           TEXT NOT NULL,
    gap_bucket      TEXT NOT NULL,      -- "gap>15" | "gap10-15" | "gap5-10" | "gap<5"
    stage_group     TEXT NOT NULL,      -- "early_r1r2" | "mid_r3r4" | "late_qfsff"
    format_type     TEXT NOT NULL,      -- "short_legs" | "long_legs" | "sets" | "bo3" | "bo5"
    line            REAL NOT NULL,
    matches_count   INTEGER NOT NULL,
    under_count     INTEGER NOT NULL,
    over_count      INTEGER NOT NULL,
    void_count      INTEGER NOT NULL DEFAULT 0,
    empirical_under_rate  REAL NOT NULL,    -- under_count / (matches_count - void_count)
    empirical_over_rate   REAL NOT NULL,
    ci_95_low       REAL,               -- Wilson confidence interval lower bound
    ci_95_high      REAL,               -- Wilson confidence interval upper bound
    last_updated    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(sport, gap_bucket, stage_group, format_type, line)
);
```

**Rebuild trigger:** Rebuilt from scratch whenever new settled matches are added to `betfair_markets`. Never patched — always full rebuild.

**Minimum sample gate:** A profile cell with fewer than 20 settled matches has wide confidence intervals and should not be used as a calibration anchor. Flag with `ci_95_high - ci_95_low > 0.25`.

---

### 2.8 `model_runs`
```sql
CREATE TABLE model_runs (
    run_id          TEXT PRIMARY KEY,
    match_id        TEXT NOT NULL REFERENCES matches(match_id),
    sport           TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    run_at          TEXT NOT NULL DEFAULT (datetime('now')),
    p_under         REAL NOT NULL,
    p_over          REAL NOT NULL,
    fair_odds_under REAL NOT NULL,
    fair_odds_over  REAL NOT NULL,
    ci_95_lower     REAL,
    ci_95_upper     REAL,
    edge_under      REAL,
    edge_over       REAL,
    n_simulations   INTEGER NOT NULL DEFAULT 10000,
    seed            TEXT NOT NULL
);
```

---

### 2.9 `ledger`
```sql
CREATE TABLE ledger (
    bet_id              TEXT PRIMARY KEY,
    run_id              TEXT REFERENCES model_runs(run_id),
    match_id            TEXT NOT NULL REFERENCES matches(match_id),
    sport               TEXT NOT NULL,
    bet_direction       TEXT NOT NULL,  -- UNDER | OVER
    line                REAL NOT NULL,
    odds_taken          REAL NOT NULL,
    stake_gbp           REAL NOT NULL,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    profit_loss_gbp     REAL,
    sportmarket_order_id TEXT,
    placed_at           TEXT NOT NULL DEFAULT (datetime('now')),
    settled_at          TEXT,
    mode                TEXT NOT NULL DEFAULT 'PAPER'  -- PAPER | LIVE
);
```

**Critical rule:** Ledger entry written immediately on placement, before settlement.

---

## 3. Minimum Dataset Requirements (Phase 0 Hard Gates)

| Requirement | Minimum | Target | Verification SQL |
|-------------|---------|--------|-----------------|
| Darts matches | 300 | 500+ | `SELECT COUNT(*) FROM matches WHERE sport='darts'` |
| Darts matches with 180s | 150 | 300+ | `WHERE sport='darts' AND total_180s IS NOT NULL` |
| Snooker matches | 200 | 400+ | `WHERE sport='snooker'` |
| Snooker with centuries | 100 | 250+ | `WHERE sport='snooker' AND total_centuries IS NOT NULL` |
| Tennis matches | 500 | 2000+ | `WHERE sport='tennis'` |
| Tennis with aces | 400 | 1500+ | `WHERE sport='tennis' AND total_aces IS NOT NULL` |
| Real Betfair odds | 100 | 300+ | `WHERE verified=1` in betfair_markets |
| NULL player_ids | 0 | 0 | `WHERE player1_id IS NULL OR player2_id IS NULL` |
| Pending alias queue | 0 | 0 | `WHERE resolved_at IS NULL` in alias_review_queue |

Human must run these queries and confirm counts before Phase 1 begins.

---

<a name="model-specification"></a>
# PART 3 — Model Specification

---

## 1. The Unified Framework

All three sports share the same fundamental equation:

```
Expected total events = Expected units played × Expected events per unit

Units:  legs (darts)  /  frames (snooker)  /  service games (tennis)
Events: 180s per leg  /  centuries per frame  /  aces per service game
```

Both components are independently measurable, independently predictive, and **compound together**. The market uses season-average totals which blend all opponent types and match contexts. We estimate each component separately and multiply — that is where the edge lives.

### The Two Signals

```
MISMATCH UNDER:  skill gap → fewer units played → lower per-unit rate (close units
                 don't happen) → total events compressed below market line

PARITY OVER:     elite vs elite → more units played (contested throughout) → higher
                 per-unit rate (every unit is fought out) → total events above line
```

Signal classification happens before the model runs. The model only bets on
MISMATCH_UNDER and PARITY_OVER candidates. Everything else is passed.

```python
def classify_match(skill_gap, stage, format_max_units, sport):
    """
    Pre-filter: only run the model on high-conviction setups.
    Returns signal type or NO_SIGNAL.
    """
    # Thresholds to be calibrated per sport from universe.db
    MISMATCH_THRESHOLD = {"darts": 10.0, "snooker": 25, "tennis": 150000}  # pts gap
    PARITY_THRESHOLD   = {"darts": 5.0,  "snooker": 10, "tennis": 50000}
    LATE_STAGES        = {"QF", "SF", "F"}
    LONG_FORMAT        = {"darts": 19, "snooker": 17, "tennis": 5}  # legs/frames/sets

    gap   = skill_gap[sport]
    late  = stage in LATE_STAGES
    long_ = format_max_units >= LONG_FORMAT[sport]

    if gap > MISMATCH_THRESHOLD[sport]:
        return "MISMATCH_UNDER"

    if gap < PARITY_THRESHOLD[sport] and (late or long_):
        return "PARITY_OVER"

    return "NO_SIGNAL"
```

---

## 2. Darts Model — Full Metric Set

### 2.1 Component 1: Legs Played

The number of legs played drives the total opportunity space. It is a function of:

**Primary driver — 3-dart average differential:**
```python
def leg_win_prob(avg_a, avg_b, k=0.08):
    """
    Logistic: average differential → leg win probability.
    k = 0.08 is PLACEHOLDER — calibrate against universe.db.
    Empirical: 10pt gap → ~62-65% leg win rate.
    """
    return 1 / (1 + math.exp(-k * (avg_a - avg_b)))
```

**Secondary driver — form trajectory:**
The 3-dart average used must be the recency-weighted form window, not the season average. A player whose last 5 averages are [95, 97, 99, 101, 103] is different from one whose last 5 are [103, 101, 99, 97, 95] even if the mean is the same.

```python
def weighted_avg(recent_avgs: list[float]) -> float:
    """
    Exponential decay weighting — most recent match weighted highest.
    weights = [0.10, 0.15, 0.20, 0.25, 0.30] for 5-match window
    """
    weights = [0.10, 0.15, 0.20, 0.25, 0.30]
    return sum(w * a for w, a in zip(weights, recent_avgs))
```

**Tertiary driver — format type:**
Straight legs (first to N) vs sets format (first to N sets of first to M legs) produce different distributions even with the same total maximum. Sets format adds a structural compression layer — a player can go 3-0 in sets having won only 9 legs when the maximum was 21.

```python
FORMAT_STRUCTURE = {
    "BO7":   {"type": "legs", "legs_to_win": 4,  "max_legs": 7},
    "BO9":   {"type": "legs", "legs_to_win": 5,  "max_legs": 9},
    "BO11":  {"type": "legs", "legs_to_win": 6,  "max_legs": 11},
    "BO13":  {"type": "legs", "legs_to_win": 7,  "max_legs": 13},
    "BO19":  {"type": "legs", "legs_to_win": 10, "max_legs": 19},
    "BO21":  {"type": "legs", "legs_to_win": 11, "max_legs": 21},
    "BO35":  {"type": "legs", "legs_to_win": 18, "max_legs": 35},
    "SETS5": {"type": "sets", "sets_to_win": 3,  "legs_per_set": 5, "max_legs": 25},
    "SETS7": {"type": "sets", "sets_to_win": 4,  "legs_per_set": 5, "max_legs": 35},
}
```

**Expected legs played** from simulation:
```python
def simulate_legs_played(p_win_a, format_struct, rng, n=10000):
    """
    Returns distribution of total legs played across n simulations.
    This is Component 1 — the denominator for 180 rate.
    """
    results = []
    for _ in range(n):
        legs_a, legs_b = 0, 0
        while legs_a < format_struct["legs_to_win"] and legs_b < format_struct["legs_to_win"]:
            if rng.random() < p_win_a:
                legs_a += 1
            else:
                legs_b += 1
        results.append(legs_a + legs_b)
    return results
```

---

### 2.2 Component 2: 180s Per Leg

The rate at which 180s occur per leg played is NOT fixed — it is conditioned on match context.

**Primary driver — player's individual 180 rate per leg:**
Sourced from recent form window (last 5 matches), recency-weighted. Not season average.

```python
# Stored in player_form.avg_180s_per_leg
# Computed from: total_180s / legs_played across last N matches
# This is per leg — not per match, not per visit
```

**Secondary driver — leg competitiveness (visits per leg):**
A close leg between two evenly matched players produces more non-checkout visits → more 180 opportunities per leg. A leg that ends in 9 darts (3 visits) has far fewer 180 opportunities than one that ends in 24 darts (8 visits).

```python
def visits_per_leg(avg_winner, avg_loser, is_won_by_a):
    """
    Expected visits in a leg is a function of winner's average (efficiency)
    and whether it's competitive.

    Empirical ranges (to be calibrated):
      avg 100+ : winner closes in ~5-6 visits (15-18 darts)
      avg 90-99: winner closes in ~6-7 visits (18-21 darts)
      avg 80-89: winner closes in ~7-9 visits (21-27 darts)

    Loser gets floor(winner_visits - 1) visits minimum.
    In competitive legs (close averages), loser gets more visits.
    """
    base_visits = visits_mean_from_avg(avg_winner)
    competitiveness_bonus = max(0, (avg_loser - avg_winner + 20) / 40)
    loser_visits = max(1, int(base_visits * (0.85 + 0.15 * competitiveness_bonus)))
    return base_visits, loser_visits
```

**Critical rule — checkout visit:**
A player cannot score a 180 on their checkout visit. The last visit of the winner is always a checkout attempt — exclude it from 180 scoring. This prevents inflating projections for high-average players.

```python
def score_visit(visit_num, total_visits_this_player, player_180_rate, rng):
    is_checkout_visit = (visit_num == total_visits_this_player)
    if is_checkout_visit:
        return 0
    return 1 if rng.random() < player_180_rate else 0
```

**Tertiary driver — tournament stage / pressure:**
180 rates in World Championship finals are measurably different from qualifying rounds. Track `tournament_stage` in player_form and allow stage-specific rate lookup when sample is sufficient.

---

### 2.3 Full Darts Model Input Set

| Input | Source | Type | Notes |
|-------|--------|------|-------|
| `p1_avg_weighted` | player_form (last 5, decayed) | float | Primary skill measure |
| `p2_avg_weighted` | player_form (last 5, decayed) | float | |
| `p1_180_rate_per_leg` | player_form.avg_180s_per_leg | float | Recency-weighted |
| `p2_180_rate_per_leg` | player_form.avg_180s_per_leg | float | |
| `p1_form_trajectory` | slope of last 5 avgs | float | +ve = improving |
| `p2_form_trajectory` | slope of last 5 avgs | float | |
| `format` | matches.format | string | Drives max legs + structure |
| `tournament_stage` | matches.round | string | R1/R2/QF/SF/F |
| `skill_gap` | abs(p1_avg - p2_avg) | float | Signal classifier input |
| `signal_type` | classify_match() | string | MISMATCH_UNDER / PARITY_OVER |
| `market_line` | betfair_markets.line | float | The O/U line to evaluate |
| `market_odds_under` | betfair_markets.under_odds | float | For edge calculation |
| `market_odds_over` | betfair_markets.over_odds | float | |
| `seed` | hash(match_id + version) | int | Deterministic reproducibility |

---

## 3. Snooker Model — Full Metric Set

### 3.1 Component 1: Frames Played

**Primary driver — world ranking differential:**
Rankings are non-linear. The gap between rank 1 and rank 10 is enormous in practice; between rank 50 and rank 60 is negligible. Use ranking **points** where available, not rank number.

```python
def frame_win_prob(rank_a, rank_b, points_a=None, points_b=None):
    """
    If ranking points available, use points differential (better signal).
    If only rank numbers, use a non-linear mapping:
      top-16 vs top-16      → close to 50/50
      top-16 vs 17-50       → ~60-65% to top-16
      top-16 vs 51+         → ~70-75% to top-16
      top-8  vs 33+         → ~75-80% to top-8
    All values PLACEHOLDER — calibrate from universe.db.
    """
    if points_a and points_b:
        gap = points_a - points_b
        k = 0.000015  # PLACEHOLDER
        return 1 / (1 + math.exp(-k * gap))
    else:
        # Rank-based fallback
        rank_diff = rank_b - rank_a  # positive = a is better ranked
        k = 0.025  # PLACEHOLDER
        return 1 / (1 + math.exp(-k * rank_diff))
```

**Secondary driver — format (critical for snooker):**
Snooker formats have enormous variance — BO9 qualifiers vs BO35 World Championship final. This is the biggest single source of market mispricing: a BO9 match produces ~7-8 frames played on average; a BO35 produces ~22-25. The century count scales roughly linearly with frames played.

```python
SNOOKER_FORMATS = {
    "BO7":  {"frames_to_win": 4,  "max_frames": 7},
    "BO9":  {"frames_to_win": 5,  "max_frames": 9},
    "BO11": {"frames_to_win": 6,  "max_frames": 11},
    "BO13": {"frames_to_win": 7,  "max_frames": 13},
    "BO17": {"frames_to_win": 9,  "max_frames": 17},
    "BO19": {"frames_to_win": 10, "max_frames": 19},
    "BO25": {"frames_to_win": 13, "max_frames": 25},
    "BO33": {"frames_to_win": 17, "max_frames": 33},
    "BO35": {"frames_to_win": 18, "max_frames": 35},
}
```

**Tertiary driver — current form vs ranking:**
Rankings are updated weekly but reflect points accumulated over 12 months. A player ranked 8 who has won 3 of their last 4 matches is undervalued by their ranking. Form trajectory (last 5 results) is a leading indicator vs ranking as a lagging one.

---

### 3.2 Component 2: Centuries Per Frame

**Primary driver — player century rate per frame:**
Sourced from player_form.avg_centuries_per_frame, recency-weighted. This is the correct denominator — not per match, not per tournament.

**Secondary driver — playing style (safety vs attacking):**
This is the most important snooker-specific factor. Some top-50 players are safety specialists who rarely compile centuries even in winning frames. A match between two safety-dominant players will produce far fewer centuries than a match between two attacking players, regardless of ranking gap.

```python
# Classify player style from historical data:
# attacking: avg_centuries_per_frame > 0.35
# balanced:  0.20 - 0.35
# safety:    < 0.20

def player_style(century_rate_per_frame):
    if century_rate_per_frame > 0.35: return "attacking"
    if century_rate_per_frame > 0.20: return "balanced"
    return "safety"

# Style mismatch also matters:
# attacking vs safety → safety player kills century opportunities
# attacking vs attacking → both players compiling → OVER candidate
```

**Tertiary driver — frame competitiveness:**
A frame won 143-0 (one visit, one century, match over) is different from a frame won 80-72 (many visits, neither player reaches 100). In mismatched games, dominant players win frames in single visits → opponent gets no century attempts. In close games, both players visit frequently → more century chances for both.

```python
# Phase 1 simplified: use century rate per frame as fixed rate
# Phase 2 upgrade: condition rate on frame win probability
# (a frame won easily by the dominant player suppresses opponent century chances)

def century_prob_per_frame(player_rate, frame_win_prob_for_player):
    """
    Phase 2: adjust century rate based on who is winning frames.
    Player winning frames easily gets more single-visit opportunities.
    Player losing frames gets fewer visits.
    """
    # Winning player: rate boosted when dominant (more single-visit frames)
    # Losing player: rate suppressed when outclassed (fewer visits per frame)
    pass  # Phase 2
```

**Quaternary driver — venue / tournament tier:**
The Crucible (World Championship) has slower cloth by tradition — affects break building. Ranking events use standard cloth. This is partially captured by tournament_id and partially by the surface field in tournaments table.

---

### 3.3 Full Snooker Model Input Set

| Input | Source | Type | Notes |
|-------|--------|------|-------|
| `p1_world_ranking` | players table | int | Non-linear — use points if available |
| `p2_world_ranking` | players table | int | |
| `p1_ranking_points` | players table (optional) | float | Better signal than rank number |
| `p2_ranking_points` | players table (optional) | float | |
| `p1_century_rate_per_frame` | player_form | float | Recency-weighted, last 5 matches |
| `p2_century_rate_per_frame` | player_form | float | |
| `p1_style` | derived from century_rate | string | attacking / balanced / safety |
| `p2_style` | derived from century_rate | string | |
| `p1_form_trajectory` | slope of last 5 results | float | Win/loss trend |
| `p2_form_trajectory` | slope of last 5 results | float | |
| `format` | matches.format | string | Critical — BO9 vs BO35 are different models |
| `tournament_stage` | matches.round | string | |
| `tournament_id` | matches.tournament_id | string | Venue proxy |
| `skill_gap` | rank/points differential | float | Signal classifier input |
| `signal_type` | classify_match() | string | |
| `market_line` | betfair_markets.line | float | |
| `market_odds_under` | betfair_markets.under_odds | float | |
| `market_odds_over` | betfair_markets.over_odds | float | |
| `seed` | hash(match_id + version) | int | |

---

## 4. Tennis Model — Full Metric Set

### 4.1 The Corrected Thesis

The thesis is **duration-based, not style-based**. Skill mismatch → dominant player breaks serve regularly → match ends in fewer total games → fewer total service games → fewer total aces. Parity → both players hold serve → match goes deep → more service games → more aces.

Style (big server vs passive returner) affects the **rate** of aces per service game. Match duration driven by skill gap determines the **volume** of service games in which that rate applies. Both matter but duration is primary.

```
Expected total aces = Expected service games played × Expected aces per service game

Component 1: service games played = f(skill gap, surface, format, break rate)
Component 2: aces per service game = f(server ace rate, surface, format pressure)
```

---

### 4.2 Component 1: Service Games Played

**Primary driver — skill gap via ranking points:**
ATP/WTA ranking points (not rank number) are the correct measure. The gap between rank 1 and rank 10 in points is dramatic; between rank 50 and rank 60 is minor.

```python
def break_probability(points_a, points_b, surface):
    """
    Probability that the server loses a service game.
    Higher break prob → fewer games per set → fewer total service games.

    Surface baseline break rates (to calibrate from Sackmann data):
      clay:  ~28-32%  (slowest surface, easiest to break)
      hard:  ~22-26%
      grass: ~16-20%  (fastest surface, hardest to break)

    Skill gap adjustment: bigger ranking gap → higher break prob for better player.
    """
    surface_base = {"clay": 0.30, "hard": 0.24, "grass": 0.18}
    base = surface_base.get(surface, 0.24)
    points_gap = points_a - points_b  # positive = a is better
    k = 0.000008  # PLACEHOLDER — calibrate
    gap_adjustment = (1 / (1 + math.exp(-k * points_gap))) - 0.5
    return base + gap_adjustment * 0.15
```

**Secondary driver — surface:**
Surface is the strongest single predictor of service games per match. A grass court match between two evenly matched players goes to more games than the same players on clay because holding serve is easier on grass.

```python
# Expected games per set by surface (to calibrate from Sackmann):
EXPECTED_GAMES_PER_SET = {
    "grass": {"mismatch": 7.8, "parity": 10.2},  # 6-2 vs 7-6
    "hard":  {"mismatch": 7.2, "parity": 9.8},
    "clay":  {"mismatch": 6.8, "parity": 9.4},
}
```

**Tertiary driver — format (BO3 vs BO5):**
Grand Slams for men are BO5 — up to 5 sets. All other ATP tournaments are BO3. This doubles the maximum service game count and dramatically amplifies the parity OVER signal.

```python
FORMAT_MAX_SETS = {
    "BO3": 3,  # all ATP/WTA except Grand Slams for men
    "BO5": 5,  # ATP Grand Slams
}
```

**Quaternary driver — tiebreak rules:**
Most tournaments play a tiebreak at 6-6 in all sets. Australian Open plays a 10-point supertiebreak in the final set. Wimbledon plays a tiebreak at 12-12 in the final set. These rules affect expected games in close sets — especially important for PARITY_OVER bets.

```python
TIEBREAK_RULES = {
    "standard":       {"final_set": "tiebreak_at_6"},
    "wimbledon":      {"final_set": "tiebreak_at_12"},
    "australian_open":{"final_set": "supertiebreak"},
    "roland_garros":  {"final_set": "advantage_set"},  # no tiebreak, play until 2-game lead
}
# Roland Garros advantage final set = highest expected games count when match is close
```

---

### 4.3 Component 2: Aces Per Service Game

**Primary driver — player's ace rate per service game:**
Sourced from Sackmann data as aces / service games played, recency-weighted. Not per match, not per set.

```python
# From Sackmann columns:
# w_ace / (total service games won + total service games lost by winner)
# = aces per service game for the winner
# Do same for loser

# Store in player_form.avg_aces_per_service_game
```

**Secondary driver — surface:**
The same player hits dramatically different ace rates across surfaces. Surface-specific form is more predictive than overall form.

```python
# player_form must be segmented by surface:
# avg_aces_per_service_game_grass
# avg_aces_per_service_game_hard
# avg_aces_per_service_game_clay
```

**Tertiary driver — pressure situations:**
Tiebreaks produce higher first-serve aggression → more aces per game. The more parity in a match, the more tiebreaks are played, the more aces occur per game. This is another compounding parity effect — parity not only produces more games but also slightly higher aces per game.

**Note on return quality (secondary, not primary):**
A strong returner does reduce the opponent's ace rate per service game. But this is a second-order effect — what matters more is match duration driving total service games. Return quality belongs in the model as a rate modifier, not a primary signal.

```python
def aces_per_game_adjusted(base_ace_rate, returner_return_pts_pct):
    """
    Adjust server's base ace rate for opponent return quality.
    This is a secondary modifier — effect is real but smaller than surface.

    returner_return_pts_pct: % of server's points won by returner
    League average ~26-30%. Higher = better returner = suppresses aces.
    """
    league_avg_return = 0.28
    return_factor = league_avg_return / max(returner_return_pts_pct, 0.15)
    return base_ace_rate * return_factor
```

---

### 4.4 Full Tennis Model Input Set

| Input | Source | Type | Notes |
|-------|--------|------|-------|
| `p1_ranking_points` | players table | float | Primary skill measure |
| `p2_ranking_points` | players table | float | |
| `p1_rank` | players table | int | Fallback if points unavailable |
| `p2_rank` | players table | int | |
| `p1_ace_rate_surface` | player_form (surface-segmented) | float | Aces per service game on this surface |
| `p2_ace_rate_surface` | player_form (surface-segmented) | float | |
| `p1_return_pts_pct` | player_form | float | Secondary rate modifier |
| `p2_return_pts_pct` | player_form | float | |
| `p1_form_trajectory` | slope of last 5 surface results | float | |
| `p2_form_trajectory` | slope of last 5 surface results | float | |
| `surface` | tournaments.surface | string | grass / hard / clay — major driver |
| `format` | matches.format | string | BO3 / BO5 |
| `tiebreak_rule` | tournaments table | string | standard / wimbledon / aus_open / rg |
| `tournament_stage` | matches.round | string | R1 / R2 / QF / SF / F |
| `skill_gap` | ranking points differential | float | Signal classifier input |
| `signal_type` | classify_match() | string | MISMATCH_UNDER / PARITY_OVER |
| `market_line` | betfair_markets.line | float | |
| `market_odds_under` | betfair_markets.under_odds | float | |
| `market_odds_over` | betfair_markets.over_odds | float | |
| `seed` | hash(match_id + version) | int | |

---

## 5. Player Form — How It Is Built

Form is the single most important input. It must be built correctly.

### 5.1 Form Window Rules

```python
FORM_WINDOW   = 7     # last N matches — 7 dilutes single outlier matches better than 5
MIN_MATCHES   = 3     # below this → DATA_INSUFFICIENT, no bet
STALE_DAYS    = 30    # above this → DATA_STALE, no bet
DECAY_WEIGHTS = [0.05, 0.08, 0.12, 0.15, 0.18, 0.20, 0.22]  # oldest to most recent, 7 matches
```

### 5.2 No Look-Ahead Constraint

Form must be built using only matches **strictly before** the match being evaluated. Enforced by passing `as_of_date = match_date - 1 day` to the form builder. No exceptions.

### 5.3 Outlier Match Handling

We cannot prevent extreme outlier performances (9-darters, 147 maximums, ace storms). We do not remove them from the database — they are real data and the model's probability distribution should include their tail probability naturally.

**What we prevent** is a single outlier match contaminating the form window and feeding a distorted rate into the next bet.

```python
def build_rate_from_window(per_unit_rates: list[float], method: str = "trimmed_mean") -> float:
    """
    Build a robust per-unit rate from a window of match-level rates.

    per_unit_rates: [180s/leg, 180s/leg, ...] or [cents/frame, ...] or [aces/game, ...]
    Ordered oldest to newest (decay weighting applied after outlier handling).

    Methods:
      trimmed_mean — drop highest and lowest, weight the rest
                     best for 7-match windows, handles 9-darters cleanly
      weighted_mean — pure decay weighting, no trimming
                      use when window is short (3-4 matches)
      median        — most robust to outliers, least responsive to recent form
                      use as diagnostic check against trimmed_mean
    """
    n = len(per_unit_rates)

    if method == "trimmed_mean" and n >= 5:
        # Drop single highest and single lowest
        trimmed = sorted(per_unit_rates)[1:-1]
        # Apply decay weights to trimmed set (re-normalise)
        weights = DECAY_WEIGHTS[-(n-2):]  # take last (n-2) weights
        weights = [w / sum(weights) for w in weights]
        return sum(w * r for w, r in zip(weights, trimmed))

    elif method == "weighted_mean" or n < 5:
        weights = DECAY_WEIGHTS[-n:]
        weights = [w / sum(weights) for w in weights]
        return sum(w * r for w, r in zip(weights, per_unit_rates))

    elif method == "median":
        return sorted(per_unit_rates)[n // 2]


def flag_outlier_match(per_unit_rate: float, player_season_rate: float) -> bool:
    """
    Flag a single match as an outlier for diagnostic purposes.
    Does NOT exclude it from the database.
    Does flag it so the form builder can handle it robustly.

    A match is an outlier if its per-unit rate is > 2.5× the player's season average.
    Examples:
      Player season avg 180s/leg = 1.2 → outlier if match rate > 3.0
      (This catches 9-darter type matches without being too aggressive)
    """
    return per_unit_rate > player_season_rate * 2.5
```

**Key principle:** The model's probability distribution already assigns non-zero probability to tail events. What we're protecting against is the *rate input* being distorted, not the *output distribution* being clean. The 9-darter can happen — the model just needs an honest baseline rate to project from.

**Diagnostic check:** Always compute rate using both `trimmed_mean` and `median` and report both. If they diverge by more than 30%, there's an outlier in the window — investigate before betting.

### 5.4 Form Trajectory

The direction of form matters as much as the level.

```python
def form_trajectory(recent_rates: list[float]) -> float:
    """
    Returns the slope of the linear trend through the rate window.
    Positive = player improving (rate increasing).
    Negative = player declining (rate decreasing).

    Used in confidence scoring:
      Both players improving  → rates likely understated → amplifies parity OVER
      One player declining    → rates likely overstated → reduces confidence on bets
                                featuring that player
    """
    import numpy as np
    x = np.arange(len(recent_rates))
    slope, _ = np.polyfit(x, recent_rates, 1)
    return float(slope)
```

### 5.5 Surface-Segmented Form (Tennis Only)

Tennis form is **always** built surface-specific. Hard court form is irrelevant for predicting Wimbledon ace rates.

```python
def build_tennis_form(player_id, surface, as_of_date, db):
    """
    Returns form using only matches on the same surface before as_of_date.
    Falls back to all-surface form if fewer than 3 surface-specific matches.
    Flags surface_form_missing=True on fallback.
    """
    query = """
        SELECT m.match_date,
               CASE WHEN m.player1_id = ? THEN m.p1_aces ELSE m.p2_aces END AS aces,
               m.legs_sets_total AS games_played,
               CASE WHEN m.player1_id = ? THEN m.p1_return_pts_won_pct
                    ELSE m.p2_return_pts_won_pct END AS return_pts_pct
        FROM matches m
        JOIN tournaments t ON t.tournament_id = m.tournament_id
        WHERE (m.player1_id = ? OR m.player2_id = ?)
          AND m.match_date < ?
          AND t.surface = ?
          AND m.sport = 'tennis'
          AND m.total_aces IS NOT NULL
        ORDER BY m.match_date DESC
        LIMIT ?
    """
```

### 5.6 What Gets Stored in player_form

```sql
-- Extended player_form table (schema v1.4)
ALTER TABLE player_form ADD COLUMN avg_180s_per_leg_trimmed     REAL;  -- trimmed mean
ALTER TABLE player_form ADD COLUMN avg_180s_per_leg_median      REAL;  -- diagnostic
ALTER TABLE player_form ADD COLUMN form_trajectory              REAL;  -- slope (+ = improving)
ALTER TABLE player_form ADD COLUMN outlier_match_in_window      INTEGER DEFAULT 0;  -- flag
ALTER TABLE player_form ADD COLUMN century_style                TEXT;  -- attacking/balanced/safety
ALTER TABLE player_form ADD COLUMN avg_aces_per_service_game    REAL;  -- tennis
ALTER TABLE player_form ADD COLUMN avg_aces_grass               REAL;  -- surface-split
ALTER TABLE player_form ADD COLUMN avg_aces_hard                REAL;
ALTER TABLE player_form ADD COLUMN avg_aces_clay                REAL;
ALTER TABLE player_form ADD COLUMN avg_return_pts_pct           REAL;
ALTER TABLE player_form ADD COLUMN surface_form_missing         INTEGER DEFAULT 0;
ALTER TABLE player_form ADD COLUMN data_quality                 TEXT;  -- FULL/LOW_SAMPLE/STALE
```

---

## 6. The Simulation — Full Flow

```python
def run_model(inputs: ModelInputs, n_sims: int = 10000) -> ModelOutput:
    """
    Master simulation function — same structure for all sports.
    Returns full probability distribution over total scoring events.
    """
    rng = np.random.default_rng(inputs.seed)

    # Step 1: Pre-filter — only proceed if signal is clear
    signal = classify_match(inputs.skill_gap, inputs.stage,
                            inputs.format_max_units, inputs.sport)
    if signal == "NO_SIGNAL":
        return ModelOutput(signal="NO_SIGNAL", skip=True)

    # Step 2: Simulate units played (legs / frames / service games)
    units_dist = simulate_units_played(inputs, rng, n_sims)

    # Step 3: Simulate events per unit (180s/leg, cents/frame, aces/game)
    results = []
    for units in units_dist:
        total_events = 0
        for unit in range(units):
            rate_a = get_conditioned_rate(inputs.p1_rate, inputs, unit)
            rate_b = get_conditioned_rate(inputs.p2_rate, inputs, unit)
            events = simulate_events_in_unit(rate_a, rate_b, inputs, rng)
            total_events += events
        results.append(total_events)

    # Step 4: Calculate probability distribution at EVERY available line
    results = np.array(results)
    line_probs = compute_probs_all_lines(results, inputs.available_lines)

    return ModelOutput(
        mean=results.mean(),
        std=results.std(),
        ci_95=(np.percentile(results, 2.5), np.percentile(results, 97.5)),
        line_probs=line_probs,       # p_under and p_over at every available line
        best_bet=find_best_line(line_probs, inputs.available_markets),
        signal=signal,
        flags=compute_flags(inputs, results),
    )
```

---

## 6b. Line Scanner — Finding the Best Line Across Bookies

The model runs across every available line from every available bookie, not just the Betfair exchange line. The output is the **best edge available in the market**, which may be on a completely different line from a fixed-odds bookie.

### Why Line Variance Matters More Than Odds Variance

```
Match: Humphries (avg 102) vs qualifier (avg 84) — gap = 18pts, R1, BO11
Model expected 180s: 3.6

Available markets:
  Betfair:     Under 5.5 @ 1.85  →  market p_under = 54%  →  model p_under = 74%  →  edge = +20%
  Smarkets:    Under 5.5 @ 1.88  →  market p_under = 53%  →  model p_under = 74%  →  edge = +21%
  Bet365:      Under 4.5 @ 1.75  →  market p_under = 57%  →  model p_under = 87%  →  edge = +30%
  William Hill: Under 6.5 @ 2.10 →  market p_under = 48%  →  model p_under = 57%  →  edge = +9%

Best bet: Bet365 Under 4.5 @ 1.75 — edge is +30%, not +20%
The line is one whole number lower — that shifts p_under by 13 percentage points.
```

One whole number on the line is typically worth 10–18 percentage points in win probability depending on the sport and distribution shape. This is far larger than the 1–3% you gain from shopping odds on the same line.

### Line Scanner Implementation

```python
def compute_probs_all_lines(simulation_results: np.ndarray,
                            available_lines: list[float]) -> dict:
    """
    Computes p_under and p_over at every line in available_lines.
    Returns dict keyed by line.
    """
    n = len(simulation_results)
    return {
        line: {
            "p_under":        np.mean(simulation_results < line),
            "p_over":         np.mean(simulation_results > line),
            "fair_odds_under": n / max(np.sum(simulation_results < line), 1),
            "fair_odds_over":  n / max(np.sum(simulation_results > line), 1),
        }
        for line in available_lines
    }


def find_best_line(line_probs: dict, available_markets: list[dict],
                   direction: str = "both") -> dict | None:
    """
    Finds the single best bet across all available lines and bookies.

    available_markets: list of {bookie, line, under_odds, over_odds,
                                available_volume, min_volume_threshold}

    Returns the best market dict with added fields:
      model_p, market_p, edge, kelly_fraction, recommended_direction
    """
    MIN_EDGE    = 0.05     # 5% minimum — below this, no bet regardless of line
    MIN_VOLUME  = 50       # £50 matched minimum for exchanges
    best        = None
    best_edge   = MIN_EDGE

    for market in available_markets:
        line   = market["line"]
        probs  = line_probs.get(line)
        if probs is None:
            continue

        # Check UNDER
        if market.get("under_odds") and (
            market.get("available_volume", 999) >= MIN_VOLUME
            or market["bookie"] in FIXED_ODDS_BOOKIES
        ):
            market_p = 1 / market["under_odds"]
            edge     = probs["p_under"] - market_p
            if edge > best_edge:
                best_edge = edge
                best = {**market, "direction": "UNDER",
                        "model_p": probs["p_under"], "market_p": market_p,
                        "edge": edge}

        # Check OVER
        if market.get("over_odds") and (
            market.get("available_volume", 999) >= MIN_VOLUME
            or market["bookie"] in FIXED_ODDS_BOOKIES
        ):
            market_p = 1 / market["over_odds"]
            edge     = probs["p_over"] - market_p
            if edge > best_edge:
                best_edge = edge
                best = {**market, "direction": "OVER",
                        "model_p": probs["p_over"], "market_p": market_p,
                        "edge": edge}

    return best


FIXED_ODDS_BOOKIES = {"bet365", "william_hill", "paddy_power", "betfair_sportsbook",
                      "skybet", "coral", "ladbrokes", "unibet"}
```

### Bookie Coverage Priority

| Tier | Bookies | Line Type | Volume |
|------|---------|-----------|--------|
| 1 — Exchange (primary) | Betfair, Smarkets, via Sportmarket | Market-priced, best liquidity | £100–£5,000 |
| 2 — Fixed odds (line hunting) | Bet365, William Hill, Paddy Power | Fixed lines, often offset by 1 | £unlimited but can't be large |
| 3 — Asian books | PS3838 via Sportmarket | Sharp lines, best indicator of true price | Variable |

**Operational note:** Fixed odds bookies can limit or ban winning accounts — the opposite of Betfair. Use them for line hunting on significant edges but be aware stakes may need to be kept lower to preserve account longevity. Sportmarket's exchange routing avoids this problem entirely.

### Line Scanner in the Telegram Report

The report should show the best line found, not just the Betfair line:

```
[1] UNDER — Humphries vs Qualifier
Gap: 18pts │ BO11 │ R1 │ 14:30
Model mean: 3.6  │ Best line: 4.5 (Bet365)
Model p_under: 87% │ Market implied: 57% │ Edge: +30%
Stake: £698  │ Odds: 1.75
Alt: Betfair Under 5.5 @ 1.85 — edge +20% (if Bet365 limit hit)
```

Always show the alternative exchange line as fallback in case the fixed-odds bookie refuses the stake.

---

## 7. Model Output Contract (All Sports)

```json
{
  "run_id":         "hash(match_id + model_version + timestamp)",
  "model_version":  "v1.4",
  "match_id":       "string",
  "sport":          "darts | snooker | tennis",
  "signal_type":    "MISMATCH_UNDER | PARITY_OVER | NO_SIGNAL",
  "generated_at":   "ISO8601",
  "seed":           "integer",
  "n_simulations":  10000,

  "inputs_summary": {
    "player1_id":                "string",
    "player2_id":                "string",
    "skill_gap":                 "float",
    "p1_rate_per_unit":          "float — 180s/leg | centuries/frame | aces/svc_game",
    "p2_rate_per_unit":          "float",
    "p1_form_trajectory":        "float — positive = improving",
    "p2_form_trajectory":        "float",
    "p1_form_age_days":          "integer",
    "p2_form_age_days":          "integer",
    "format":                    "string",
    "surface":                   "string | null — tennis only",
    "tiebreak_rule":             "string | null — tennis only",
    "tournament_stage":          "string",
    "market_line":               "float"
  },

  "outputs": {
    "mean_events":       "float — expected total scoring events",
    "std_events":        "float",
    "ci_95_low":         "float",
    "ci_95_high":        "float",
    "p_under":           "float",
    "p_over":            "float",
    "fair_odds_under":   "float",
    "fair_odds_over":    "float",
    "edge_under":        "float | null — null if no market odds",
    "edge_over":         "float | null"
  },

  "flags": {
    "no_signal":            "boolean — true if classify_match returned NO_SIGNAL",
    "stale_form":           "boolean — either player form > 21 days",
    "low_sample_form":      "boolean — either player has < 5 matches in window",
    "high_uncertainty":     "boolean — ci_95 range > 6 (darts/snooker) or > 10 (tennis)",
    "line_at_mean":         "boolean — line within 0.5 of mean (low edge)",
    "surface_form_missing": "boolean — tennis: fell back to all-surface form",
    "format_anomaly":       "boolean — unrecognised format string"
  }
}
```

---

## 8. Staking — Fractional Kelly by Confidence Tier

The staking model is conviction-driven. Most days there are no bets. When the setup is perfect across all five factors, the stake is large. The goal is not to bet often — it is to bet hard on the highest-conviction setups.

### 8.1 Kelly Foundation

```python
def kelly_fraction(edge: float, odds: float) -> float:
    """
    Full Kelly criterion.
    edge  = model_probability - market_implied_probability
    odds  = decimal odds (e.g. 1.85)

    Returns fraction of bankroll to stake.
    Always positive — if edge is negative, do not bet.
    """
    if edge <= 0:
        return 0.0
    b = odds - 1        # net odds (profit per unit staked)
    p = (1/odds) + edge # model probability of winning
    q = 1 - p           # model probability of losing
    return (b * p - q) / b
```

At £3,000 bankroll, some example full Kelly stakes:

| Edge | Odds | Full Kelly % | Full Kelly £ |
|------|------|-------------|-------------|
| 5%  | 1.85 | 9.7%  | £291  |
| 10% | 1.85 | 19.4% | £582  |
| 15% | 1.85 | 29.1% | £873  |
| 20% | 1.85 | 38.8% | £1,164 |
| 10% | 2.10 | 16.5% | £495  |

Raw Kelly is too aggressive for a model with calibration uncertainty. We apply a **confidence multiplier** to fraction it down, but the base is always Kelly — not a fixed percentage.

### 8.2 The Five Confidence Factors

Every bet is scored across five factors before the Kelly fraction is applied.

```python
def compute_confidence_tier(model_output, match_context, form_data) -> str:
    """
    Returns: VERY_HIGH | HIGH | MEDIUM | LOW | NO_BET
    """
    score = 0  # accumulate points, map to tier at end

    # --- FACTOR 1: Edge magnitude ---
    edge = max(model_output.edge_under or 0, model_output.edge_over or 0)
    if   edge >= 0.18: score += 4
    elif edge >= 0.12: score += 3
    elif edge >= 0.08: score += 2
    elif edge >= 0.05: score += 1
    else: return "NO_BET"   # < 5% edge → never bet regardless of other factors

    # --- FACTOR 2: Skill gap strength ---
    # Gap in top quartile for this sport (calibrate thresholds from data)
    if match_context.skill_gap_percentile >= 75: score += 3
    elif match_context.skill_gap_percentile >= 50: score += 2
    elif match_context.skill_gap_percentile >= 25: score += 1
    # Bottom quartile: score += 0

    # --- FACTOR 3: Form data quality ---
    both_full    = all(f.data_quality == "FULL" for f in form_data)
    any_stale    = any(f.data_quality == "STALE" for f in form_data)
    outlier_flag = any(f.outlier_match_in_window for f in form_data)

    if any_stale: return "NO_BET"       # stale form → never bet
    if both_full and not outlier_flag: score += 3
    elif both_full and outlier_flag:   score += 1   # outlier in window = lower confidence
    else:                              score += 1   # low sample

    # --- FACTOR 4: Both model components agree ---
    # Both fewer units AND lower rate point same direction
    if model_output.duration_signal == model_output.rate_signal:
        score += 2  # compound effect confirmed
    else:
        score += 0  # only one component pointing → weaker signal

    # --- FACTOR 5: Stage alignment ---
    mismatch_early = (model_output.signal_type == "MISMATCH_UNDER"
                      and match_context.stage in ("R1","R2","R3"))
    parity_late    = (model_output.signal_type == "PARITY_OVER"
                      and match_context.stage in ("QF","SF","F"))
    parity_long    = (model_output.signal_type == "PARITY_OVER"
                      and match_context.format_max_units >= LONG_FORMAT_THRESHOLD)

    if mismatch_early or parity_late or parity_long:
        score += 2  # correct alignment
    else:
        score -= 1  # misaligned (e.g. mismatch bet in a final) → penalise

    # Map total score to tier
    if   score >= 12: return "VERY_HIGH"
    elif score >= 9:  return "HIGH"
    elif score >= 6:  return "MEDIUM"
    elif score >= 3:  return "LOW"
    else:             return "NO_BET"
```

### 8.3 Kelly Fraction by Tier

```python
KELLY_FRACTION = {
    "VERY_HIGH": 0.80,   # 80% of full Kelly
    "HIGH":      0.60,   # 60% of full Kelly
    "MEDIUM":    0.40,   # 40% of full Kelly
    "LOW":       0.25,   # 25% of full Kelly
    "NO_BET":    0.00,
}

def calculate_stake(bankroll, edge, odds, confidence_tier) -> float:
    full_kelly_frac = kelly_fraction(edge, odds)
    kelly_frac      = full_kelly_frac * KELLY_FRACTION[confidence_tier]
    raw_stake       = bankroll * kelly_frac

    # Hard caps
    max_single_bet    = min(1000, bankroll * 0.33)   # never > £1k or 33% of bank
    liquidity_cap     = matched_volume * 0.15         # never > 15% of available liquidity
    stake             = min(raw_stake, max_single_bet, liquidity_cap)

    return round(stake, 0)  # round to nearest £1
```

### 8.4 Concrete Stake Examples (£3,000 bankroll)

| Signal | Edge | Odds | Full Kelly | Tier | Fraction | Stake |
|--------|------|------|-----------|------|---------|-------|
| Mismatch Under, gap top 10%, perfect form, R1 | 18% | 1.85 | £873 | VERY_HIGH | 0.80 | £698 |
| Mismatch Under, gap top 25%, good form, R2 | 12% | 1.90 | £638 | HIGH | 0.60 | £383 |
| Parity Over, QF, long format, both elite | 10% | 2.05 | £524 | HIGH | 0.60 | £314 |
| Mismatch Under, moderate gap, outlier in form | 8% | 1.80 | £357 | MEDIUM | 0.40 | £143 |
| Signal present but weak, mixed components | 6% | 1.85 | £243 | LOW | 0.25 | £61 |

**Most sessions: zero bets.** The VERY_HIGH tier triggers rarely — perhaps 20-30 times per year across all three sports. That is the point. When it fires, the stake is meaningful.

### 8.5 Hard Limits (Never Exceeded)

```python
MAX_SINGLE_BET     = min(1000, bankroll * 0.33)  # £1,000 hard cap
MAX_DAILY_EXPOSURE = bankroll * 0.50             # never risk > 50% of bank in one day
MAX_CONCURRENT     = 3                           # never more than 3 open bets at once
LIQUIDITY_CAP      = matched_volume * 0.15       # move the market if exceeded
MIN_LIQUIDITY      = 50                          # don't touch markets with < £50 matched
```

### 8.6 Backtest Profiles (Run Simultaneously)

Three profiles are evaluated in the backtest — not to pick the best one, but to understand the risk/reward trade-off across styles:

- **Conservative:** flat £20 per qualifying bet
- **Moderate:** 25% Kelly across all tiers (no confidence differentiation)
- **Conviction:** full confidence-tiered Kelly as specified above

All three use the same entry criteria. Results reported separately. The conviction profile is the target live profile — but we need to see all three to understand variance.

---

## 9. Calibration Requirements

Every coefficient marked PLACEHOLDER must be calibrated against held-out data before any betting.

### Coefficients to calibrate

| Coefficient | Sport | Description |
|-------------|-------|-------------|
| `k` in leg_win_prob | Darts | Sensitivity of average differential to leg win % |
| `visits_mean_from_avg()` | Darts | Lookup table: avg → expected visits per leg |
| `k` in frame_win_prob | Snooker | Sensitivity of ranking to frame win % |
| `k` in break_probability | Tennis | Sensitivity of ranking points to break rate |
| `EXPECTED_GAMES_PER_SET` | Tennis | By surface and mismatch level |
| `SURFACE_ACE_MULTIPLIER` | Tennis | Grass / hard / clay ace rate adjustment |
| Signal thresholds | All | MISMATCH_THRESHOLD, PARITY_THRESHOLD |
| DECAY_WEIGHTS | All | Recency weighting in form window |

### Calibration acceptance criteria

| Metric | Minimum | Target |
|--------|---------|--------|
| Brier Score | < 0.25 | < 0.22 |
| Calibration curve R² | > 0.80 | > 0.90 |
| Mean absolute error (events) | < 2.0 | < 1.5 |
| Consistent across formats | No format > 3× error of others | All within 2× |

If any metric fails → adjust coefficients → rerun → notify human before backtest.

---

<a name="backtesting-framework"></a>
# PART 4 — Backtesting Framework

---

## 1. What a Valid Backtest Row Requires (ALL must be true)

| Requirement | Why |
|-------------|-----|
| Real match result from `matches` table | Prevents invented outcomes |
| Real Betfair closing odds from `betfair_markets` | Prevents assumed profitability |
| Real matched volume > £50 | Ensures market was liquid |
| Player form data existing BEFORE match date | Prevents look-ahead bias |
| Model run using only pre-match data | Prevents future data leakage |

**If any is missing → row excluded. Never filled in with estimates.**

---

## 2. Walk-Forward Methodology

```
Train:  Jan 2023 – Jun 2024
Test 1: Jul 2024 – Dec 2024
→ Add test data to training, re-calibrate
Test 2: Jan 2025 – Jun 2025
→ Repeat
```

Calibrate and test on separate windows. Same data for both = meaningless result.

---

## 3. Betting Decision Rules (Backtest = Live rules, no hindsight)

### Entry conditions (ALL must be true)

| Condition | Threshold |
|-----------|-----------|
| Edge rule | `model_prob - market_implied_prob >= 0.05` |
| Volume rule | `matched_volume >= £50` |
| Form freshness | Both players' form < 30 days old |
| Confidence | No `high_uncertainty` flag |
| Pre-match only | No in-play entries |

---

## 4. Realistic Expectations

If the mismatch theory is real:
- Win rate: **55–65%** (not 88%)
- ROI: **8–18%** (not 46% or 234%)
- Max drawdown: 10–20% of bankroll over a bad month
- Sample size for significance: **200+ bets minimum**

**If backtest shows numbers dramatically above these ranges, something is wrong with methodology. Report honestly.**

### Red flags (investigate immediately)
- Win rate > 70% on sample > 50 bets
- ROI > 30% consistently across all segments
- Zero losing streaks of more than 3 in a row
- Results not segmented by format / stage / skill gap

---

## 5. Segmented Analysis (Mandatory)

Results must be broken down by:
- Sport (darts / snooker / tennis)
- Format (BO11, BO13, SETS etc.)
- Tournament stage (early rounds vs QF+)
- Skill gap bucket (0–5, 5–10, 10–15, 15+)
- Bet direction (Under vs Over)
- Year

Aggregate results alone are insufficient.

---

<a name="agent-task-definitions"></a>
# PART 5 — Agent Task Definitions

---

## Global Rules (Apply to ALL agents)

```
RULE 1: Never fabricate data.
  If data cannot be retrieved from a real source, report BLOCKED.

RULE 2: Never report a task complete if output does not exist on disk.
  Run SELECT COUNT(*) — not "approximately N records were added".

RULE 3: Never proceed past a HARD GATE without human confirmation.

RULE 4: Never modify schema without human approval.

RULE 5: Report failures honestly.
  Partial completion is not success. Failed validation is failure.
```

---

## 1. Agent: Sifter (Data Collector)

### Role
Scrapes historical match data from approved sources using Crawl4AI. Writes to staging tables only. Never directly to `matches`.

### Tools
- **Crawl4AI `AsyncWebCrawler`** — primary scraping tool (see Part 6)
- `JsonCssExtractionStrategy` — structured CSS extraction
- `nodes.run` — Python to write staging rows via sqlite3
- `web.search` — finding tournament pages

### Approved Sources

```
Darts:
  Primary:  https://www.dartsdatabase.co.uk
  Fallback: https://www.pdc.tv/results

Snooker:
  Primary:  https://cuetracker.net
  Fallback: https://www.worldsnooker.com/results

Tennis:
  Primary:  github.com/JeffSackmann/tennis_atp (CSV — no browser scraping needed)
  Fallback: https://www.atptour.com/en/scores/results-archive
```

### Task Sequence

```
1. For each tournament without complete data:
   a. Use Crawl4AI to fetch tournament page
   b. Extract match rows via CSS schema
   c. Write to staging table (staging_darts | staging_snooker | staging_tennis)
   d. Log fetch failures to scrape_errors — do NOT guess at missing data
2. Report: rows staged, errors, failed URLs
3. HARD GATE: Human reviews staging counts before promotion to matches
```

### Failure Modes

| Failure | Required Response |
|---------|------------------|
| Page returns 404 | Log to scrape_errors, continue |
| Page structure changed | Log with screenshot, flag for human, DO NOT guess |
| Player name unrecognisable | Write raw name, leave player_id NULL |
| 180s / century data absent | Write NULL — do not estimate |
| Crawl4AI success=False | Log error_message, mark URL as BLOCKED |

---

## 2. Agent: Resolver (Identity Manager)

### Role
Maps raw player names → canonical player_ids using the three-tier confidence system.

### Resolution Algorithm

```python
def resolve(raw_name, tour, source, context=None):
    # 1. Exact match on canonical name → confidence 1.0
    # 2. Known alias in player_aliases → use stored confidence
    # 3. Fuzzy match (SequenceMatcher) against all players for tour
    # 4. If >= 0.95 → auto-accept
    # 5. If 0.80–0.94 → queue for human review → raise ResolutionQueued
    # 6. If < 0.80 → create new player entry
```

### Completion Criteria
```sql
-- Both must return 0 before Phase 1
SELECT COUNT(*) FROM matches WHERE player1_id IS NULL OR player2_id IS NULL;
SELECT COUNT(*) FROM alias_review_queue WHERE resolved_at IS NULL;
```

---

## 3. Agent: Validator (Data Quality Gate)

### Role
Runs quality checks against `universe.db`. Produces report. Does NOT fix data — only identifies problems.

### Checks
```python
CHECKS = [
    ("null_player_ids",     "SELECT COUNT(*) FROM matches WHERE player1_id IS NULL"),
    ("pending_aliases",     "SELECT COUNT(*) FROM alias_review_queue WHERE resolved_at IS NULL"),
    ("impossible_format",   "SELECT COUNT(*) FROM matches WHERE format NOT IN ('BO7','BO9','BO11','BO13','BO19','BO21','BO35','SETS_3','SETS_5','UNKNOWN')"),
    ("future_dates",        "SELECT COUNT(*) FROM matches WHERE match_date > date('now')"),
    ("synthetic_odds",      "SELECT COUNT(*) FROM betfair_markets WHERE data_source NOT IN ('betfair_historical_api','manual')"),
    ("zero_volume",         "SELECT COUNT(*) FROM betfair_markets WHERE total_matched = 0"),
    ("darts_180s_coverage", "SELECT COUNT(*) FROM matches WHERE sport='darts' AND total_180s IS NULL"),
    ("tennis_aces",         "SELECT COUNT(*) FROM matches WHERE sport='tennis' AND total_aces IS NULL"),
]
```

---

## 4. Agent: Implementer (Model Builder)

### Deliverables

```
src/model/
  darts_poisson_v1.py
  snooker_poisson_v1.py
  tennis_aces_v1.py       ← new in v1.3
  calibration_runner.py
  form_builder.py

src/backtest/
  backtest_runner.py
  walk_forward.py
```

### Forbidden Actions
- Using data from after training cutoff during calibration
- Reporting calibration metrics without running actual code
- Skipping walk-forward split
- Accepting Brier Score > 0.25 as passing

---

## 5. Agent: Auditor (Quality Assurance)

### Independence Requirement
Must be a separate agent invocation — does not share context with agent being audited.

### Audit Verdict
- **PASS:** All items confirmed, proceed
- **CONDITIONAL PASS:** Minor issues, specific corrections required
- **FAIL:** Fundamental issues, phase must be rerun

FAIL suspends the entire pipeline. Human notified with specific reasons.

---

## 6. Agent: Adapter (Execution)

### Role
Places bets via Sportmarket API. Paper mode by default. Live mode requires explicit `LIVE_MODE=true` set by human.

### Core API Pattern

```python
def place_order(session_token, betslip_id, price, stake_gbp,
                match_id, direction, run_id, duration=300):
    request_uuid = hashlib.sha256(
        f"{match_id}_{direction}_{run_id}".encode()
    ).hexdigest()[:36]

    payload = {
        "betslip_id":   betslip_id,
        "price":        price,
        "stake":        ["GBP", stake_gbp],
        "duration":     duration,
        "request_uuid": request_uuid,
        "user_data":    f"{match_id}|{direction}|{run_id}"
    }

    response = requests.post(
        f"{BASE_URL}/orders/",
        json=payload,
        headers={"Authorization": f"Bearer {session_token}"}
    )
    response.raise_for_status()
    return response.json()["order_id"]
```

### Forbidden Actions
- Placing any order without valid authorization token
- Placing orders with LIVE_MODE not explicitly true
- Omitting `request_uuid` (prevents duplicate orders)
- Polling for settlement without writing order_id to ledger first

---

## 7. Agent: Coordinator (Orchestrator)

### SOUL.md
```
You are the Coordinator for JOB-006.

Your three absolute rules:
1. Never proceed past a HARD GATE without human confirmation in chat.
2. Never accept an agent's word that a task is complete — run verification query yourself.
3. Never allow synthetic data into universe.db. If agent can't get real data, report BLOCKED.

When in doubt, stop and ask. A delayed pipeline beats a corrupted one.
```

### Escalation Triggers (alert human immediately)
- Any agent reports BLOCKED
- Auditor returns FAIL
- Any gate reached
- Pending aliases > 20
- Backtest win rate > 70% in any segment
- Any agent attempts to write to a table it does not own

### Pipeline State JSON
```json
{
  "current_phase": "DATA_COLLECTION",
  "phases": {
    "DATA_COLLECTION":     { "status": "PENDING" },
    "IDENTITY_RESOLUTION": { "status": "PENDING" },
    "DATA_VALIDATION":     { "status": "PENDING" },
    "MODEL_BUILD":         { "status": "PENDING" },
    "CALIBRATION":         { "status": "PENDING" },
    "BACKTEST":            { "status": "PENDING" },
    "PAPER_TRADING":       { "status": "PENDING" },
    "LIVE":                { "status": "LOCKED" }
  },
  "gates": {
    "GATE_1_DATA_VERIFIED":    { "status": "PENDING", "requires": "human" },
    "GATE_2_MODEL_CALIBRATED": { "status": "PENDING", "requires": "human" },
    "GATE_3_BACKTEST_REVIEWED":{ "status": "PENDING", "requires": "human" },
    "GATE_4_PAPER_VALIDATED":  { "status": "PENDING", "requires": "human" },
    "GATE_5_LIVE_APPROVED":    { "status": "LOCKED",  "requires": "human" }
  }
}
```

---

<a name="scraping-infrastructure"></a>
# PART 6 — Scraping Infrastructure — Crawl4AI

## Why Crawl4AI

The Stage 1 scrapers use Python's built-in `urllib`. These work for simple static HTML but fail on:
- JavaScript-rendered pages (DartsDatabase uses JS for stat tables)
- Bot detection / CAPTCHA challenges
- Rate limiting and IP blocks
- Pages that require scroll-to-load for full content

**Crawl4AI** (github.com/unclecode/crawl4ai, v0.8.x) replaces urllib entirely:

| Feature | Benefit |
|---------|---------|
| Async Playwright browser | Handles JS-rendered content |
| Stealth mode | Avoids bot detection |
| `JsonCssExtractionStrategy` | Returns clean structured JSON, not raw HTML |
| `BM25ContentFilter` | LLM-friendly markdown if CSS isn't precise enough |
| Caching layer (`CacheMode`) | Avoids repeat fetches during development |
| `remove_consent_popups=True` | Kills cookie banners automatically |
| Apache 2.0 licence | Free, open source, 51k+ stars |

### Install
```bash
pip install -U crawl4ai
crawl4ai-setup       # installs Playwright browsers
crawl4ai-doctor      # verify installation
```

---

## Core Crawl4AI Pattern

```python
import asyncio
import json
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai import JsonCssExtractionStrategy


async def crawl4ai_fetch(url: str, schema: dict, css_selector: str = None) -> list[dict]:
    """
    Generic Crawl4AI scrape. Returns list of extracted dicts.
    Raises ScraperError on failure — never silently returns empty.
    """
    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
    )

    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.ENABLED,            # Use local cache during dev; BYPASS in prod
        extraction_strategy=JsonCssExtractionStrategy(schema),
        css_selector=css_selector,               # Limit scope to relevant section of page
        word_count_threshold=5,
        excluded_tags=["nav", "footer", "header", "script", "style", "aside"],
        remove_overlay_elements=True,            # Kill popups / modals
        remove_consent_popups=True,              # Kill cookie banners
        exclude_external_links=True,
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(url=url, config=run_config)

    if not result.success:
        raise ScraperError(
            f"Crawl4AI failed for {url}: {result.error_message} (HTTP {result.status_code})"
        )

    rows = json.loads(result.extracted_content)
    return rows
```

---

## DartsDatabase Crawl4AI Schema

```python
# NOTE: Exact CSS selectors must be confirmed by inspecting live pages.
# The selectors below are placeholders — Stage 7 task is to verify each one.

DARTSDATABASE_MATCH_SCHEMA = {
    "name": "DartsDatabase match results",
    "baseSelector": "tr.match-row",       # CONFIRM via page inspection
    "fields": [
        {"name": "round",    "selector": "td.col-round",   "type": "text"},
        {"name": "p1_name",  "selector": "td.col-p1",      "type": "text"},
        {"name": "p1_score", "selector": "td.col-score1",  "type": "text"},
        {"name": "p2_score", "selector": "td.col-score2",  "type": "text"},
        {"name": "p2_name",  "selector": "td.col-p2",      "type": "text"},
        {"name": "p1_180s",  "selector": "td.col-180s-p1", "type": "text"},
        {"name": "p2_180s",  "selector": "td.col-180s-p2", "type": "text"},
        {"name": "avg_p1",   "selector": "td.col-avg-p1",  "type": "text"},
        {"name": "avg_p2",   "selector": "td.col-avg-p2",  "type": "text"},
    ]
}

# Usage:
async def scrape_dartsdatabase_tournament(url: str) -> list[dict]:
    return await crawl4ai_fetch(
        url=url,
        schema=DARTSDATABASE_MATCH_SCHEMA,
        css_selector="table.results-table",   # CONFIRM
    )
```

---

## CueTracker Crawl4AI Schemas

```python
# Index page: get all match links for a tournament
CUETRACKER_INDEX_SCHEMA = {
    "name": "CueTracker match links",
    "baseSelector": "a.match-link",           # CONFIRM
    "fields": [
        {"name": "href",  "selector": "a", "type": "attribute", "attribute": "href"},
        {"name": "label", "selector": "a", "type": "text"},
    ]
}

# Individual match page: get result + centuries
CUETRACKER_MATCH_SCHEMA = {
    "name": "CueTracker match result",
    "baseSelector": "div.match-result",        # CONFIRM
    "fields": [
        {"name": "p1_name",      "selector": ".player1 .name",     "type": "text"},
        {"name": "p2_name",      "selector": ".player2 .name",     "type": "text"},
        {"name": "p1_frames",    "selector": ".player1 .score",    "type": "text"},
        {"name": "p2_frames",    "selector": ".player2 .score",    "type": "text"},
        {"name": "p1_centuries", "selector": ".player1 .cents",    "type": "text"},
        {"name": "p2_centuries", "selector": ".player2 .cents",    "type": "text"},
    ]
}

# Two-phase scrape: index → individual match pages
async def scrape_cuetracker_tournament(slug: str, tournament_name: str, year: int):
    index_url = f"https://cuetracker.net/tournaments/{slug}"
    links = await crawl4ai_fetch(index_url, CUETRACKER_INDEX_SCHEMA)

    results = []
    for link in links:
        match_url = f"https://cuetracker.net{link['href']}"
        try:
            match_data = await crawl4ai_fetch(match_url, CUETRACKER_MATCH_SCHEMA)
            results.extend(match_data)
            await asyncio.sleep(1.5)       # polite delay
        except ScraperError as e:
            log_scrape_error(match_url, str(e))
    return results
```

---

## Tennis — Sackmann Dataset (No Browser Required)

Jeff Sackmann's ATP/WTA data is raw CSV on GitHub. No Crawl4AI needed.

```python
import pandas as pd

ATP_CSV_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{year}.csv"
WTA_CSV_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_{year}.csv"

def load_sackmann_year(year: int, tour: str = "atp") -> pd.DataFrame:
    url = ATP_CSV_URL.format(year=year) if tour == "atp" else WTA_CSV_URL.format(year=year)
    df = pd.read_csv(url)
    return df
```

**Columns used from Sackmann CSV:**

| Column | Use |
|--------|-----|
| `tourney_name` | Tournament name |
| `tourney_date` | Date (YYYYMMDD) |
| `surface` | hard / clay / grass |
| `round` | R32, QF, SF, F |
| `winner_name` | Player 1 raw name |
| `loser_name` | Player 2 raw name |
| `score` | Match score string |
| `w_ace` | Winner aces |
| `l_ace` | Loser aces |
| `w_svpt` | Winner serve points played |
| `l_svpt` | Loser serve points played |
| `w_1stWon` | Winner 1st serve points won |
| `l_1stWon` | Loser 1st serve points won |
| `w_2ndWon` | Winner 2nd serve points won |
| `l_2ndWon` | Loser 2nd serve points won |

**Derived return pressure proxy:**
```python
# Loser's % of winner serve points they won = return pressure
p2_return_pressure_pct = (w_svpt - (w_1stWon + w_2ndWon)) / w_svpt * 100
```

---

## Crawl4AI Caching Strategy

```python
from crawl4ai import CacheMode

CacheMode.ENABLED      # Historical scraping — cache everything, avoid re-fetching
CacheMode.BYPASS       # Production/daily run — bypass cache, always get fresh data
CacheMode.READ_ONLY    # Test mode — use cache, never write new entries
```

Use `ENABLED` for all historical data collection runs. Same tournament pages won't be fetched twice.

---

## Stage 7: Migration from urllib to Crawl4AI

Stage 7 upgrades all three Stage 1 scrapers:

| Stage 1 file | Stage 7 replacement |
|-------------|---------------------|
| `scrapers/darts/dartsdatabase.py` (urllib) | Replace `_fetch_page()` with `crawl4ai_fetch()` |
| `scrapers/snooker/cuetrackeR.py` (urllib) | Replace `_fetch_page()` with `crawl4ai_fetch()` |
| `scrapers/tennis/sackmann.py` (pandas CSV) | No change — CSV download is already optimal |

The staging table interface and resolver integration remain identical.

**When to run Stage 7:** After Stage 3 (model) is complete and the data pipeline is validated. Only upgrade the scraping layer once we know the downstream model actually works.

---

## Polite Scraping Rules

1. Minimum 1.5 seconds between page fetches
2. User-Agent: `JOB006-research-bot/1.0`
3. Respect robots.txt — disallowed paths logged as BLOCKED, not attempted
4. Read-only access only — no form submissions, no logins
5. On 429/503 — exponential backoff: 30s, 60s, 120s, then log as BLOCKED

---

<a name="build-status"></a>
# PART 7 — Build Status

## Stage 1: Complete ✓

**Files built and tested** (`~/sports-betting/src/`):

| File | Purpose | Status |
|------|---------|--------|
| `database.py` | Full SQLite schema, init, backup, hard gate queries | ✓ Tested |
| `resolver.py` | Player identity resolution with 3-tier confidence | ✓ Tested |
| `config.py` | Sport config registry (darts / snooker / tennis) | ✓ |
| `scrapers/darts/dartsdatabase.py` | Darts scraper — staging + promotion | ✓ |
| `scrapers/snooker/cuetrackeR.py` | Snooker scraper — staging + promotion | ✓ |
| `scrapers/tennis/sackmann.py` | Tennis CSV loader from Sackmann GitHub | ✓ |

**Test results:**
- `database.py` initialises `universe.db` with all tables including tennis columns
- Hard gate queries return 0 on empty DB (correct baseline)
- Resolver correctly auto-accepts `Luke Humphries` (exact match, confidence 1.0)
- Resolver correctly queues `L. Humphries` (confidence 0.85) → `ResolutionQueued`
- All staging schemas created on first run

---

## Stage 2: Next — Sportmarket Adapter

**Scope:**
```
src/execution/
  sportmarket.py    — API client: place, poll, cancel, emergency kill
  governor.py       — stake calculator, circuit breaker, paper/live switch
  ledger_writer.py  — writes to ledger table before and after settlement
  integration_test.py
```

**Acceptance criteria:**
- Paper mode places 0 real orders, writes full ledger row per intended bet
- `request_uuid` correctly prevents duplicate placement on retry
- Emergency kill switch (`close_all`) tested and confirmed working
- Ledger entry written BEFORE polling for settlement

---

## Stage 3: Model Layer — IN PROGRESS (2026-03-10)

Model architecture updated from simple Poisson to full Negative Binomial stack
based on Sports Event Totals Analytics doc (v1.0, March 2026).

### Model Architecture

```
Model A — Negative Binomial count model     negbinom.py       PRIMARY
Model B — LightGBM interaction discovery    (deferred — need more darts data)
Model C — Bayesian shrinkage (sparse)       sparse_player.py
Model D — Isotonic calibration              calibration.py
Model E — Edge detection                    edge.py
```

### Core Thesis (operationalised)
```
expected_total = opportunity_count × event_rate

opportunity_count:  legs played (darts) | frames played (snooker) | svc games (tennis)
event_rate:         180s/leg | centuries/frame | aces/svc_game

Signal A — MISMATCH → UNDER  (skill gap compresses match, fewer opportunities)
Signal B — PARITY   → OVER   (extended match, more opportunities)
```

### Three Modelling Horizons
| Horizon | Window | Purpose |
|---------|--------|---------|
| A — Core | Rolling 24-month | Structural analysis, model training |
| B — Form | Last 7–15 matches | Rate trajectory, current form |
| C — Anchor | Career | Weak prior for sparse players |

### Player Tiers (sparse handling)
| Tier | Matches in window | Treatment |
|------|------------------|-----------|
| 1 | 10+ | Full model |
| 2 | 3–9 | Moderate shrinkage toward population prior |
| 3 | 0–2 | Strong shrinkage — larger edge threshold required |

### 6-Test Validation Ladder
```
Test 1 — Descriptive:    Does mismatch → lower totals in raw data?
Test 2 — Conditional:    Does it hold within format (BO11 only)?
Test 3 — Market:         actual_under_rate vs market_implied  [DEFERRED — no odds data]
Test 4 — Profitability:  Simulate rule, track ROI + drawdown
Test 5 — Stability:      Split 2024 vs 2025 — does edge persist?
Test 6 — Execution:      Liquidity caps + slippage            [DEFERRED — needs live API]
```

### Build Order
```
[NOW]     signal_test.py      — thesis validation (does signal exist in data?)
[NOW]     form_builder.py     — populate player_form table (rolling rates)
[DAY 2]   negbinom.py         — NB simulation engine (10,000 iterations)
[DAY 2]   sparse_player.py    — tier classification + shrinkage
[DAY 3]   calibration.py      — isotonic regression calibration
[DAY 3]   edge.py             — edge detection + stake sizing
[DAY 3]   backtest.py         — walk-forward backtest, Tests 1-2+4-5
[DAY 4]   model_test.py       — integration tests
```

### Files
```
src/model/
  signal_test.py      — quick thesis test (gap → lower totals?)
  form_builder.py     — player rolling rates → player_form table
  negbinom.py         — NB count model + Monte Carlo simulation
  sparse_player.py    — Bayesian shrinkage for Tier 2/3 players
  calibration.py      — isotonic regression probability calibration
  edge.py             — edge detection, Kelly stake, BetRecommendation
  backtest.py         — walk-forward backtest, Tests 1-2+4-5
  model_test.py       — integration tests
```

### Schema Changes Required (before model build)
1. Extend `player_form` with model fields (trajectory, tier, surface rates, quality flag)
2. Add `model_run_inputs` table (JSON blob, foreign key to model_runs)
3. Add `FORMAT_EXPECTED_LEGS` lookup to config.py (darts legs proxy)
4. Add `migrate_schema()` to database.py

### Known Data Gaps
| Gap | Impact | Mitigation |
|-----|--------|------------|
| `legs_sets_total` NULL for darts | Can't compute 180s/leg directly | Proxy via FORMAT_EXPECTED_LEGS lookup |
| No ranking data | Can't compute ranking gap | Use `round` as ordinal mismatch proxy |
| `betfair_markets` empty | Can't run Test 3 or 6 | Use default_line from config; defer |

### Acceptance Criteria
- signal_test.py: mismatch < parity avg (p < 0.05) on ≥ 2 sports
- backtest ROI > 0% on edge-filtered bets across train + test split
- Edge holds in 2024 AND 2025 separately (Test 5)
- Calibration Brier score < 0.25
- No look-ahead: all form computed strictly before match date

---

## Stages 4–7: Pending (after server arrives ~2026-03-15)

| Stage | Description | Dependency |
|-------|-------------|------------|
| Stage 4 | Telegram report generator | Stage 3 validated |
| Stage 5 | Sportmarket harvester + name normalisation | Stage 3 validated |
| Stage 6 | Settlement tracker | Stage 5 |
| Stage 7 | FastAPI + dashboard wiring | Stage 5 |
| Production | DATA_DIR migration, systemd, Linux deploy | Server arrived |

---

*End of JOB-006 Master Blueprint v1.6*
