# JOB-006 Sports Betting Intelligence System
## Production Platform — System Design
**Version:** 2.0 | **Date:** 2026-03-10 | **Author:** Fraser
**Platform:** GMK Tek Mini / Ubuntu Server 24.04 LTS
**Status:** Paper testing phase (server arrives ~2026-03-15)

---

## 1. Overview & Core Thesis

JOB-006 is a personal sports betting intelligence system targeting stat-based over/under markets.

**Core edge:** Sportsbooks price who wins. They systematically underprice what happens during the match.

| Sport | Stat | Edge Mechanism | Data Source |
|-------|------|----------------|-------------|
| Tennis (ATP) | Total Aces O/U | Big server vs passive returner + surface adj. | Sackmann ATP CSVs |
| Darts (PDC) | Total 180s O/U | Mismatch → fewer 180s / Parity → more | darts24.com |
| Snooker (WST) | Centuries O/U | Same structure as darts | cuetracker.net |

---

## 2. Current Build Status (2026-03-10)

All Stage 1 + Stage 2 components are built and tested. Data collection complete.

### Stage 1 — Data Layer ✓ COMPLETE

| Component | File | Status | Notes |
|-----------|------|--------|-------|
| Database schema | `src/database.py` | ✓ | SQLite, WAL mode, full schema |
| Player resolver | `src/resolver.py` | ✓ | Bug fixed 2026-03-10 (rollback on QUEUED) |
| Sport config | `src/config.py` | ✓ | Darts / snooker / tennis registry |
| Darts scraper | `src/scrapers/darts/darts24.py` | ✓ | 1,230 matches in DB |
| Snooker scraper | `src/scrapers/snooker/cuetrackeR.py` | ✓ | 2,185 matches in DB |
| Tennis scraper | `src/scrapers/tennis/sackmann.py` | ✓ | 5,632 matches in DB |

**DB state:** 9,047 total matches, alias_review_queue: 0 unresolved

### Stage 2 — Execution Layer ✓ COMPLETE

| Component | File | Status | Notes |
|-----------|------|--------|-------|
| Sportmarket API client | `src/execution/sportmarket.py` | ✓ | place, poll, close_all, betslip fetch |
| Governor | `src/execution/governor.py` | ✓ | Kelly sizing, circuit breaker, LIVE_MODE gate |
| Ledger writer | `src/execution/ledger_writer.py` | ✓ | Pre-placement + settlement writes |
| Integration tests | `src/execution/integration_test.py` | ✓ | 21/21 pass |

**Paper mode enforced:** LIVE_MODE=false by default. Real orders require explicit env var.

### Stages 3–7 — PENDING

| Stage | Component | Dependency |
|-------|-----------|------------|
| 3 | Poisson model + backtest | **Next — build now** |
| 4 | Telegram report generator | Stage 3 |
| 5 | Sportmarket harvester + name normalisation | Stage 3 |
| 6 | Settlement tracker | Stage 5 |
| 7 | FastAPI + dashboard wiring | Stage 5 |

---

## 3. Immediate Plan — Paper Testing (Next 5 Days)

Server arrives ~2026-03-15. Until then: validate the theory before building production.

### Phase A — Poisson Model (Stage 3)
Build and backtest the model on the 9,047 historical matches we have.

```
src/model/
  poisson.py          — core simulation (per-sport, 10,000 iterations)
  calibration.py      — fit model params to historical data
  backtest.py         — simulate betting on historical matches
  form_builder.py     — populate player_form table from matches
```

**Output needed:**
- Backtested ROI per sport (darts / snooker / tennis)
- Edge distribution — how often does model find edge > 5%?
- Calibration curves — does model_prob predict actual outcomes?
- Identify which tournament formats / player types drive the edge

### Phase B — Paper Trading Simulation (Stage 4–5 preview)
Manually feed 10–20 upcoming real matches into the model, generate recommendations,
track outcomes. No real money. Validates the full recommendation flow before production.

---

## 4. Production Build Plan (After Server Arrives)

### 4.1 Hardware & OS
- **Hardware:** GMK Tek Mini — always-on home server
- **OS:** Ubuntu Server 24.04 LTS (no desktop, minimal footprint)
- **Python:** managed via venv, dependencies pinned in `requirements.txt`
- **Database:** SQLite with WAL mode (migrate to Postgres only if write contention emerges)
- **Remote access:** Tailscale — zero-config VPN, no port forwarding required

