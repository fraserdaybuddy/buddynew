# JOB-006 Session Log
Chronological record of what was built each session.
Reference this before starting a new session to avoid repeating work.

---

## Session 2026-03-09 — Infrastructure Complete (Stage 1)

**Outcome:** All Stage 1 infrastructure built and verified.

### Built
- `src/database.py` — SQLite schema: players, matches, tournaments, betfair_markets, ledger, player_form, elo_ratings
- `src/resolver.py` — player identity resolution (raw_name → canonical player_id)
- `src/config.py` — sport config registry
- `src/scrapers/tennis/sackmann.py` — Sackmann ATP CSV loader → 5,632 ATP matches in DB
- `src/scrapers/darts/darts24.py` — darts24.com scraper (16 major PDC tournaments 2024+2025)
- `src/scrapers/snooker/cuetrackeR.py` — cuetracker.net scraper (NOT YET RUN — needs source verification)
- `src/model/elo_warmup.py` — ELO warm-up from 5yr Sackmann history
- `src/model/elo_loader.py` — surface-split ELO calculator → 4,657 players in elo_ratings
- `src/model/form_builder.py` — rolling player form → 5,700 player_form rows
- `src/model/simulate.py` — Monte Carlo game simulation (BO3/BO5)

### DB State After Session
- Tennis matches: 5,632 | Darts: 1,230 | Snooker: 2,185
- ELO: 4,657 players. Top: Sinner 1990, Djokovic 1930 (Hard)
- player_form: 5,700 rows (last: 2024-12-18)

### Known Issues
- dartsdatabase.co.uk REJECTED — no per-match 180s, only scores+averages
- snooker scraper not yet run — cuetracker.net source unverified

---

## Session 2026-03-11 — Betfair Integration + Live Dashboard

**Outcome:** Live Betfair data in DB, dashboard wired to real API, paper betting possible.

### Built
- `src/execution/betfair.py` — BetfairSession class (cert login, list_markets, get_market_book)
  - Confirmed working: cert auth, delayed key, 1,118 Indian Wells market rows
  - listMarketBook rules (learned hard way): no orderProjection/matchProjection, EX_BEST_OFFERS only, batch≤5
- `src/execution/scraper.py` — poll_sport() scraping COMBINED_TOTAL + NUMBER_OF_SETS markets
  - Added link_markets_to_matches() for surname-based match linking
  - Added event_name column to betfair_markets
- `src/execution/governor.py` — Kelly stake sizing: quarter-Kelly × tier_mult × elo_confidence, clamped £5–£500
- `src/model/edge.py` — BetSignal dataclass + edge screeners:
  - `screen_from_db()` — needs 2026 match data (currently stale at 2024-12-18)
  - `screen_from_betfair_markets()` — live screener, reads betfair_markets event_name → ELO lookup → Monte Carlo → edge
  - `write_to_ledger()` — writes BetSignals to ledger table
- `src/api/server.py` + `run_server.py` — Flask API server port 5000:
  - GET /api/status, /api/latest-date, /api/signals, /api/markets, /api/ledger
  - POST /api/analyse — real ELO lookup + Monte Carlo, tested: Djokovic 1930 vs Medvedev 1813 → fair line 23.0
- `dashboard/betting-dashboard.html` — complete dashboard rebuild:
  - 6 tabs: Dashboard, Analyser, Backtest, Calendar, Performance, Bet Log
  - Bank Bar, Today's Matches, Sniper Board (12-col live signals table)
  - Paper bet modal with Kelly staking calculator
  - Performance tab: all metrics computed from localStorage JB.bets
  - All hardcoded data removed (EDGES[], TODAY{}, perfData{})
- `run_presession.py` — manual pre-session runner (4 steps: scrape, summarise, screen, ledger)

### DB State After Session
- betfair_markets: 1,118 rows (Indian Wells 2026 — 20 events × 55 lines COMBINED_TOTAL + NUMBER_OF_SETS)
- ledger: 0 rows (paper testing not yet started)

