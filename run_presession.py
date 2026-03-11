"""
run_presession.py — Pre-session pipeline runner
JOB-006 Sports Betting Model

Run this ~30 minutes before the first match of the day.
Steps:
  1. Poll Betfair for COMBINED_TOTAL (total games) + NUMBER_OF_SETS markets
  2. Link market rows to match_ids via surname matching
  3. Run edge.py screener for today's matches
  4. Print a formatted signal table

Usage:
    PYTHONUTF8=1 python run_presession.py
    PYTHONUTF8=1 python run_presession.py --date 2026-03-12
    PYTHONUTF8=1 python run_presession.py --date 2026-03-11 --bankroll 500 --dry-run
    PYTHONUTF8=1 python run_presession.py --skip-scrape   # use existing betfair_markets rows
"""

import argparse
import logging
import sys
from datetime import date as date_type
from pathlib import Path

# Make sure src/ is importable from project root
sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger("presession")


def _fmt_signal(s) -> str:
    """One-line summary of a BetSignal."""
    if s.reject_reason:
        return f"  SKIP  [{s.market_type:<20}]  {s.reject_reason}"
    if s.stake_gbp > 0:
        return (
            f"  BET   [{s.market_type:<12} {s.direction:<6}]  "
            f"line={s.line:<6}  edge={s.edge:+.1%}  "
            f"odds={s.odds}  stake=£{s.stake_gbp:.2f}  T{s.tier}"
        )
    # synthetic preview
    return (
        f"  WATCH [{s.market_type:<12} {s.direction:<6}]  "
        f"line={s.line:<6}  edge={s.edge:+.1%}  "
        f"(no real line yet)"
    )


def run(
    match_date:  str,
    bankroll:    float,
    mode:        str,
    skip_scrape: bool,
    dry_run:     bool,
):
    # ── Step 1: Scrape Betfair ────────────────────────────────────────────────
    if not skip_scrape:
        log.info("=" * 60)
        log.info(f"STEP 1: Polling Betfair tennis markets (date={match_date})")
        log.info("=" * 60)
        from src.execution.betfair import BetfairSession
        from src.execution.scraper import poll_sport

        session = BetfairSession()
        session.login()
        try:
            n = poll_sport(
                session,
                sport="tennis",
                days_ahead=2,
                dry_run=dry_run,
                link_date=match_date,
            )
            log.info(f"Scrape complete — {n} line rows {'(dry-run)' if dry_run else 'written'}")
        finally:
            session.logout()
    else:
        log.info("STEP 1: Skipping scrape (--skip-scrape)")
        # Still attempt linking in case rows are unlinked
        if not dry_run:
            from src.database import get_conn
            from src.execution.scraper import link_markets_to_matches
            conn = get_conn()
            n = link_markets_to_matches(conn, "tennis", match_date)
            conn.close()
            log.info(f"Linker: {n} rows linked")

    # ── Step 2: Market summary ────────────────────────────────────────────────
    log.info("")
    log.info("=" * 60)
    log.info("STEP 2: betfair_markets summary")
    log.info("=" * 60)

    from src.database import get_conn
    conn = get_conn()
    rows = conn.execute(
        """SELECT market_type, line, over_odds, under_odds, total_matched,
                  event_name, match_id, verified
           FROM betfair_markets
           WHERE sport = 'tennis'
             AND market_id LIKE '%'
           ORDER BY event_name, market_type, line"""
    ).fetchall()
    conn.close()

    if not rows:
        log.warning("No rows in betfair_markets for tennis — scrape may have found nothing.")
    else:
        log.info(f"{'Event':<30} {'Type':<15} {'Line':<6} {'Over':<7} {'Under':<7} {'£Match':<8} {'Linked'}")
        log.info("-" * 90)
        for r in rows:
            linked = "YES" if r["match_id"] else "NO "
            log.info(
                f"{(r['event_name'] or ''):<30} "
                f"{r['market_type']:<15} "
                f"{r['line']:<6} "
                f"{str(r['over_odds'] or ''):<7} "
                f"{str(r['under_odds'] or ''):<7} "
                f"{str(int(r['total_matched'] or 0)):<8} "
                f"{linked}"
            )

    # ── Step 3: Edge screening ────────────────────────────────────────────────
    log.info("")
    log.info("=" * 60)
    log.info(f"STEP 3: Edge screening — {match_date}  bankroll=£{bankroll:.0f}  mode={mode}")
    log.info("=" * 60)

    from src.model.edge import screen_from_db
    signals = screen_from_db(match_date, bankroll, mode, sport="tennis")

    bets    = [s for s in signals if s.stake_gbp > 0]
    watches = [s for s in signals if s.stake_gbp == 0 and not s.reject_reason]
    skips   = [s for s in signals if s.reject_reason]

    log.info(f"{len(signals)} signals total: {len(bets)} BET, {len(watches)} WATCH, {len(skips)} SKIP")
    log.info("")

    if bets:
        log.info("── BETS ──────────────────────────────────────────")
        for s in bets:
            log.info(_fmt_signal(s))
        log.info("")

    if watches:
        log.info("── WATCH (no real line) ──────────────────────────")
        for s in watches:
            log.info(_fmt_signal(s))
        log.info("")

    if skips:
        log.info("── SKIPPED ───────────────────────────────────────")
        for s in skips:
            log.info(_fmt_signal(s))

    # ── Step 4: Write to ledger ───────────────────────────────────────────────
    if bets and not dry_run:
        log.info("")
        log.info("=" * 60)
        log.info("STEP 4: Writing bets to ledger")
        log.info("=" * 60)

        from src.model.edge import write_to_ledger
        import hashlib, time
        run_id = hashlib.sha256(f"presession_{match_date}_{time.time()}".encode()).hexdigest()[:12]

        conn = get_conn()
        n_written = write_to_ledger(bets, run_id, conn)
        conn.close()
        log.info(f"Ledger: {n_written} bets written  run_id={run_id}")
    elif dry_run:
        log.info("")
        log.info("DRY-RUN: ledger write skipped")

    return bets


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Pre-session pipeline runner")
    parser.add_argument(
        "--date",
        default=str(date_type.today()),
        help="Match date to screen YYYY-MM-DD (default: today)",
    )
    parser.add_argument("--bankroll",    type=float, default=1000.0)
    parser.add_argument("--mode",        choices=["PAPER", "LIVE"], default="PAPER")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Scrape and screen without writing anything to DB")
    parser.add_argument("--skip-scrape", action="store_true",
                        help="Skip Betfair polling (use existing betfair_markets rows)")
    args = parser.parse_args()

    bets = run(
        match_date=args.date,
        bankroll=args.bankroll,
        mode=args.mode,
        skip_scrape=args.skip_scrape,
        dry_run=args.dry_run,
    )

    if not bets:
        print("\nNo qualifying bets found for today.")
    else:
        print(f"\n{len(bets)} bet(s) found. Check log for details.")


if __name__ == "__main__":
    main()
