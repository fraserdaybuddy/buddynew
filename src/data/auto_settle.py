"""
auto_settle.py — Auto-settle PENDING paper bets from Betfair market outcomes
JOB-006 Sports Betting Model

How it works:
  1. Load all PENDING paper bets from ledger
  2. Find the corresponding Betfair market_id from betfair_markets
     (match on event_name + market_type + line)
  3. Call listMarketBook — if market status is CLOSED (settled), check which
     runner has status='WINNER' (the Over or Under side)
  4. Mark ledger bet as WON/LOST and compute P&L

Note: Betfair keeps settled market data accessible for ~24h after settlement.
      Run this within a day of matches finishing for reliable auto-settlement.

Usage:
    PYTHONUTF8=1 python -m src.data.auto_settle
"""

import logging
import sys
from pathlib import Path
from datetime import date as _date

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.database import get_conn
from src.execution.betfair import BetfairSession

log = logging.getLogger("auto_settle")


def _get_market_outcome(session: BetfairSession, market_id: str) -> str | None:
    """
    Query a single Betfair market and return 'OVER', 'UNDER', or None.

    Returns None if market is still open, or if outcome can't be determined.
    Uses SP_TRADED + RUNNER_DESCRIPTION to get runner names and statuses.
    """
    try:
        raw = session.list_market_book(
            market_ids=[market_id],
            price_data=["EX_BEST_OFFERS"],
        )
    except Exception as e:
        log.warning("[auto_settle] listMarketBook failed for %s: %s", market_id, e)
        return None

    if not raw:
        return None

    book = raw[0]
    if book.get("status") != "CLOSED":
        return None  # still open

    # Check each runner for WINNER status
    # Runner names come from the catalogue — but we stored OVER/UNDER in betfair_markets.
    # Fall back: runner with status='WINNER' and handicap matches the line.
    for runner in book.get("runners", []):
        if runner.get("status") == "WINNER":
            # Determine if this is the OVER or UNDER runner.
            # We look up the runner name from betfair_markets catalogue data.
            # betfair_markets doesn't store runner names, so we infer from handicap:
            # COMBINED_TOTAL: positive handicap = OVER, negative = UNDER.
            # But all runners share the same handicap for a given line, so we
            # use the runner's sort priority: 1 = OVER, 2 = UNDER (Betfair convention).
            sort_priority = runner.get("sortPriority")
            if sort_priority == 1:
                return "OVER"
            elif sort_priority == 2:
                return "UNDER"
            # Fallback: try metadata if available
            log.debug("[auto_settle] market %s winner runner sort_priority=%s",
                      market_id, sort_priority)
            return None  # can't determine

    return None  # no WINNER found yet


