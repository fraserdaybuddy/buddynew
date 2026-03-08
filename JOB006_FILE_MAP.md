# JOB-006 File Map
## Where Everything Lives
**Last updated:** 2026-03-08

---

## Documents (blueprints, strategies, specs)

These live in `/mnt/user-data/outputs/` — the Claude outputs directory.
When you download from Claude, these are what you get.

```
/mnt/user-data/outputs/
│
├── JOB006_MASTER_BLUEPRINT.md              ← MAIN DOCUMENT (v1.3, always latest)
├── JOB006_MASTER_BLUEPRINT_v1.2_backup.md  ← Previous version backup
├── JOB006_ANALYTICS_TESTING_STRATEGY.md   ← Edge proof methodology (this is NEW)
│
├── job006-blueprint/                       ← Earlier separate section files (superseded)
│   ├── 00_BLUEPRINT_INDEX.md
│   ├── 01_DATA_ARCHITECTURE.md
│   ├── 02_MODEL_SPECIFICATION.md
│   ├── 03_BACKTEST_FRAMEWORK.md
│   └── 04_AGENT_TASK_DEFINITIONS.md
│
└── sports-betting/                         ← Python project (Stage 1 built)
    ├── data/
    │   └── universe.db                     ← SQLite database (schema exists, NO DATA YET)
    └── src/
        ├── __init__.py
        ├── database.py                     ← Schema, init, backup, gate queries
        ├── resolver.py                     ← Player identity resolution
        ├── config.py                       ← Sport config registry
        ├── scrapers/
        │   ├── darts/
        │   │   └── dartsdatabase.py        ← Darts scraper (urllib, Stage 7 → Crawl4AI)
        │   ├── snooker/
        │   │   └── cuetrackeR.py           ← Snooker scraper (urllib → Crawl4AI)
        │   └── tennis/
        │       └── sackmann.py             ← Tennis CSV loader (pandas, no change needed)
        ├── model/                          ← EMPTY — Stage 3
        ├── backtest/                       ← EMPTY — Stage 3
        ├── execution/                      ← EMPTY — Stage 2 (next)
        └── reporting/                      ← EMPTY — Stage 4
```

---

## Document Purpose Summary

| File | Purpose | Status |
|------|---------|--------|
| `JOB006_MASTER_BLUEPRINT.md` | Complete spec — data architecture, models, agents, Crawl4AI, staking | ✅ Current (v1.3) |
| `JOB006_ANALYTICS_TESTING_STRATEGY.md` | Step-by-step edge proof for each sport | ✅ New (v1.0) |
| `job006-blueprint/` folder | Earlier split docs — still valid, but blueprint supersedes them | ⚠️ Superseded |
| `sports-betting/src/` | Python code — Stage 1 complete | ✅ Stage 1 done |
| `sports-betting/data/universe.db` | The database — schema only, no data yet | ⚠️ Empty |

---

## What's Missing (Not Built Yet)

```
Stage 2: src/execution/sportmarket.py        Sportmarket API client
         src/execution/governor.py            Stake calculator + circuit breaker

Stage 3: src/model/darts_poisson_v1.py       Darts simulation
         src/model/snooker_poisson_v1.py      Snooker simulation
         src/model/tennis_aces_v1.py          Tennis aces simulation
         src/model/calibration_runner.py

Stage 4: src/reporting/telegram_formatter.py  Telegram report generator

Stage 5: src/reporting/reply_parser.py        Parse "1Y 2N 3Y" replies

Stage 6: src/execution/settlement_tracker.py  Post-match P&L settlement

Stage 7: Replace urllib in dartsdatabase.py   Crawl4AI upgrade
         Replace urllib in cuetrackeR.py
```

---

## Data Status

```
universe.db tables:    CREATED (all tables exist with correct schema)
universe.db data:      EMPTY (no matches scraped yet)

Sackmann tennis CSVs:  NOT DOWNLOADED YET
DartsDatabase data:    NOT SCRAPED YET
CueTracker data:       NOT SCRAPED YET
Betfair odds:          NOT IMPORTED YET
```

**First real task before any analytics work:** Run the scrapers to populate `universe.db`.

---

## Next Steps (In Order)

1. **Download Sackmann tennis data** — fastest, no scraping needed
   ```python
   # Run: python src/scrapers/tennis/sackmann.py --years 2022 2023 2024 2025
   ```

2. **Scrape DartsDatabase** — 2022–2025 PDC major tournaments
   ```python
   # Run: python src/scrapers/darts/dartsdatabase.py
   ```

3. **Scrape CueTracker** — 2022–2025 WST ranking events
   ```python
   # Run: python src/scrapers/snooker/cuetrackeR.py
   ```

4. **Clear alias queue** — human reviews and confirms all player identities

5. **Run analytics tests** — see JOB006_ANALYTICS_TESTING_STRATEGY.md Step 1 per sport

6. **Import Betfair odds** — required for Steps 2+ in analytics tests

7. **Stage 2: Build Sportmarket adapter**
