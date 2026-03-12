# JOB-006 Sports Betting Model — Agent Context

## Repo
**GitHub:** https://github.com/fraserdaybuddy/buddynew
**Branch:** main
**Local path (Windows):** `C:/Users/frase/Downloads/Claude/JOB006_complete_v2/sports-betting`

## What This Project Is
A sports betting model targeting stat-based over/under markets.
Core thesis: books price WHO WINS, they underprice WHAT HAPPENS DURING THE MATCH.

| Sport | Stat | Edge |
|-------|------|------|
| Darts (PDC) | 180s | Mismatch → fewer 180s / Parity → more |
| Snooker (WST) | Centuries | Same structure |
| Tennis (ATP/WTA) | Total games O/U | Big server vs passive returner → mismatch → shorter match |

## Current Status (2026-03-12)
- **Stage 2 ACTIVE** — paper testing phase, 0 settled bets (ledger cleared this session)
- **Tennis:** 5,632 ATP matches in DB (last date: 2024-12-18 — stale, live screener bypasses this)
- **Darts:** 1,230 matches in DB
- **Snooker:** 2,185 matches in DB
- **Betfair:** Live markets scraped — 1,680+ rows (Indian Wells 2026)
- **Dashboard:** Live at `http://127.0.0.1:5000` — clean, all fake/hardcoded data removed
- **Paper testing:** ACTIVE — bets logged to SQLite ledger, settled via dashboard WIN/LOSS buttons
- **Staking model:** Full Kelly + tiered optimal caps (updated this session — see Staking Model section)

## Daily Startup
```
double-click START_JOB006.bat
```
That runs: backup → scrape → screen → ledger → start server → open browser.

Or manually in PowerShell:
```powershell
cd C:\Users\frase\Downloads\Claude\JOB006_complete_v2\sports-betting
$env:PYTHONUTF8=1; python run_daily.py   # daily pipeline
$env:PYTHONUTF8=1; python run_server.py  # then open http://127.0.0.1:5000
```

## Key Files
```
START_JOB006.bat                   ← one-click daily launcher
run_daily.py                       ← daily orchestrator: backup→scrape→screen→ledger→summary
run_presession.py                  ← manual pre-session pipeline (use run_daily.py instead)
run_server.py                      ← Flask API server (port 5000)
test_all.py                        ← full system test suite
SESSION_LOG.md                     ← build history / changelog

src/
  database.py                      ← SQLite schema + helpers (get_conn, backup, init_db)
  resolver.py                      ← player identity resolution
  config.py                        ← sport config registry

  api/
    server.py                      ← Flask API (6 endpoints incl. /api/ledger/settle)

  model/
    edge.py                        ← edge screeners + Kelly staking + write_to_ledger
    simulate.py                    ← Monte Carlo match simulation (BO3/BO5)
    elo_loader.py                  ← surface-split ELO ratings
    elo_warmup.py                  ← warm-up ELO from Sackmann CSV history
    form_builder.py                ← rolling player form metrics

  execution/
    betfair.py                     ← Betfair API client (cert login, list_markets, book)
    scraper.py                     ← poll COMBINED_TOTAL + NUMBER_OF_SETS → betfair_markets
    governor.py                    ← Full Kelly + tiered_cap() + circuit breaker
    ledger_writer.py               ← ledger write helpers

  scrapers/
    tennis/sackmann.py             ← Sackmann ATP/WTA CSV loader ✓
    darts/darts24.py               ← darts24.com scraper ✓
    snooker/cuetrackeR.py          ← cuetracker.net scraper — NOT YET RUN

data/
  universe.db                      ← SQLite DB (not in git — binary)
  backups/                         ← daily DB backups (auto-created by run_daily.py)

dashboard/
  betting-dashboard.html           ← live dashboard — 5 tabs (Backtest removed), all live data
```

