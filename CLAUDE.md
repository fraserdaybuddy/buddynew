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
| Tennis (ATP/WTA) | Aces | Big server vs passive returner |

## Current Status (2026-03-09)
- **Stage 1 COMPLETE** — all infrastructure built
- **Tennis:** 5,632 ATP matches in DB ✓
- **Darts:** Scraper built and RUNNING NOW (darts24.com, 16 major tournaments 2024+2025)
- **Snooker:** Scraper built, NOT yet run — needs data source verification first
- **Next after darts data:** Stage 2 — Sportmarket adapter (execution layer)

## Key Files
```
CLAUDE.md                          ← you are here
JOB006_MASTER_BLUEPRINT.md         ← full spec, model logic, build status
JOB006_FILE_MAP.md                 ← file index and data architecture
src/
  database.py                      ← SQLite schema + helpers
  resolver.py                      ← player identity resolution
  config.py                        ← sport config registry
  scrapers/
    darts/
      darts24.py                   ← PRIMARY darts scraper (darts24.com) ✓
      dartsdatabase.py             ← DEPRECATED — ignore
    snooker/
      cuetrackeR.py                ← snooker scraper — NOT yet verified/run
    tennis/
      sackmann.py                  ← tennis scraper — COMPLETE ✓
data/
  universe.db                      ← SQLite DB (not in git — binary)
```

## Data Sources Confirmed
| Sport | Source | Stats per match |
|-------|--------|-----------------|
| Darts | darts24.com | 180s, 3-dart avg, 140+, 100+, checkout %, highest checkout |
| Tennis | github.com/JeffSackmann/tennis_atp | Full ATP/WTA CSV — aces, df, svpt, etc. |
| Snooker | cuetracker.net (UNVERIFIED) | Centuries — needs checking before running |

**dartsdatabase.co.uk — REJECTED.** No per-match 180s. Only scores + averages.

## darts24.com URL Pattern
```
Results page:  https://www.darts24.com/{region}/{tournament}/{year}/results/
Stats page:    https://www.darts24.com/match/{p1-slug}/{p2-slug}/summary/stats/?mid={id}
```

## Darts Tournaments in Scope (2024 + 2025)
- PDC World Championship
- PDC Premier League
- PDC UK Open
- PDC World Matchplay
- PDC World Grand Prix
- PDC European Championship
- PDC Grand Slam of Darts
- PDC Players Championship Finals

## Database Tables
- `staging_darts` — raw scraped darts rows (PENDING → RESOLVED)
- `staging_tennis` — raw scraped tennis rows
- `matches` — promoted/validated match data
- `players` / `player_aliases` — identity resolution
- `tournaments` — tournament metadata
- `player_form` — computed form metrics (not yet populated)
- `betfair_markets` — market data (not yet populated)

## Git Operations
**Use Claude Code (terminal), NOT browser Claude.**
Browser Claude has no terminal access and cannot push to git.
All git work must go through the Claude Code CLI session.

## Rules (from blueprint — never violate)
1. Never fabricate data. If a source is unavailable, report BLOCKED.
2. Never mark a task complete if the output file doesn't exist on disk.
3. Never skip a validation step.
4. Never proceed past a HARD GATE without human confirmation.
5. NULL policy: missing stats → NULL, never 0, never estimated.