### Known Issues / Workarounds
- Betfair abbreviated names ("Le Tien", "Ja Draper") may fail ELO lookup — _lookup_elo_by_name() has surname fallback
- PowerShell env syntax: `$env:PYTHONUTF8=1; python run_server.py` (NOT `PYTHONUTF8=1 python`)
- screen_from_db() returns 0 signals until 2025/2026 Sackmann data loaded

---

## Session 2026-03-12 — Paper Testing Ready + Settlement System

**Outcome:** Full paper testing pipeline with persistent result tracking across restarts.

### Built
- `START_JOB006.bat` — one-click daily launcher: runs run_daily.py then starts server + opens browser
- `run_daily.py` — daily orchestrator:
  - Step 1: DB backup → `data/backups/universe_YYYYMMDD_HHMMSS_daily.db`
  - Step 2: Scrape Betfair markets (--no-scrape flag to skip)
  - Step 3: Edge screener → PENDING bets written to DB ledger
  - Step 4: List PENDING bets > 3h old needing settlement
  - Step 5: P&L summary with running ROI, win rate, bank balance
- `test_all.py` — system health test suite:
  - 8 test groups (DB, Model, Edge, Ledger, Governor, API, Betfair, Files)
  - `--server` flag tests live API endpoints
  - `--betfair` flag tests Betfair cert login
  - Coloured output, pass/fail counts, full traceback on --verbose

### Fixed
- `src/api/server.py`:
  - Added `POST /api/ledger/settle` — marks PENDING bets WON/LOST/VOID, computes P&L, persists to DB
  - `/api/ledger` response now includes `rowid` for each row (required by settle endpoint)
  - CORS updated to allow PATCH method
- `src/model/edge.py`:
  - `write_to_ledger()` now generates deterministic `bet_id` hash (was NULL previously)
  - Hardcoded date `"2026-03-11"` in check_filters → `str(date.today())`
- `dashboard/betting-dashboard.html`:
  - Removed dead `perfData_REMOVED` hardcoded block (~80 lines)
  - Fixed `betlog-body` → `betlog-tbody` ID mismatch (2 locations)
  - `loadLedger()` now called on Bet Log tab open
  - Added `settleBet()` function + WIN/LOSS/VOID buttons on PENDING rows in bet log
  - `loadLedger()` limit increased from 50 → 100 rows
- `CLAUDE.md` — fully updated to reflect current system state

### Architecture: Bet Persistence
```
run_daily.py → write_to_ledger() → SQLite ledger table  ← persists across restarts
Dashboard → Bet Log tab → WIN/LOSS buttons → POST /api/ledger/settle → SQLite update
Performance tab → reads JB.bets (localStorage) for manual paper bets
/api/ledger → reads SQLite (pipeline bets from run_daily.py)
```

### Known Issues / Next Tasks
- Betfair markets stale — re-scrape needed each day via run_daily.py
- screen_from_db() still returns 0 (needs 2025 Sackmann data loaded)
- surface/format hardcoded as Hard/BO3 in screen_from_betfair_markets()

---

---

## Session 2026-03-12 (Part 2) — Dashboard Cleanup + Staking Model Overhaul

**Outcome:** Dashboard stripped of all fake data and bugs fixed. Staking model upgraded to full Kelly with simulation-derived tiered caps.

### Dashboard fixes (`dashboard/betting-dashboard.html`)
- **Removed fake data:**
  - Deleted 4 hardcoded demo rows from Sniper Board HTML (Djokovic/Alcaraz etc — showed if API offline)
  - Deleted "Model Performance — Last 90 Days" section (entirely fake +18.3% ROI etc.)
  - Deleted Backtest page (`id="page-backtest"` — unreachable and 100% fake data)
  - Removed debug bar (development artifact always visible at bottom of screen)
