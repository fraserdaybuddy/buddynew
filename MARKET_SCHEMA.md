# Market Schema — JOB-006

Living document. Update as new exchanges and markets are enabled.
Last verified: 2026-03-11 (Betfair Exchange live API).

---

## Exchange Registry

| Exchange ID | Name | Status | Connection |
|---|---|---|---|
| `betfair_exchange` | Betfair Exchange | **LIVE** — auth working | cert + .env |
| `betfair_sportsbook` | Betfair Sportsbook | **CONFIRMED via UI** — API not wired | manual only |
| `matchroom` | Matchroom | NOT CONNECTED | — |

---

## Market Definitions

Each market has:
- **exchange_id** — which exchange it lives on
- **sport** — darts / snooker / tennis
- **market_type_code** — exact string used in API query
- **market_name** — human label as it appears on exchange
- **our_use** — what we're betting (UNDER / OVER / WINNER / skip)
- **status** — CONFIRMED / UNVERIFIED / N/A
- **liquidity_note** — observed volume or notes
- **edge_py_key** — key used in edge.py market_lines dict

---

## Betfair Exchange — Tennis

| market_type_code | market_name | our_use | status | liquidity_note | edge_py_key |
|---|---|---|---|---|---|
| `NUMBER_OF_SETS` | Number of Sets | UNDER / OVER | **CONFIRMED** — 28 markets found 2026-03-11 | moderate | `total_sets` |
| `SET_WINNER` | Set X Winner | WINNER | **CONFIRMED** — 200 markets found 2026-03-11 | high | `first_set` |
| `COMBINED_TOTAL` | Combined Total | UNDER / OVER (games) | **UNVERIFIED** — 2 found, need to check runners | — | `total_games` |
| `MATCH_ODDS` | Match Odds | skip (not our primary) | CONFIRMED | very high | — |
| `SET_BETTING` | Set Betting | skip | CONFIRMED | low | — |
| `GAME_BY_GAME_XX_YY` | Game NN (in-play) | skip | CONFIRMED (in-play only) | zero pre-match | — |
| `TOTAL_GAMES` | — | — | **DOES NOT EXIST** on Exchange | — | — |
| `TOTAL_SETS` | — | — | **DOES NOT EXIST** — use `NUMBER_OF_SETS` | — | — |
| `FIRST_SET_WINNER` | — | — | **DOES NOT EXIST** — use `SET_WINNER` | — | — |

### Tennis notes
- `TOTAL_GAMES` as seen on Betfair Sportsbook (line 21.5) is a **Sportsbook product**, not Exchange
- Exchange equivalent is `COMBINED_TOTAL` — needs runner inspection to confirm O/U format
- `SET_WINNER` covers all sets (filter by `marketName` containing "1st Set" for first-set only)
- Serving first is unknown pre-match (coin toss) — skip SET_WINNER until live data feed connected

---

## Betfair Exchange — Darts

| market_type_code | market_name | our_use | status | liquidity_note | edge_py_key |
|---|---|---|---|---|---|
| `TOTAL_180S` | Total 180s | UNDER / OVER | **CONFIRMED via UI** — Exchange + Sportsbook | PDC major events only | `total_180s` |
| `TOTAL_LEGS` | Total Legs | — | **DOES NOT EXIST** confirmed N/A | — | — |
| `MATCH_ODDS` | Match Odds | skip | CONFIRMED | high | — |

### Darts notes
- No PDC event on 2026-03-11 — markets only appear during active tournaments
- Next check: confirm `TOTAL_180S` code via `--list-all-markets darts` during a live PDC event

---

## Betfair Exchange — Snooker

| market_type_code | market_name | our_use | status | liquidity_note | edge_py_key |
|---|---|---|---|---|---|
| `TOTAL_FRAMES` | Total Frames | UNDER / OVER | **UNVERIFIED** — no current events | — | `total_frames` |
| `TOTAL_CENTURIES` | Total Centuries | UNDER / OVER | **UNVERIFIED** — no current events | — | `total_centuries` |
| `MATCH_ODDS` | Match Odds | skip | expected | — | — |

### Snooker notes
- No WST events found 2026-03-11
- When event found: run `--list-all-markets snooker` to verify codes

---

## Betfair Sportsbook — All Sports

Sportsbook is a separate product from the Exchange. API not wired yet — data collected manually.

| Sport | Market | Confirmed via UI | Notes |
|---|---|---|---|
| Darts | Total 180s | YES | Both Sportsbook and Exchange have this |
| Tennis | Total Games | YES — line 21.5 seen | Sportsbook only, not on Exchange |
| Tennis | Set 1 Winner | YES | Also on Exchange as `SET_WINNER` |
| Snooker | Any | NO | No current markets |

---

## Matchroom

| Status | Notes |
|---|---|
| NOT CONNECTED | Future — may offer darts/snooker markets at better margins |

---

## Update Checklist

When enabling a new exchange or market:
1. Add row to Exchange Registry with status
2. Add market rows with `status = UNVERIFIED`
3. Run `--list-all-markets {sport}` and `--search-markets {sport}` via betfair.py CLI
4. Confirm `market_type_code` from `description.marketType` in raw API response
5. Update status to CONFIRMED with date and count observed
6. Add `edge_py_key` and wire into `edge.py` screen_tennis / equivalent function
7. Update `STAT_MARKET_TYPES` in `betfair.py` with confirmed codes