### 4.2 Path Migration (Windows → Linux)
**Required before Linux deploy.** Current code has hardcoded Windows paths.

Replace all hardcoded paths with `DATA_DIR` env var:
```python
# Current (broken on Linux):
DB_PATH = Path("C:/Users/frase/Downloads/...")

# After migration:
import os
DATA_DIR = Path(os.environ.get("DATA_DIR", "/home/sportsbetting/data"))
DB_PATH = DATA_DIR / "universe.db"
```

Set in systemd service file:
```ini
[Service]
Environment="DATA_DIR=/home/sportsbetting/data"
Environment="LOG_DIR=/home/sportsbetting/logs"
```

Files to update: `src/database.py`, all scrapers, `src/execution/ledger_writer.py`

### 4.3 Schema Additions Required

Two tables to add to `src/database.py` before production deploy:

**`pipeline_runs`** — observability log (powers dashboard status panel):
```sql
CREATE TABLE pipeline_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    sport        TEXT,
    rows_scraped INTEGER,
    rows_promoted INTEGER,
    status       TEXT,   -- SUCCESS | FAILED
    error        TEXT    -- NULL on success
);
```

**`bets`** — full bet lifecycle (replaces/extends current `ledger` table):
```sql
-- Current ledger covers PAPER → PENDING → WON/LOST/VOID
-- Production bets table adds RECOMMENDED → APPROVED states
-- and validation_json for full audit trail
-- Implement when building Stage 5 (harvester + confirmation flow)
```

### 4.4 Config Additions Required

Add to `src/config.py` before Phase 2 automation:
```python
AUTO_BETTING      = False   # Master kill switch — set True only for Phase 2+
MAX_DAILY_STAKE   = 500.0   # Hard daily limit — code change required to alter
MIN_EDGE          = 0.05    # Minimum model edge to recommend a bet
```

### 4.5 Service Layout

Three systemd units manage the full lifecycle:

| Service | Type | Function | Schedule |
|---------|------|----------|----------|
| `scraper.service` | oneshot | Runs all active scrapers | Daily 02:00 via timer |
| `pipeline.service` | oneshot | Promotes staged rows, resolves players | After scraper |
| `api.service` | simple | FastAPI — dashboard + Sportmarket adapter | Always-on |

**Example timer unit:**
```ini
[Unit]
Description=JOB-006 Daily Scraper

[Timer]
OnCalendar=02:00
Persistent=true

[Install]
WantedBy=timers.target
```

### 4.6 FastAPI — `src/api/main.py`

| Endpoint | Method | Function |
|----------|--------|----------|
| `/api/health` | GET | Last scrape time, DB row counts, pipeline status |
| `/api/edge` | GET | Live edge opportunities (edge > 5%) |
| `/api/betlog` | GET | Recent bets with outcomes and P&L |
| `/api/backtest` | GET | Model performance stats by sport |
| `/api/analyse` | POST | Run model on a given matchup |
| `/api/bet/confirm` | POST | Human confirms a bet → validate + place |
| `/api/config` | GET/POST | Read/update AUTO_BETTING and stake limits |

### 4.7 Dashboard Confirmation Flow

**Critical constraint:** odds must be re-fetched at confirmation time, not at detection time.

```
Edge detected → row appears in dashboard
User clicks row → modal opens with full model breakdown
Modal triggers GET /api/edge/{id}/refresh → fetches current Sportmarket odds
If edge still valid → CONFIRM BET button activates
User confirms → POST /api/bet/confirm
Adapter validates → places → returns order_id
Row moves to bet log with matched odds
```

### 4.8 Networking
- Static local IP via MAC reservation in router
- Nginx on port 80 (local only) — proxies `/api/*` to FastAPI on port 8000
- No public internet exposure — Tailscale only
- If Sportmarket requires IP whitelisting: VPS relay or static residential proxy

### 4.9 First-Time Setup Sequence (Server Day)
1. Flash Ubuntu Server 24.04 LTS
2. SSH in, create `sportsbetting` system user
3. Clone repo, create venv, `pip install -r requirements.txt`
4. Set `DATA_DIR` env var, run `python -m src.database` to init DB
5. Copy `universe.db` from Windows machine via `scp` or USB
6. Install and configure Tailscale
7. Write and enable three systemd units
8. Run scrapers manually to verify, then enable timers
9. Install Nginx, configure reverse proxy
10. Run integration tests: `python -m src.execution.integration_test`