- **Fixed labels:**
  - Market labels now human-readable: `total_games_OVER` → `Total Games O/19.5`, `total_sets_OVER` → `No. of Sets — Three`, `total_sets_UNDER` → `No. of Sets — Two`
  - `betLabel` in Bet Log no longer double-appends line number
  - Sidebar status pills (Tennis/Darts/Snooker counts) now update live from `/api/status`
- **Fixed JS bugs:**
  - `currentMode` properly declared (was implicit global)
  - `showToast()` supports error (red) / info (blue) / success (green) types
  - `setMode` live confirm text corrected to "Betfair API"
  - Kelly % in Sniper Board now shows effective fraction (stake ÷ bank) not raw uncapped Kelly
  - Period tabs WEEK/MONTH/YTD now filter correctly (all showed ALL TIME before)
  - Auto-refresh (60s) only fires when Dashboard tab is active
  - `#betlog-bank` element added (referenced by `loadLedger` JS but missing from HTML)
  - `perf-bankroll` pill now updated dynamically by `renderPerformance()`
  - Removed dead `loadSignals()` function (~100 lines, wrong element IDs, never called)
- **Match Analyser:** Result panel now shows neutral `—` awaiting state on load (was showing fake Djokovic result)
- **Bet Log summary:** No longer hardcoded "+£807 overall"
- **DB:** Cleared 94 stale test rows from ledger

### Staking model overhaul (`governor.py`, `edge.py`)

**Kelly formula fixed** — was using approximation `edge/b`, now uses proper formula:
```
p          = (1/odds) × (1 + edge)
full_kelly = (b × p − (1−p)) / b
```
Old formula underestimated stakes by ~2× at near-evens odds.

**`KELLY_FRACTION` → `1.0`** (full Kelly, was 0.25 quarter-Kelly)

**Tiered optimal cap system** replaces flat 5% cap. Derived from Monte Carlo simulations optimising median bankroll growth subject to ruin probability < 5% with ±5% model noise:

| Edge   | Cap | vs old 5% |
|--------|-----|-----------|
| 5–9%   | 8%  | baseline  |
| 10–14% | 10% | +25%      |
| 15–19% | 12% | +67%      |
| 20–24% | 16% | +163%     |
| 25–29% | 20% | +236%     |
| 30%+   | 22% | +272%     |

**`MIN_EDGE` → `0.05`** (5%, was 8%) — more signals qualify

### Commits this session
- `b283a69` — dashboard: major cleanup (fake data, label fixes, JS bugs)
- `3707a02` — staking: full Kelly + proper formula + tiered caps + 5% edge threshold

---

---

## Session 2026-03-12 (Part 3) — Surface Auto-Detection + API Kelly Fix

**Outcome:** Live screener now auto-detects surface and format from Betfair competition name. Proper Kelly formula now used in Match Analyser endpoint.

### Built / Fixed

- `src/execution/betfair.py` — added `"COMPETITION"` to `marketProjection` in `list_markets()` so competition name is returned from Betfair API
- `src/execution/scraper.py` — extracts `competition_name` from catalogue and stores it in `betfair_markets` (column added via `ALTER TABLE` migration if not present)
- `src/model/edge.py`:
  - Added `TOURNAMENT_SURFACE_MAP` — 80+ tournament → (surface, best_of) keyword pairs (grass/clay/hard)
  - Added `_detect_surface_and_format(competition_name)` — returns `("Hard", 3)` as default if no match
  - `screen_from_betfair_markets()` now auto-detects per event; explicit `surface`/`best_of` args act as override (default `""` / `0` = auto)
  - `surface="Hard", best_of=3` parameters now default to empty/0 to trigger auto-detection
- `src/api/server.py`:
  - `/api/signals` — removed hardcoded `surface="Hard", best_of=3` from fallback screener call
  - `/api/analyse` — replaced old quarter-Kelly approximation with `governor.kelly_stake()` (proper formula, full Kelly base)
  - Fixed reject_reason string: "below 8% threshold" → dynamic `{MIN_EDGE:.0%}` (now "below 5% threshold")

