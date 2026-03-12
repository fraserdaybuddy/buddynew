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

## Next Session Priorities

### P1 — First real paper bet
```powershell
double-click START_JOB006.bat
```
Watch terminal for signal output. After match completes → Bet Log → WIN/LOSS.

### P2 — Load 2025 Sackmann data
```
Edit src/model/elo_warmup.py: extend year range 2019-2023 → 2019-2025
PYTHONUTF8=1 python src/model/elo_warmup.py
PYTHONUTF8=1 python src/scrapers/tennis/sackmann.py  (load 2025 match data)
```
After: screen_from_db() will return signals for live matches.

### P3 — Run test suite
```powershell
$env:PYTHONUTF8=1; python test_all.py
$env:PYTHONUTF8=1; python run_server.py  # (separate terminal)
$env:PYTHONUTF8=1; python test_all.py --server
```

### P4 — Surface/format auto-detection
`screen_from_betfair_markets()` hardcodes surface="Hard" best_of=3.
Need to parse tournament name from event_name or betfair marketName.
