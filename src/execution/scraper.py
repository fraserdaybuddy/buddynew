"""
scraper.py — Betfair line logger
JOB-006 Sports Betting Model

Polls upcoming Betfair markets and stores all available O/U lines
in betfair_markets table. Designed to run nightly once server arrives.

Betfair market structure (COMBINED_TOTAL / NUMBER_OF_SETS):
  - One market per match event
  - COMBINED_TOTAL: all O/U lines are runners sharing the same handicap value
    (runner names contain "Over"/"Under"; handicap is the line)
  - NUMBER_OF_SETS: runners are "Two Sets" / "Three Sets" (BO3) or
    "2 Sets" / "3 Sets" / "4 Sets" / "5 Sets" (BO5); no handicap field.
    Stored as a single row with line=2.5 (BO3) or line=3.5 (BO5).
  - Row key: "{betfair_market_id}_{line}" to store one row per line

Confirmed market type codes (MARKET_SCHEMA.md, 2026-03-11):
  COMBINED_TOTAL  → "Total Games" (tennis) / "Total 180s" (darts)
  NUMBER_OF_SETS  → "Number of Sets" (tennis, BO3/BO5)
  TOTAL_ACES does NOT exist on Exchange.

Usage:
    PYTHONUTF8=1 python -m src.execution.scraper --sport tennis
    PYTHONUTF8=1 python -m src.execution.scraper --sport tennis --dry-run
    PYTHONUTF8=1 python -m src.execution.scraper --sport tennis --link-date 2026-03-11
"""

import logging
import argparse
from collections import defaultdict
from datetime import datetime, date as date_type, timedelta, timezone

from src.database import get_conn
from src.execution.betfair import BetfairSession, list_markets, get_market_book

log = logging.getLogger("scraper")

# Betfair market type code → (internal stat name, required substring in marketName)
# Name guard prevents pulling wrong COMBINED_TOTAL markets across sports.
# Confirmed codes: COMBINED_TOTAL (tennis Total Games, darts 180s),
#                  NUMBER_OF_SETS (tennis sets O/U)
MARKET_SPECS = {
    "darts":   [("COMBINED_TOTAL", "total_180s",   "180")],
    "snooker": [("COMBINED_TOTAL", "total_centuries", "centur")],
    "tennis":  [
        ("COMBINED_TOTAL", "total_games", "total games"),
        ("NUMBER_OF_SETS", "total_sets",  "number of sets"),
    ],
}