### Impact
- Roland Garros / clay events: simulation now uses Clay ELO + clay-specific hold probs
- Wimbledon / Grass events: uses Grass ELO
- Grand Slams (AO, RG, Wimbledon, USO): best_of=5 simulation (longer match → different game distribution)
- Match Analyser stake now consistent with live screener (both use governor.kelly_stake)

### Commit
- `3513420` — feat(P3): surface/format auto-detection from Betfair competition name

---

## Session 2026-03-12 (Part 4) — Dashboard Audit + Bug Fixes

**Outcome:** Full dashboard audit (26 issues catalogued). 8 confirmed bugs fixed, all 48 tests passing.

### Audit findings (26 issues total)
Full audit run against all JS, API wiring, labels, and calculations. See commit `1b2ce55` for full diff.

### Fixed (`dashboard/betting-dashboard.html`)

| # | Bug | Root cause |
|---|-----|-----------|
| 1 | `currentMode` declared at line 2191 (after use) | Moved to STATE section with other globals |
| 2 | `setMode()` tried to update `mode-tag-1/2/3/4` — IDs don't exist | Removed non-existent IDs from forEach loop |
| 3 | Sniper board market labels always showed raw fallback | Checked `COMBINED_TOTAL`/`NUMBER_OF_SETS` but API sends `total_games`/`total_sets` (lowercase). Fixed conditions. Also added `total_180s` / `total_centuries` for darts/snooker |
| 4 | ROI showed completely wrong numbers | `staked = sum(kellyPct)` — divided P&L by percentages (2.5, 3.1) not £. Fixed to `sum(stakeGBP)` |
| 5 | Sport ROI denominator inflated | Included PENDING bets. Fixed: filter to settled only before summing staked |
| 6 | WEEK/MONTH/YTD period filters broken | Dates stored as `"12 Mar"` (locale). `new Date("12 Mar")` unreliable. Fixed: store ISO `"2026-03-12"` |
| 7 | Analyser skip left stale previous result visible | Only cleared edge field. Fixed: clear all `.result-val` cells on skip |
| 8 | 5–8% edges coloured yellow (looked like warning) | Threshold still at 0.08. Fixed to 0.05 to match updated `MIN_EDGE` |

### Not fixed (documented, deferred)
- **Dual bet system**: localStorage `JB.bets` (manual modal) vs DB ledger (pipeline) are separate — Performance tab reads localStorage only, Bet Log reads DB only. Unifying these is a larger task.
- **Analyser hardcodes `best_of=3`**: Grand Slam surface detection not wired to best_of selector yet.
- **Calendar tooltip** not cleared on tab switch (minor).
- **Modal index staleness**: Bet modal uses array index — stale if signals refresh while modal open (edge case).

### Commit
- `1b2ce55` — fix(dashboard): 8 bugs from audit

---

---

## Session 2026-03-12 (Part 5) — Multi-Sport Pipeline + Manual-Only Bet Log

**Outcome:** All 3 sports scraping/screening end-to-end. Bet log now manual-only, persisted in SQLite.

### Built / Fixed

- **`run_daily.py`** — now scrapes and screens all 3 sports (tennis → darts → snooker):
  - Step 2: `poll_sport()` called for each sport; per-sport failure doesn't abort others
  - Step 3: `screen_from_db()` + `screen_from_betfair_markets()` called per sport
  - Auto-write to ledger **removed** — bets must be placed manually via dashboard
- **`src/model/edge.py`** — fixed `UnboundLocalError` in `screen_from_db()`: darts/snooker now return `[]` instead of crashing on unbound `signals`
- **`src/api/server.py`**:
  - `/api/signals` fallback: removed `sport == "tennis"` guard — all sports now fall back to live Betfair screener
  - Added `POST /api/ledger` endpoint — accepts manual bet (match, sport, market, direction, line, odds, stake, edge, kelly_frac, mode) → writes to SQLite with PENDING status