---

## 5. Execution Layer — Automation Phases

Full automation is introduced incrementally. Each phase requires the previous phase to be proven stable.

| Phase | Behaviour | Gate to proceed |
|-------|-----------|-----------------|
| Phase 1 | Every bet requires explicit human confirmation via dashboard | Default — build this first |
| Phase 2 | Auto-place if edge > threshold AND stake < limit | Phase 1 stable over 50+ bets |
| Phase 3 | Full auto with kill switch | Phase 2 stable, drawdown limits proven |

**Paper trading required:** Minimum 2 weeks in paper mode before Phase 1 live.

### Bet Lifecycle States
```
RECOMMENDED → APPROVED → PLACED → MATCHED → SETTLED
```
Every state transition written to DB immediately.
If process crashes mid-flight, state is recoverable without re-querying Sportmarket.

### Safety Rules (Non-Negotiable)
These are **constants in config** — not DB values, not env vars. Changing them requires a code commit.

| Rule | Implementation |
|------|---------------|
| Max single bet size | Hardcoded in `config.py` |
| Daily loss limit | Checked at start of each placement attempt |
| Kill switch | `AUTO_BETTING` flag in config — readable from dashboard |
| Odds re-fetch | Always re-fetched at placement moment, not at detection |
| Idempotency | Attempt logged to DB before POST sent — duplicate check on retry |
| Validation snapshot | Full state serialised to `validation_json` on every bet |

---

## 6. Backup Strategy

`universe.db` is excluded from git (binary). Must be backed up externally.

- **Recommended:** `rclone` to Backblaze B2 or S3 nightly
- **Minimum viable:** `rsync` to USB drive on same machine
- **Frequency:** Daily after scraper completes

---

## 7. Invariant Rules

These rules may not be violated by any automated process or code change.

| # | Rule | Consequence of violation |
|---|------|--------------------------|
| 1 | Never fabricate data — if source unavailable, report BLOCKED | Corrupts model training data |
| 2 | Never mark task complete if output file doesn't exist on disk | Creates false pipeline state |
| 3 | Never skip a validation step | Wrong stats attributed to wrong players |
| 4 | Never proceed past a HARD GATE without human confirmation | Irreversible financial consequences |
| 5 | NULL policy: missing stats → NULL, never 0, never estimated | Destroys model accuracy |

---

## 8. Name Normalisation — Known Gap

**Issue:** Darts names are scraped as `Surname Initial.` format (e.g., `Littler L.`, `van Gerwen M.`).
Sportmarket will send full names (`Luke Littler`, `Michael van Gerwen`).
Fuzzy match between these formats scores ~0.65 — below the 0.80 auto-accept threshold.

**Impact:** Harvester will fail to match players to DB records without a normalisation layer.

**Plan:** Build name normalisation as part of Stage 5 (Sportmarket harvester).
Add `canonical_name TEXT` column to `players` table.
Populate top ~50 PDC players with full names before harvester goes live.
Stage 3 (Poisson model) is unaffected — uses player_id directly.

---

## 9. Repository Structure

```
sports-betting/
  CLAUDE.md                          ← agent context / memory
  PRODUCTION_PLATFORM.md             ← this document
  JOB006_MASTER_BLUEPRINT.md         ← full model spec and logic
  JOB006_FILE_MAP.md                 ← file index
  requirements.txt                   ← (to be created for Linux deploy)
  src/
    database.py                      ← SQLite schema + helpers ✓
    resolver.py                      ← player identity resolution ✓
    config.py                        ← sport config registry ✓
    scrapers/
      darts/darts24.py               ← PDC scraper ✓ (1,230 matches)
      snooker/cuetrackeR.py          ← WST scraper ✓ (2,185 matches)
      tennis/sackmann.py             ← ATP scraper ✓ (5,632 matches)
    execution/
      sportmarket.py                 ← API client ✓
      governor.py                    ← Kelly + circuit breaker ✓
      ledger_writer.py               ← DB writes ✓
      integration_test.py            ← 21/21 tests pass ✓
    model/                           ← Stage 3 — build next
      poisson.py
      calibration.py
      backtest.py
      form_builder.py
    api/                             ← Stage 7 — after server arrives
      main.py
  dashboard/                         ← Stage 7 — wire after FastAPI built
    betting-dashboard.html
  data/
    universe.db                      ← NOT in git (9,047 matches)
```