# Market types that use set-count runners (not handicap-based Over/Under runners)
SET_COUNT_MARKET_TYPES = {"NUMBER_OF_SETS"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _best_back(runner_book: dict) -> float | None:
    """Return best available back price for a runner, or None."""
    offers = runner_book.get("ex", {}).get("availableToBack", [])
    return offers[0]["price"] if offers else None


def _extract_lines(
    cat_runners:  list[dict],
    book_runners: list[dict],
) -> list[tuple[float, float | None, float | None, float | None]]:
    """
    Parse all Over/Under lines from a single Betfair market.

    Betfair encodes each line as a pair of runners sharing the same handicap
    value. Runner names ('Over' / 'Under') come from the catalogue; prices
    and matched volume come from the market book. Joined by selectionId.

    Returns a list of (handicap, over_odds, under_odds, total_matched)
    tuples, one per handicap level, sorted ascending.
    """
    # selectionId → 'over' | 'under'
    side_by_id = {}
    for r in cat_runners:
        name = r.get("runnerName", "").lower()
        sel  = r.get("selectionId") or r.get("id")
        if "over" in name:
            side_by_id[sel] = "over"
        elif "under" in name:
            side_by_id[sel] = "under"

    # Group book runners by handicap → {handicap: {side: price, side_matched: vol}}
    by_hcap: dict[float, dict] = defaultdict(dict)
    for runner in book_runners:
        sel_id  = runner.get("selectionId")
        hcap    = runner.get("handicap")
        side    = side_by_id.get(sel_id)
        if side is None or hcap is None:
            continue
        by_hcap[hcap][f"{side}_odds"]    = _best_back(runner)
        by_hcap[hcap][f"{side}_matched"] = runner.get("totalMatched", 0.0)

    lines = []
    for hcap, data in sorted(by_hcap.items()):
        matched = data.get("over_matched", 0.0) + data.get("under_matched", 0.0)
        lines.append((
            hcap,
            data.get("over_odds"),
            data.get("under_odds"),
            round(matched, 2) if matched > 0 else None,
        ))
    return lines


def _extract_set_count_lines(
    cat_runners:  list[dict],
    book_runners: list[dict],
) -> list[tuple[float, float | None, float | None, float | None]]:
    """
    Parse NUMBER_OF_SETS market into a synthetic O/U line.

    Runners "Two Sets"/"2 Sets" → UNDER 2.5 (BO3) or UNDER 3.5/4.5 (BO5 proxy).
    Runners "Three Sets"/"3 Sets" → OVER 2.5 (BO3).
    For BO5 we store line=3.5 with "3 Sets" as under, "4/5 Sets" summed as over.

    Returns list with exactly one tuple: (line, over_odds, under_odds, total_matched).
    Returns empty list if runners cannot be interpreted.
    """
    # Build selectionId → (runner_name, price, matched) from book
    book_by_id: dict = {}
    for r in book_runners:
        sel_id  = r.get("selectionId")
        price   = _best_back(r)
        matched = r.get("totalMatched", 0.0) or 0.0
        book_by_id[sel_id] = (price, matched)

    # Parse runner names from catalogue
    under_odds, under_matched = None, 0.0
    over_odds,  over_matched  = None, 0.0

    for r in cat_runners:
        name   = (r.get("runnerName") or "").lower().strip()
        sel_id = r.get("selectionId") or r.get("id")
        price, matched = book_by_id.get(sel_id, (None, 0.0))

        # "Two Sets" or "2 Sets" or "2 sets" → under (match ends in fewer sets)
        if name in ("two sets", "2 sets", "2"):
            under_odds    = price
            under_matched = matched
        # "Three Sets"/"3 Sets" in BO3 → over 2.5; also catches BO5 "3 sets"
        elif name in ("three sets", "3 sets", "3"):
            over_odds    = price
            over_matched = matched
        # BO5: "4 Sets", "5 Sets" — aggregate into over side (simplification)
        elif name in ("four sets", "4 sets", "4", "five sets", "5 sets", "5"):
            # Best price of whichever comes first; later winner overwrites — acceptable
            if over_odds is None:
                over_odds = price
            over_matched += matched

    if under_odds is None and over_odds is None:
        return []

    # Infer line: 2.5 for BO3, 3.5 for BO5 (heuristic: BO5 has 4/5-set runners)
    line = 2.5
    for r in cat_runners:
        name = (r.get("runnerName") or "").lower()
        if "4" in name or "five" in name or "5" in name:
            line = 3.5
            break

    total_matched = under_matched + over_matched
    return [(line, over_odds, under_odds, round(total_matched, 2) if total_matched > 0 else None)]


# ── Match linking ──────────────────────────────────────────────────────────────

def _surname(full_name: str) -> str:
    """Extract last word as a lowercase surname proxy."""
    return full_name.strip().split()[-1].lower() if full_name.strip() else ""


def link_markets_to_matches(
    conn,
    sport:      str,
    match_date: str,
    dry_run:    bool = False,
) -> int:
    """
    Resolve betfair_markets rows with match_id=NULL for the given sport/date.

    Strategy:
      1. Load all matches for sport+date with their player IDs.
      2. Load player_aliases to build surname → player_id lookup.
      3. For each unlinked betfair_markets row, parse the event name
         (Betfair format: "Surname A v Surname B") into two surnames.
      4. Look up each surname against aliases. If both resolve to a known
         player_id pair that appears in a match, link that match.
      5. Update match_id + verified=1 for matched rows.

    Returns number of rows linked.
    """
    # Load matches for this date
    rows = conn.execute(
        """SELECT match_id, player1_id, player2_id
           FROM matches
           WHERE sport = ? AND match_date = ?""",
        (sport, match_date)
    ).fetchall()
    if not rows:
        log.warning(f"[linker] No {sport} matches found for {match_date}")
        return 0

    # Build player_id → set of surname tokens from player table
    pid_surnames: dict[str, set[str]] = {}
    for r in conn.execute("SELECT player_id, full_name FROM players WHERE active = 1").fetchall():
        pid_surnames[r["player_id"]] = {_surname(r["full_name"])}

    # Also include raw_name tokens from player_aliases
    for r in conn.execute(
        "SELECT player_id, raw_name FROM player_aliases WHERE status = 'ACCEPTED'"
    ).fetchall():
        pid_surnames.setdefault(r["player_id"], set()).add(_surname(r["raw_name"]))
        # Add first word too (some Betfair names are "Surname, F." format)
        pid_surnames[r["player_id"]].add(r["raw_name"].strip().split()[0].lower())

    # Build reverse: surname_token → player_id (last-write wins for collisions)
    surname_to_pid: dict[str, str] = {}
    for pid, surnames in pid_surnames.items():
        for s in surnames:
            if s and len(s) >= 3:           # ignore very short tokens
                surname_to_pid[s] = pid

    # Build set of valid match pairs (frozenset so order-invariant)
    match_by_pair: dict[frozenset, str] = {}
    for r in rows:
        pair = frozenset([r["player1_id"], r["player2_id"]])
        match_by_pair[pair] = r["match_id"]

    # Load unlinked betfair_markets rows for this sport
    unlinked = conn.execute(
        """SELECT DISTINCT market_id,
               substr(market_id, 1, instr(market_id || '_', '_') - 1) AS betfair_mid
           FROM betfair_markets
           WHERE sport = ? AND match_id IS NULL""",
        (sport,)
    ).fetchall()

    # We need event names — re-query from the stored event name column if it exists,
    # otherwise we need to rely on a temporary in-memory map built during poll_sport.
    # Since betfair_markets doesn't store event_name, we rely on the caller passing
    # an event_map or we query markets again. Here we use a simpler approach:
    # store event_name in betfair_markets (added below) or accept partial linking.
    # For now: group unlinked rows by their base betfair market ID and use a
    # cached event_name if available in the 'data_source' field as a workaround.
    # The proper fix: add event_name column. We add it via ALTER IF NOT EXISTS.
    for col in ("event_name", "competition_name"):
        try:
            conn.execute(f"ALTER TABLE betfair_markets ADD COLUMN {col} TEXT")
            conn.commit()
            log.info(f"[linker] Added {col} column to betfair_markets")
        except Exception:
            pass  # column already exists

    # Now re-query with event_name
    unlinked = conn.execute(
        """SELECT market_id, event_name
           FROM betfair_markets
           WHERE sport = ? AND match_id IS NULL AND event_name IS NOT NULL""",
        (sport,)
    ).fetchall()

    linked = 0
    seen_mids: set[str] = set()

    for row in unlinked:
        market_id  = row["market_id"]
        event_name = row["event_name"] or ""

        # Betfair event name format: "Player A v Player B"
        parts = [p.strip() for p in event_name.split(" v ")]
        if len(parts) != 2:
            log.debug(f"[linker] Cannot parse event name: {event_name!r}")
            continue

        name_a, name_b = parts
        # Try to match each name to a player_id via surname
        pid_a = surname_to_pid.get(_surname(name_a))
        pid_b = surname_to_pid.get(_surname(name_b))

        if not pid_a or not pid_b:
            log.debug(f"[linker] Unresolved: {name_a!r}→{pid_a}  {name_b!r}→{pid_b}")
            continue

        pair = frozenset([pid_a, pid_b])
        match_id = match_by_pair.get(pair)
        if not match_id:
            log.debug(f"[linker] No match found for pair {pid_a} / {pid_b} on {match_date}")
            continue

        if dry_run:
            log.info(f"[linker] DRY-RUN: {event_name!r} → {match_id}")
            linked += 1
            continue

        # Update all rows for this market_id (multiple lines per market)
        n = conn.execute(
            """UPDATE betfair_markets
               SET match_id = ?, verified = 1
               WHERE market_id LIKE ? AND match_id IS NULL""",
            (match_id, f"{market_id.split('_')[0]}%")
        ).rowcount
        log.info(f"[linker] {event_name!r} → {match_id} ({n} rows linked)")
        linked += n

    if not dry_run:
        conn.commit()
    return linked


# ── Core poll ─────────────────────────────────────────────────────────────────

def poll_sport(
    session:    BetfairSession,
    sport:      str,
    days_ahead: int  = 2,
    dry_run:    bool = False,
    link_date:  str  = "",   # YYYY-MM-DD — if set, run match linker after poll
) -> int:
    """
    Poll upcoming markets for a sport and upsert all lines into betfair_markets.
    Each (market, line) pair stored as a separate row keyed by '{market_id}_{line}'.

    For COMBINED_TOTAL markets: one row per handicap/line (handicap field = line).
    For NUMBER_OF_SETS markets: one row with synthetic line 2.5 (BO3) or 3.5 (BO5),
      over_odds = odds of more sets, under_odds = odds of fewer sets.

    Stores event_name so that link_markets_to_matches() can resolve match_ids.
    Returns number of rows written (or found, in dry-run mode).
    """
    specs = MARKET_SPECS.get(sport)
    if not specs:
        raise ValueError(f"No market spec for sport: {sport!r}")

    now    = datetime.now(timezone.utc)
    to_dt  = now + timedelta(days=days_ahead)
    from_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_s   = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = None if dry_run else get_conn()
    rows_written = 0

    # Ensure event_name and competition_name columns exist
    if conn:
        for col in ("event_name", "competition_name"):
            try:
                conn.execute(f"ALTER TABLE betfair_markets ADD COLUMN {col} TEXT")
                conn.commit()
            except Exception:
                pass

    try:
        for betfair_type, internal_type, name_guard in specs:
            log.info(f"[scraper] {sport} {betfair_type}: listing {from_s} → {to_s}")
            markets = list_markets(
                session, sport, from_s, to_s,
                market_types=[betfair_type],
                max_results=200,
            )
            markets = [m for m in markets
                       if name_guard.lower() in m.get("marketName", "").lower()]
            log.info(f"[scraper] {len(markets)} markets matched")
            if not markets:
                continue

            meta       = {m["marketId"]: m for m in markets}
            market_ids = list(meta.keys())

            # Batch size 5: Total Games markets have 110+ runners per market.
            # Betfair TOO_MUCH_DATA triggers at large runner counts; 5 markets
            # × 110 runners × 1 projection ≈ 550 is safely within limits.
            all_books = []
            for i in range(0, len(market_ids), 5):
                all_books.extend(get_market_book(session, market_ids[i:i+5]))

            for book in all_books:
                mid              = book["marketId"]
                catalogue        = meta.get(mid, {})
                event_name       = catalogue.get("event", {}).get("name", "")
                competition_name = catalogue.get("competition", {}).get("name", "")
                cat_runners      = catalogue.get("runners", [])
                book_runners     = book.get("runners", [])
                # Market-level totalMatched (EX_TRADED not requested — use as liquidity proxy)
                market_matched   = book.get("totalMatched", 0.0) or 0.0

                # Choose correct line extractor
                if betfair_type in SET_COUNT_MARKET_TYPES:
                    lines = _extract_set_count_lines(cat_runners, book_runners)
                else:
                    lines = _extract_lines(cat_runners, book_runners)

                # Without EX_TRADED, runner.totalMatched = 0.
                # Use market-level totalMatched as liquidity proxy for all lines.
                lines = [
                    (hcap, ov, un, round(market_matched, 2) if (not mkt) and market_matched > 0 else mkt)
                    for (hcap, ov, un, mkt) in lines
                ]

                if not lines:
                    log.warning(f"[scraper] {mid} {event_name!r}: no lines parsed — skipping")
                    continue

                log.info(f"  {mid}  {event_name}  ({len(lines)} lines)")
                for hcap, over_odds, under_odds, matched in lines:
                    log.info(
                        f"    line={hcap:<5}  over={over_odds}  under={under_odds}"
                        + (f"  matched=£{matched:.0f}" if matched else "")
                    )
                    rows_written += 1

                    if dry_run:
                        continue

                    row_key = f"{mid}_{hcap}"
                    conn.execute("""
                        INSERT INTO betfair_markets
                            (market_id, match_id, sport, market_type, line,
                             over_odds, under_odds, total_matched,
                             event_name, competition_name, data_source, verified)
                        VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'betfair_api_live', 0)
                        ON CONFLICT(market_id) DO UPDATE SET
                            over_odds        = excluded.over_odds,
                            under_odds       = excluded.under_odds,
                            total_matched    = excluded.total_matched,
                            event_name       = excluded.event_name,
                            competition_name = excluded.competition_name
                    """, (row_key, sport, internal_type, hcap,
                          over_odds, under_odds, matched, event_name, competition_name))

        if conn:
            conn.commit()

        # Auto-link if date provided
        if conn and link_date and not dry_run:
            n = link_markets_to_matches(conn, sport, link_date)
            log.info(f"[scraper] Linked {n} market rows to matches for {link_date}")

    finally:
        if conn:
            conn.close()

    return rows_written


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    parser = argparse.ArgumentParser(description="Betfair line logger — polls upcoming markets")
    parser.add_argument("--sport",      choices=["darts", "snooker", "tennis"], default="tennis")
    parser.add_argument("--days-ahead", type=int, default=2,
                        help="Window = days_ahead × 24h (default: 2)")
    parser.add_argument("--dry-run",    action="store_true",
                        help="Print all lines without writing to DB")
    parser.add_argument("--link-date",  default="",
                        help="After polling, link markets to matches for this date (YYYY-MM-DD)")
    args = parser.parse_args()

    session = BetfairSession()
    session.login()
    try:
        n    = poll_sport(session, args.sport, args.days_ahead, args.dry_run, args.link_date)
        mode = "dry-run" if args.dry_run else "written to DB"
        log.info(f"[scraper] Done — {n} lines {mode}")
    finally:
        session.logout()


if __name__ == "__main__":
    main()