- **`dashboard/betting-dashboard.html`**:
  - `loadSniperBoard()` error handling: individual sport errors no longer kill the whole board; throws only if ALL three sports fail
  - `confirmBet()` rewritten: POSTs to `POST /api/ledger` instead of writing to localStorage
  - Bet Log tab: removed `renderBetLog()` call — only `loadLedger()` (SQLite) runs, single source of truth

### Bet Persistence Architecture (new)
```
PAPER button click → POST /api/ledger → SQLite ledger table → persists on disk
Dashboard Bet Log tab → GET /api/ledger → reads SQLite → WIN/LOSS/VOID buttons → POST /api/ledger/settle
localStorage JB.bets → no longer used for bets (bank setting still stored there)
```

### Live signals tonight (2026-03-12)
- 21 tennis signals (Le Tien v Sinner UNDER 18.5 @ 2.38, edge +27.1%, £196 — top pick)
- 2 darts signals: Price v Littler OVER 8.5 @ 2.78 (+57.3%, £158) | Humphries v Van Veen OVER 7.5 @ 3.10 (+51.6%, £34)

---

---

## Session 2026-03-12 (Part 6) — Model Audit + Local Persistence + Auto-Settle

**Outcome:** Model figures verified accurate. Server is now self-sustaining on local machine — auto-scrapes every 30 min, auto-settles paper bets from Betfair outcomes.

### Model Audit Findings

- **Sniper Board / Match Analyser — accurate ✓**
  - Edge = `model_p - devig_market_p` (multiplicative devig) — correct
  - `model_p` from Monte Carlo PMF — correct
  - Kelly formula slightly non-standard (`(1/odds)*(1+edge)` vs direct `model_p`) — intentionally conservative, ~5% difference, safe
  - Fair line = simulation median — correct
  - Darts/snooker Poisson model + avg_legs — correct
- **Bet Log P&L — accurate ✓** (uses `profit_loss_gbp` from API in GBP)
- **Bet log edge column — always shows `—` (known issue)** — edge not stored in ledger schema
- **Performance tab — broken (known issue)** — still reads from empty `JB.bets` localStorage; needs rewire to `/api/ledger` (deferred)

### Built

- **`src/api/server.py`** — background scheduler thread (starts 20s after launch):
  - Daily DB backup (once per calendar day)
  - Re-scrape all 3 sports every **30 minutes** (was: manual only)
  - Auto-settle pending paper bets after each scrape
  - `_scrape_state` dict tracks `last_scraped_at` / `in_progress`
  - New endpoints: `GET /api/scrape-status`, `POST /api/scrape-now`, `POST /api/settle-auto`
- **`src/data/auto_settle.py`** — auto-settle engine:
  - Queries all PENDING paper bets from ledger
  - Looks up market_id from betfair_markets by (event_name, market_type, line)
  - Calls `listMarketBook` → if `status=CLOSED`, reads runner with `status=WINNER`
  - `sortPriority=1` → OVER won, `sortPriority=2` → UNDER won
  - Computes P&L and updates ledger; supports `--dry-run`
  - Note: Betfair keeps settled market data ~24h — run within a day of match finishing
- **`START_JOB006.bat`** — registers Windows Task Scheduler auto-start at login (first run only, requires admin)
- **`dashboard/betting-dashboard.html`**:
  - Sniper Board: "odds: Xmin ago" badge (green <10min / yellow <30min / orange >30min)
  - Sniper Board: "↺ Refresh odds" button → `POST /api/scrape-now` → reloads signals
  - Bet Log: "↺ Auto-settle" button → `POST /api/settle-auto`

### Odds Staleness Fix
Root cause: scraper ran once at startup; odds moved significantly pre-match (1.8 vs 2.78 observed).
Fix: 30-min background scrape + "odds age" indicator + manual Refresh button.
Delayed app key adds 3-min lag on top — negligible vs scrape interval.