def settle_pending_paper_bets(dry_run: bool = False) -> dict:
    """
    Check all PENDING paper bets against Betfair market outcomes and settle them.

    Returns:
        {settled: int, skipped: int, errors: int, results: list[dict]}
    """
    conn = get_conn()

    # All pending paper bets
    pending = conn.execute(
        """SELECT l.rowid, l.match_id, l.bet_direction, l.line,
                  l.odds_taken, l.stake_gbp, l.mode
           FROM ledger l
           WHERE l.status = 'PENDING' AND l.mode = 'PAPER'
           ORDER BY l.placed_at ASC"""
    ).fetchall()

    if not pending:
        conn.close()
        log.info("[auto_settle] No pending paper bets.")
        return {"settled": 0, "skipped": 0, "errors": 0, "results": []}

    log.info("[auto_settle] %d pending paper bet(s) to check", len(pending))

    # Build lookup: event_name → {market_type → market_id} from betfair_markets
    # event_name is stored without 'BF:' prefix
    mkt_lookup = {}
    cols = [r[1] for r in conn.execute("PRAGMA table_info(betfair_markets)").fetchall()]
    if "event_name" in cols:
        rows = conn.execute(
            """SELECT DISTINCT event_name, market_type, market_id, line
               FROM betfair_markets
               WHERE event_name IS NOT NULL"""
        ).fetchall()
        for r in rows:
            ev = (r["event_name"] or "").lower().strip()
            mt = (r["market_type"] or "").lower()
            key = (ev, mt, float(r["line"] or 0))
            mkt_lookup[key] = r["market_id"]

    session = BetfairSession()
    try:
        session.login()
    except Exception as e:
        conn.close()
        log.error("[auto_settle] Betfair login failed: %s", e)
        return {"settled": 0, "skipped": 0, "errors": 1, "results": []}

    settled_count = 0
    skipped_count = 0
    error_count   = 0
    results       = []

    try:
        for bet in pending:
            rowid     = bet["rowid"]
            match_id  = bet["match_id"] or ""
            bet_dir   = bet["bet_direction"] or ""
            line      = float(bet["line"] or 0)
            odds      = float(bet["odds_taken"] or 0)
            stake     = float(bet["stake_gbp"] or 0)

            # Parse market_type and direction from bet_direction string
            # e.g. "total_games_UNDER" → market_type="total_games", direction="UNDER"
            # e.g. "total_180s total_180s OVER 8.5" → handle both formats
            parts = bet_dir.upper().rsplit("_", 1)
            if len(parts) == 2 and parts[1] in ("OVER", "UNDER"):
                market_type = parts[0].lower()
                bet_side    = parts[1]
            else:
                # Manual bet format: "total_games OVER 18.5" or similar
                words = bet_dir.upper().split()
                if "OVER" in words:
                    bet_side    = "OVER"
                    market_type = " ".join(w for w in words if w != "OVER" and not _is_float(w)).lower()
                elif "UNDER" in words:
                    bet_side    = "UNDER"
                    market_type = " ".join(w for w in words if w != "UNDER" and not _is_float(w)).lower()
                else:
                    log.debug("[auto_settle] can't parse bet_direction %r — skipping", bet_dir)
                    skipped_count += 1
                    continue

            # Extract event name (strip "BF:" prefix and un-slug)
            if match_id.startswith("BF:"):
                event_name = match_id[3:].replace("_", " ").lower().strip()
            else:
                event_name = match_id.lower().strip()

            # Map internal market_type names to Betfair market_type in DB
            mt_aliases = {
                "total_games":    "total_games",
                "total_180s":     "total_180s",
                "total_centuries":"total_centuries",
                "total_sets":     "total_sets",
            }
            db_mt = mt_aliases.get(market_type, market_type)

            # Find matching market_id
            lookup_key = (event_name, db_mt, line)
            market_id  = mkt_lookup.get(lookup_key)

            if not market_id:
                # Try without exact line (pick closest)
                candidates = [
                    (k, v) for k, v in mkt_lookup.items()
                    if k[0] == event_name and k[1] == db_mt
                ]
                if candidates:
                    market_id = min(candidates, key=lambda x: abs(x[0][2] - line))[1]

            if not market_id:
                log.debug("[auto_settle] no market_id found for rowid=%d %s %s %.1f",
                          rowid, event_name, db_mt, line)
                skipped_count += 1
                continue

            # Query Betfair for settlement outcome
            outcome = _get_market_outcome(session, market_id)

            if outcome is None:
                log.debug("[auto_settle] market %s not yet settled", market_id)
                skipped_count += 1
                continue

            # Determine WON/LOST
            result = "WON" if outcome == bet_side else "LOST"
            pnl    = round((odds - 1.0) * stake, 2) if result == "WON" else round(-stake, 2)

            log.info("[auto_settle] rowid=%d  %s %s %.1f  market=%s  outcome=%s  → %s  pnl=£%+.2f",
                     rowid, event_name, bet_side, line, market_id, outcome, result, pnl)

            if not dry_run:
                conn.execute(
                    """UPDATE ledger
                       SET status=?, profit_loss_gbp=?, settled_at=datetime('now')
                       WHERE rowid=?""",
                    (result, pnl, rowid)
                )

            results.append({
                "rowid":   rowid,
                "match":   event_name,
                "bet":     f"{bet_side} {line}",
                "outcome": outcome,
                "result":  result,
                "pnl":     pnl,
            })
            settled_count += 1

    except Exception as e:
        log.error("[auto_settle] Unexpected error: %s", e)
        error_count += 1
    finally:
        if not dry_run:
            conn.commit()
        conn.close()
        try:
            session.logout()
        except Exception:
            pass

    log.info("[auto_settle] Done: %d settled, %d skipped, %d errors",
             settled_count, skipped_count, error_count)
    return {
        "settled": settled_count,
        "skipped": skipped_count,
        "errors":  error_count,
        "results": results,
    }


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    r = settle_pending_paper_bets(dry_run=args.dry_run)
    print(f"\nSettled: {r['settled']}  Skipped: {r['skipped']}  Errors: {r['errors']}")
    for b in r["results"]:
        print(f"  {b['match']}  {b['bet']}  → {b['result']}  £{b['pnl']:+.2f}")
