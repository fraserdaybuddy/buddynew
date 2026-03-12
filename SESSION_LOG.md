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

## Next Session Priorities

### P1 — First real paper test run
```
double-click START_JOB006.bat
```
Fresh scrape → signals → PENDING bets in ledger. Settle via Bet Log WIN/LOSS buttons.
Watch for name-matching issues (Betfair abbreviated names vs Sackmann format).

### P2 — Load 2025 Sackmann data
DB is 15 months stale. `screen_from_db()` returns 0 signals without 2026 data.
```
Edit src/model/elo_warmup.py: extend year range 2019-2023 → 2019-2025
PYTHONUTF8=1 python src/model/elo_warmup.py
PYTHONUTF8=1 python src/scrapers/tennis/sackmann.py
```

### P3 — DONE ✓ Surface/format auto-detection
Implemented via `TOURNAMENT_SURFACE_MAP` + `competition_name` column in `betfair_markets`.

### P4 — Stale filter in live screener (do AFTER P2)
Both `p1_last_match` / `p2_last_match` passed as `None` → stale filter bypassed.
Fix: look up player_form for last match date after ELO lookup.
**Note:** fixing this BEFORE P2 will block all signals (DB is 15 months stale).