## API Endpoints (all at http://127.0.0.1:5000)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serves betting-dashboard.html |
| GET | `/api/status` | DB health, match counts, pipeline state |
| GET | `/api/latest-date` | Most recent betfair_markets date |
| GET | `/api/signals` | Edge screener — returns BetSignal list |
| GET | `/api/markets` | betfair_markets rows |
| GET | `/api/ledger` | Bet history + P&L summary |
| POST | `/api/ledger/settle` | Mark bet WON/LOST/VOID, compute P&L |
| POST | `/api/analyse` | ELO lookup + Monte Carlo for named players |

## Data Sources Confirmed
| Sport | Source | Stats |
|-------|--------|-------|
| Tennis | github.com/JeffSackmann/tennis_atp | Aces, df, svpt — full ATP/WTA CSV |
| Darts | darts24.com | 180s, avg, 140+, checkout % |
| Snooker | cuetracker.net (UNVERIFIED) | Centuries — not yet scraped |
| Betfair | Exchange API (cert auth) | Live O/U lines, liquidity |

## Staking Model
Full Kelly with confidence scaling and tiered bankroll caps.

### Kelly formula
```
p          = (1 / decimal_odds) × (1 + edge)     ← model prob derived from market
full_kelly = (b × p − (1−p)) / b                 ← standard Kelly fraction
             where b = decimal_odds − 1
fraction   = tier_mult × elo_confidence           ← confidence scaling (KELLY_FRACTION=1.0)
raw_frac   = full_kelly × fraction
stake      = bankroll × min(raw_frac, tiered_cap(edge))
```

### Tiered optimal caps (governor.py → tiered_cap)
Derived from Monte Carlo simulations (median growth subject to ruin < 5%, ±5% model noise):

| Edge     | Full Kelly | Cap | vs old 5% cap |
|----------|-----------|-----|---------------|
| 5–9%     | 3–6%      | 8%  | baseline      |
| 10–14%   | 6–10%     | 10% | +25%          |
| 15–19%   | 9–14%     | 12% | +67%          |
| 20–24%   | 12–17%    | 16% | +163%         |
| 25–29%   | 15–22%    | 20% | +236%         |
| 30%+     | 20–25%    | 22% | +272%         |

### Confidence scaling
```
tier_mult:
  T1 (≥10 surface matches): 1.00
  T2 (3–9 matches):          0.70
  T3 (0–2 matches):          0.40

elo_confidence:
  gap < 50 pts:  0.0  (model has no edge — block bet entirely)
  gap 50–350:    linear 0 → 1
  gap ≥ 350:     1.0
```

### Hard limits
```
MIN_STAKE = £5     (floor)
MAX_STAKE = £500   (absolute £ ceiling, after tiered cap)
MIN_EDGE  = 5%     (was 8% — lowered to surface more signals)
MIN_ODDS  = 1.60   (dashboard filter only)
```

## Database Tables
| Table | Purpose |
|-------|---------|
| `players` | Canonical player registry |
| `player_aliases` | raw_name → player_id mapping |
| `tournaments` | Tournament metadata |
| `matches` | Core match results (append-only) |
| `player_form` | Rolling form metrics per player |
| `elo_ratings` | Surface-split ELO per player |
| `betfair_markets` | Live Betfair O/U lines |
| `ledger` | All paper/live bets with full lifecycle |

## Betfair API — Confirmed Working
- Login: cert-based at `https://identitysso-cert.betfair.com/api/certlogin`
- Certs: `C:\Users\frase\client.crt` + `C:\Users\frase\client.pem`
- App key (delayed): `orGvcfyb0YqLUqaR`  App key (live): `8P5aQxQ6iXp1jEAe`
- Market types used: `COMBINED_TOTAL` (total games), `NUMBER_OF_SETS`
- `listMarketBook`: max 5 markets per call, EX_BEST_OFFERS only, no orderProjection

## Git Operations
**Use Claude Code (terminal), NOT browser Claude.**
All git work must go through the Claude Code CLI session.

## Rules (never violate)
1. Never fabricate data. If a source is unavailable, report BLOCKED.
2. Never mark a task complete if the output file doesn't exist on disk.
3. Never skip a validation step.
4. Never proceed past a HARD GATE without human confirmation.
5. NULL policy: missing stats → NULL, never 0, never estimated.
6. Paper mode always before live mode — LIVE gate requires 30 days paper P&L ≥ 0.
