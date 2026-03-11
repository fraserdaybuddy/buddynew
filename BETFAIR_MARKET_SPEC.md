# Betfair Market Specification — JOB-006
*Target markets, exact market type codes, runner format, availability, and verification status*

---

## How Betfair O/U Markets Work

Unlike traditional bookmakers, Betfair Exchange O/U stat markets work as **binary markets**:

- Each "line" is a separate market (e.g., "Total Games Over 20.5", "Total Games Over 22.5")
- Within each market there are exactly 2 runners: **Over X.5** and **Under X.5**
- You back or lay either runner — odds reflect exchange consensus
- The line value is **baked into the runner name**, not a separate field
- To find the market closest to your fair line, you must scan all available markets and pick the best fit

**Implication for model:** `betfair.py:list_markets()` will return multiple markets for the same match (one per line). We must:
1. Fetch all markets for the match
2. Find the market whose line is closest to our model's fair line
3. Record both the line and the runners' best available odds

---

## Event Type IDs (confirmed)

| Sport   | Betfair Event Type ID | Confirmed? |
|---------|-----------------------|------------|
| Tennis  | `2`                   | YES        |
| Darts   | `3503`                | YES        |
| Snooker | `6423`                | YES        |

---

## Tennis — Target Markets

### T1. Total Games O/U
| Field              | Value                                             |
|--------------------|---------------------------------------------------|
| **Market type code** | `TOTAL_GAMES`                                   |
| **Market name (API)** | "Total Games O/U X.5" or "Total Games"         |
| **Runners**         | "Over X.5 Games" / "Under X.5 Games"            |
| **Typical lines**   | 18.5 – 34.5 (BO3); 32.5 – 54.5 (BO5)           |
| **Liquidity**       | £500 – £20,000 per market (most liquid tennis stat) |
| **Availability**    | All ATP/WTA tournaments, all rounds              |
| **Confidence**      | HIGH — confirmed market type code                |
| **Notes**           | Multiple line markets per match; pick closest to model fair line |

**Trigger conditions from model:**
- UNDER: ELO gap ≥ 250, Hard/Clay, R1–R3 → HIGH confidence
- UNDER: Clay, big server vs strong returner, gap ≥ 200 → HIGH confidence
- OVER: RG BO5, parity gap < 50, SF/F → HIGH confidence (advantage-set fat tail)
- OVER: Grass, two big servers, parity → WEAK (0.5 game signal only, deprioritised)

---

### T2. Total Sets O/U
| Field              | Value                                             |
|--------------------|---------------------------------------------------|
| **Market type code** | `TOTAL_SETS` or `NUMBER_OF_SETS`               |
| **Market name (API)** | "Number of Sets" / "Total Sets"               |
| **Runners**         | "Over 2.5 Sets" / "Under 2.5 Sets" (BO3)       |
|                     | "Over 3.5 Sets" / "Under 3.5 Sets" (BO5)       |
| **Typical lines**   | BO3: 2.5 only. BO5: 3.5 and 4.5                |
| **Liquidity**       | £200 – £5,000                                    |
| **Availability**    | Most ATP/WTA tournaments, major events           |
| **Confidence**      | HIGH — standard Betfair tennis market           |
| **Verify**          | `NUMBER_OF_SETS` vs `TOTAL_SETS` — run `listMarketCatalogue` to confirm code |

**Trigger conditions from model:**
- UNDER: BO3, gap ≥ 200 → P(straight sets) = 0.62 → GOOD
- OVER: BO5, parity QF+ → MEDIUM

---

### T3. First Set Winner
| Field              | Value                                             |
|--------------------|---------------------------------------------------|
| **Market type code** | `FIRST_SET_WINNER` or `SET_WINNER_1` or `SET_1_WINNER` |
| **Market name (API)** | "To Win 1st Set" / "1st Set Winner"           |
| **Runners**         | Player 1 name / Player 2 name                   |
| **Typical lines**   | N/A (moneyline market)                           |
| **Liquidity**       | £500 – £30,000 (very liquid — most common in-play market) |
| **Availability**    | All ATP/WTA tournaments                          |
| **Confidence**      | HIGH — exists on Betfair confirmed              |
| **Verify**          | Exact market type code — could be `FIRST_SET_WINNER` or `SET_WINNER` with set number in name |

**Trigger conditions from model:**
- FAVOURITE: gap ≥ 200, any surface → HIGH (Brier 0.193)
- FAVOURITE: gap ≥ 300, any surface → STRONG (Brier 0.177)