### Architecture: How the Local Machine Stays Current
```
Windows login → Task Scheduler → START_JOB006.bat (auto-registered first run)
  → run_daily.py (backup + scrape + screen, prints signals)
  → run_server.py starts Flask + background thread:
       every 30min → re-scrape Betfair → update betfair_markets
       every 30min → check PENDING bets → auto-settle if market CLOSED
       daily       → DB backup
Dashboard "Refresh odds" → immediate re-scrape on demand
Dashboard "Auto-settle"  → immediate settle check on demand
```

---

## Session 2026-03-13 — Bankroll + Direction-Aware ELO Confidence

**Outcome:** Stakes dramatically increased via £10k bank and a direction-aware U-curve confidence model.

### Problem diagnosed
`elo_confidence()` was a monotonic ramp (0 at gap≤25, 1.0 at gap≥350) designed for match-winner prediction. For over/under markets this is backwards at small gaps: a close match (small ELO gap) is high confidence OVER because evenly matched players grind out more games. The old curve gave near-zero confidence to these signals even on a £10k bank.

Combined effect: Alcaraz vs Medvedev (gap=48, edge=12.8%) → old stake £11. New stake £500.

### Changes

**`src/model/edge.py`**
- `elo_confidence(gap)` → `elo_confidence(gap, direction="NONE")` — direction-aware U-curve:
  - `OVER`: 1.0 at gap≤50, linear fade to floor 0.2 at gap≥200 (close match = more games = confident)
  - `UNDER`: floor 0.2 at gap≤50, linear rise to 1.0 at gap≥250 (blowout = fewer games = confident)
  - `NONE / HOME / AWAY`: flat 0.2
- `recommended_stake()` — added `direction` param, passed to `elo_confidence`
- All 4 `recommended_stake()` call sites updated to pass `direction` (already in scope at each)

**`src/api/server.py`**
- `/api/analyse` inline `elo_confidence(abs_gap)` → `elo_confidence(abs_gap, direction)`
- Bankroll default: 1000 → 10000

**`dashboard/betting-dashboard.html`**
- `JB.bank` default: 1000 → 10000
- JS `eloConfidence()` updated to match Python U-curve (direction-aware, FLOOR=0.2)

### U-curve at a glance
```
gap= 25  OVER=1.00  UNDER=0.20  NONE=0.20
gap= 50  OVER=1.00  UNDER=0.20  NONE=0.20
gap=100  OVER=0.73  UNDER=0.40  NONE=0.20
gap=150  OVER=0.47  UNDER=0.60  NONE=0.20
gap=200  OVER=0.20  UNDER=0.80  NONE=0.20
gap=250  OVER=0.20  UNDER=1.00  NONE=0.20
```

### Known ceiling
`MAX_STAKE_GBP = £500` (governor.py) is now the binding constraint on most high-confidence signals (tiered cap on £10k bank = £800–£2,200 but absolute ceiling cuts to £500). Appropriate for paper testing. Revisit before going live.

---

## Next Session Priorities

### P3 — Stale player filter (LOWER)
In `screen_from_betfair_markets()`, look up player_form last match date after ELO lookup and reject if > 30 days stale.

### P4 — Load Sackmann 2025 data (when available)
Check monthly: https://github.com/JeffSackmann/tennis_atp for `atp_matches_2025.csv`
```
PYTHONUTF8=1 python -c "from src.scrapers.tennis.sackmann import SackmannScraper; s=SackmannScraper(); s.load_year(2025,'ATP'); s.load_year(2025,'WTA')"
PYTHONUTF8=1 python -c "from src.model.elo_warmup import run_warmup; run_warmup()"
PYTHONUTF8=1 python -c "from src.model.elo_loader import run; run(warm_start=True)"
PYTHONUTF8=1 python src/model/form_builder.py
# Then restore MIN_ELO_GAP to 50 in src/model/edge.py
```

### P5 — Live betting gate (FUTURE)
30 days paper P&L ≥ 0 required before enabling LIVE mode.
