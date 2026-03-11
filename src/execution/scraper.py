"""
scraper.py — Betfair line logger
JOB-006 Sports Betting Model

Polls upcoming Betfair markets and stores all available O/U lines
in betfair_markets table. Designed to run nightly once server arrives.

Betfair market structure (COMBINED_TOTAL / TOTAL_ACES):
  - One market per match event
  - All lines (e.g. 0.5 to 19.5) are runners in the same market
  - Line value is in runner['handicap'], not in the market name
  - Row key: "{betfair_market_id}_{handicap}" to store one row per line

Usage:
    PYTHONUTF8=1 python -m src.execution.scraper --sport darts
    PYTHONUTF8=1 python -m src.execution.scraper --sport darts --dry-run
    PYTHONUTF8=1 python -m src.execution.scraper --sport tennis --days-ahead 3
"""

import logging
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.database import get_conn
from src.execution.betfair import BetfairSession, list_markets, get_market_book

log = logging.getLogger("scraper")

# Betfair market type code → (internal stat name, required substring in marketName)
# Name guard prevents pulling wrong COMBINED_TOTAL markets (e.g. "Total Games")
MARKET_SPECS = {
    "darts":   [("COMBINED_TOTAL", "total_180s",      "180")],
    "snooker": [("COMBINED_TOTAL", "total_centuries", "centur")],
    "tennis":  [("TOTAL_ACES",     "total_aces",      "ace")],
}


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


# ── Core poll ─────────────────────────────────────────────────────────────────

def poll_sport(
    session:    BetfairSession,
    sport:      str,
    days_ahead: int  = 2,
    dry_run:    bool = False,
) -> int:
    """
    Poll upcoming markets for a sport and upsert all lines into betfair_markets.
    Each (market, line) pair stored as a separate row keyed by '{market_id}_{line}'.
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

            # Fetch live prices in batches of 40 (API limit)
            all_books = []
            for i in range(0, len(market_ids), 40):
                all_books.extend(get_market_book(session, market_ids[i:i+40]))

            for book in all_books:
                mid          = book["marketId"]
                catalogue    = meta.get(mid, {})
                event_name   = catalogue.get("event", {}).get("name", "")
                cat_runners  = catalogue.get("runners", [])
                book_runners = book.get("runners", [])

                lines = _extract_lines(cat_runners, book_runners)
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
                             over_odds, under_odds, total_matched, data_source, verified)
                        VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 'betfair_api_live', 0)
                        ON CONFLICT(market_id) DO UPDATE SET
                            over_odds     = excluded.over_odds,
                            under_odds    = excluded.under_odds,
                            total_matched = excluded.total_matched
                    """, (row_key, sport, internal_type, hcap,
                          over_odds, under_odds, matched))

        if conn:
            conn.commit()

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
    parser.add_argument("--sport",      choices=["darts", "snooker", "tennis"], default="darts")
    parser.add_argument("--days-ahead", type=int, default=2,
                        help="Hours window = days_ahead × 24 (default: 2 days)")
    parser.add_argument("--dry-run",    action="store_true",
                        help="Print all lines without writing to DB")
    args = parser.parse_args()

    session = BetfairSession()
    session.login()
    try:
        n    = poll_sport(session, args.sport, args.days_ahead, args.dry_run)
        mode = "dry-run" if args.dry_run else "written to DB"
        log.info(f"[scraper] Done — {n} lines {mode}")
    finally:
        session.logout()


if __name__ == "__main__":
    main()