---

### T4. First Set Total Games O/U — PENDING
| Field              | Value                                             |
|--------------------|---------------------------------------------------|
| **Market type code** | `SET_1_TOTAL_GAMES` or `FIRST_SET_TOTAL_GAMES` (**UNVERIFIED**) |
| **Market name (API)** | "1st Set Total Games" (**unverified**)         |
| **Runners**         | Over X.5 / Under X.5                            |
| **Typical lines**   | 8.5, 9.5, 10.5, 11.5, 12.5                     |
| **Liquidity**       | Unknown — PENDING                                |
| **Availability**    | Unknown — PENDING (may only exist for majors)   |
| **Confidence**      | LOW — need to verify via API before targeting   |
| **Action**          | Run `listMarketCatalogue` with no market type filter and search for "1st Set" in name |

**Trigger conditions from model:**
- UNDER: Clay, gap ≥ 200 → PENDING check Betfair availability
- OVER: Grass, parity, big servers → PENDING check Betfair availability

---

## Darts — Target Markets

### D1. Total 180s O/U
| Field              | Value                                             |
|--------------------|---------------------------------------------------|
| **Market type code** | `TOTAL_180S`                                    |
| **Market name (API)** | "Total 180s" / "180s O/U X.5"                 |
| **Runners**         | "Over X.5" / "Under X.5"                        |
| **Typical lines**   | 3.5, 4.5, 5.5, 6.5, 7.5 (varies by match format) |
| **Liquidity**       | £50 – £2,000 (lower than tennis; major events only) |
| **Availability**    | PDC World Championship, Premier League, World Matchplay, Grand Slam, World Grand Prix. **NOT** all Players Championship events |
| **Confidence**      | HIGH — market confirmed to exist on Betfair for major PDC events |
| **Verify**          | Confirm `TOTAL_180S` is the exact API code (vs. descriptive name in market name field) |

**Trigger conditions from model:**
- UNDER: Large skill gap (avg_A − avg_B ≥ 7), early rounds → HIGH (60.9% validated, 71.4% at gap 17+)
- OVER: SF/F parity matches → MEDIUM

---

### D2. Total Legs O/U — VERIFY REQUIRED
| Field              | Value                                             |
|--------------------|---------------------------------------------------|
| **Market type code** | `TOTAL_LEGS` (**UNVERIFIED — may not exist as standalone market**) |
| **Market name (API)** | "Total Legs" (**unverified**)                  |
| **Runners**         | Over X.5 / Under X.5                            |
| **Typical lines**   | BO11: 8.5–9.5; BO13: 9.5–10.5                  |
| **Liquidity**       | Unknown — PENDING                                |
| **Availability**    | Unknown — PENDING                                |
| **Confidence**      | LOW — this market may NOT exist on Betfair      |
| **Fallback**        | If no Total Legs market: use Correct Score market (legs handicap betting). Correct score markets for darts express e.g. "7-4" which maps to 11 total legs. But this is not a clean O/U |
| **Action**          | After Betfair login: run `listMarketCatalogue` for darts with no market type filter, search for "Legs" in market names |

**Trigger conditions from model:**
- UNDER: BO11/BO13, large skill gap → HIGH (compression −30% to −46%)
- OVER: Late rounds, parity → MEDIUM

**If market doesn't exist:** This signal cannot be bet directly. Consider framing via correct score markets or handicap betting.

---

## Snooker — Target Markets

### S1. Total Frames O/U
| Field              | Value                                             |
|--------------------|---------------------------------------------------|
| **Market type code** | `TOTAL_FRAMES`                                  |
| **Market name (API)** | "Total Frames" / "Total Frames O/U X.5"        |
| **Runners**         | "Over X.5 Frames" / "Under X.5 Frames"          |
| **Typical lines**   | BO7: 5.5–6.5; BO9: 6.5–7.5; BO17: 12.5–14.5; BO25: 18.5–21.5 |
| **Liquidity**       | £100 – £5,000 (best for World Championship and Masters) |
| **Availability**    | World Championship, UK Championship, Masters, Welsh Open, Scottish Open. **Limited** for qualifying rounds |
| **Confidence**      | HIGH — standard Betfair snooker market          |
| **Verify**          | Confirm `TOTAL_FRAMES` is the exact API code    |

**Trigger conditions from model:**
- UNDER: Large skill gap (fwr diff ≥ 0.15), any format → MEDIUM (51.9% base, 66% at large gaps)
- OVER: SF/F parity, long format → HIGH

---

### S2. Total Centuries O/U
| Field              | Value                                             |
|--------------------|---------------------------------------------------|
| **Market type code** | `TOTAL_CENTURIES` (**UNVERIFIED**) or may appear as `TOTAL_CENTURIES_MATCH` |
| **Market name (API)** | "Total Centuries" (**unverified**)             |
| **Runners**         | Over X.5 / Under X.5                            |
| **Typical lines**   | 2.5, 3.5, 4.5, 5.5 (BO17/BO25 could be 8.5+)  |
| **Liquidity**       | Low — likely £50–£500 only for major events     |
| **Availability**    | Possibly only World Championship and Masters     |
| **Confidence**      | LOW — market may exist but liquidity may be insufficient |
| **Action**          | After Betfair login: verify existence and liquidity for BO25+ SF/F matches |

**Trigger conditions from model:**
- OVER: Long format SF/F BO25+ → HIGH (72.5% over rate, 100% at BO25+)
- This is the strongest snooker signal — worth pursuing even with lower liquidity

---

## Markets Confirmed NOT on Betfair

| Market               | Reason not targeted                              |
|----------------------|--------------------------------------------------|
| Tennis Aces O/U      | Does not exist as a Betfair market              |
| Tennis Total Service Games | Does not exist as a Betfair market        |
| Football corners     | Edge ~2–4%, market too well covered             |

---

## Betfair Market Search Procedure

Once Betfair login is working, run this sequence to verify all market type codes:

```bash
# 1. List all tennis markets for a known upcoming match (e.g. AO)
PYTHONUTF8=1 python -m src.execution.betfair --list-all-markets tennis \
  --from-date 2026-01-01T00:00:00Z --to-date 2026-01-31T23:59:59Z

# 2. List all darts markets for a PDC major
PYTHONUTF8=1 python -m src.execution.betfair --list-all-markets darts \
  --from-date 2026-01-01T00:00:00Z --to-date 2026-03-31T23:59:59Z

# 3. List all snooker markets for WST event
PYTHONUTF8=1 python -m src.execution.betfair --list-all-markets snooker \
  --from-date 2026-01-01T00:00:00Z --to-date 2026-04-30T23:59:59Z
```

For each sport, collect all unique `marketType` values to confirm the exact codes.

---

## DB Schema — betfair_markets Table

When markets are verified and lines are stored, `betfair_markets` should record:

```sql
CREATE TABLE IF NOT EXISTS betfair_markets (
    market_id       TEXT PRIMARY KEY,   -- Betfair market ID (e.g. "1.234567890")
    match_id        TEXT,               -- FK to matches
    sport           TEXT,               -- tennis | darts | snooker
    market_type     TEXT,               -- TOTAL_GAMES | TOTAL_SETS | FIRST_SET_WINNER | etc.
    market_name     TEXT,               -- full name from Betfair catalogue
    line            REAL,               -- X.5 line value (NULL for moneyline markets)
    runner_over_id  INTEGER,            -- selectionId for OVER runner
    runner_under_id INTEGER,            -- selectionId for UNDER runner
    over_odds       REAL,               -- best available back odds for OVER
    under_odds      REAL,               -- best available back odds for UNDER
    total_matched   REAL,               -- £ total matched volume
    verified        INTEGER DEFAULT 0,  -- 1 once we confirm line matches our target
    fetched_at      TEXT                -- timestamp of last price fetch
);
```

---

## Summary: Confidence by Market

| Market                 | Sport    | Model Signal | Market Exists? | Code Verified? |
|------------------------|----------|-------------|----------------|----------------|
| Total games O/U        | Tennis   | HIGH        | YES            | HIGH           |
| Total sets O/U         | Tennis   | GOOD        | YES            | HIGH           |
| First set winner       | Tennis   | HIGH        | YES            | HIGH           |
| First set total games  | Tennis   | PENDING     | UNKNOWN        | NO — verify    |
| Total 180s O/U         | Darts    | HIGH        | YES (majors)   | HIGH           |
| Total legs O/U         | Darts    | HIGH        | UNKNOWN        | NO — verify    |
| Total frames O/U       | Snooker  | MEDIUM/HIGH | YES (majors)   | HIGH           |
| Total centuries O/U    | Snooker  | HIGH        | UNKNOWN        | NO — verify    |
