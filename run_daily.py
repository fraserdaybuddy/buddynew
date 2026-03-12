"""
run_daily.py — JOB-006 daily refresh pipeline
==============================================
Run once each morning before the first match.

Steps:
  1. Backup database
  2. Scrape fresh Betfair markets (or --no-scrape to skip)
  3. Run edge screener → write PENDING bets to ledger
  4. Show yesterday's PENDING bets that need manual settlement
  5. Print daily summary

Usage:
    PYTHONUTF8=1 python run_daily.py
    PYTHONUTF8=1 python run_daily.py --no-scrape        # use existing betfair_markets rows
    PYTHONUTF8=1 python run_daily.py --bankroll 500
    PYTHONUTF8=1 python run_daily.py --dry-run          # preview only, nothing written
"""

import argparse
import logging
import sys
from datetime import date as date_type, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger("daily")

BANK_START = 1000.0  # initial paper bankroll — used for display only


def _separator(title=""):
    bar = "=" * 60
    if title:
        log.info(bar)
        log.info(f"  {title}")
        log.info(bar)
    else:
        log.info(bar)


def run(
    match_date: str,
    bankroll:   float,
    mode:       str,
    no_scrape:  bool,
    dry_run:    bool,
):
    from src.database import get_conn, backup

    # ── Step 1: Backup ────────────────────────────────────────────────────────
    _separator("STEP 1: Database backup")
    if not dry_run:
        dest = backup(label="daily")
        log.info(f"Backup written: {dest.name}")
    else:
        log.info("DRY-RUN: backup skipped")

    # ── Step 2: Scrape Betfair ────────────────────────────────────────────────
    _separator(f"STEP 2: Betfair market scrape  (date={match_date})")
    if no_scrape:
        log.info("--no-scrape: using existing betfair_markets rows")
    else:
        try:
            from src.execution.betfair import BetfairSession
            from src.execution.scraper import poll_sport

            session = BetfairSession()
            session.login()
            try:
                total_rows = 0
                for sport in ("tennis", "darts", "snooker"):
                    try:
                        n = poll_sport(
                            session,
                            sport=sport,
                            days_ahead=2,
                            dry_run=dry_run,
                            link_date=match_date,
                        )
                        log.info(f"  {sport}: {n} line rows {'(dry-run)' if dry_run else 'written'}")
                        total_rows += n
                    except Exception as sport_err:
                        log.warning(f"  {sport}: scrape failed — {sport_err}")
                log.info(f"Scrape complete — {total_rows} total rows")
            finally:
                session.logout()
        except Exception as e:
            log.error(f"Scrape FAILED: {e}")
            log.warning("Continuing with existing betfair_markets data")

    # ── Step 3: Edge screening → write bets ───────────────────────────────────
    _separator(f"STEP 3: Edge screening  bankroll=£{bankroll:.0f}  mode={mode}")

    from src.model.edge import screen_from_db, screen_from_betfair_markets, write_to_ledger

    signals = []
    for sport in ("tennis", "darts", "snooker"):
        db_signals = screen_from_db(match_date, bankroll, mode, sport=sport)
        if db_signals:
            log.info(f"  [{sport}] {len(db_signals)} DB signal(s)")
            signals.extend(db_signals)
        else:
            live_signals = screen_from_betfair_markets(
                sport=sport, bankroll=bankroll, mode=mode, min_liquidity=50.0,
            )
            if live_signals:
                log.info(f"  [{sport}] {len(live_signals)} live signal(s) from Betfair screener")
            signals.extend(live_signals)

    bets    = [s for s in signals if s.stake_gbp > 0]
    watches = [s for s in signals if s.stake_gbp == 0 and not s.reject_reason]
    skips   = [s for s in signals if s.reject_reason]

    log.info(f"{len(signals)} signals — {len(bets)} BET  {len(watches)} WATCH  {len(skips)} SKIP")

    if bets:
        log.info("")
        log.info("── TODAY'S BETS ──────────────────────────────────────────────")
        for s in bets:
            match = getattr(s, "event_name", "") or s.match_id
            log.info(
                f"  [{s.market_type:<15} {s.direction:<6}]  "
                f"{match:<32}  line={s.line:<6}  "
                f"edge={s.edge:+.1%}  odds={s.odds}  stake=£{s.stake_gbp:.2f}"
            )

    if watches:
        log.info("")
        log.info("── WATCH (line missing) ──────────────────────────────────────")
        for s in watches:
            match = getattr(s, "event_name", "") or s.match_id
            log.info(f"  [{s.market_type:<15} {s.direction:<6}]  {match}  edge={s.edge:+.1%}")

    # Bets are NOT auto-written to ledger — place them manually via the dashboard
    if dry_run:
        log.info("\nDRY-RUN: no ledger writes")
    else:
        log.info(f"\n{len(bets)} qualifying bet(s) — open dashboard and click PAPER to log each one")

    # ── Step 4: Pending settlement check ──────────────────────────────────────
    _separator("STEP 4: Pending bets — need manual settlement")

    conn = get_conn()
    pending = conn.execute(
        """SELECT rowid, match_id, sport, bet_direction, line, odds_taken,
                  stake_gbp, placed_at, mode
           FROM ledger
           WHERE status = 'PENDING'
             AND placed_at < datetime('now', '-3 hours')
           ORDER BY placed_at ASC"""
    ).fetchall()
    conn.close()

    if not pending:
        log.info("No pending bets awaiting settlement.")
    else:
        log.info(f"{len(pending)} bet(s) need settlement — mark WIN/LOSS in dashboard Bet Log tab:")
        log.info(f"  {'rowid':<6}  {'Date':<12}  {'Sport':<8}  {'Bet':<30}  {'Line':<6}  {'Odds':<6}  {'Stake'}")
        log.info(f"  {'-'*6}  {'-'*12}  {'-'*8}  {'-'*30}  {'-'*6}  {'-'*6}  {'-'*6}")
        for row in pending:
            log.info(
                f"  {row['rowid']:<6}  {row['placed_at'][:10]:<12}  "
                f"{row['sport']:<8}  {row['bet_direction']:<30}  "
                f"{row['line']:<6}  {row['odds_taken']:<6.2f}  £{row['stake_gbp']:.0f}"
            )

    # ── Step 5: Summary ───────────────────────────────────────────────────────
    _separator("STEP 5: P&L summary (all-time)")

    conn = get_conn()
    summary = conn.execute(
        """SELECT
             COUNT(*)                                              AS total_bets,
             SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END)   AS pending,
             SUM(CASE WHEN status='WON'     THEN 1 ELSE 0 END)   AS wins,
             SUM(CASE WHEN status='LOST'    THEN 1 ELSE 0 END)   AS losses,
             SUM(COALESCE(profit_loss_gbp, 0))                   AS total_pnl,
             SUM(stake_gbp)                                       AS total_staked
           FROM ledger
           WHERE mode = ?""",
        (mode,)
    ).fetchone()
    conn.close()

    if summary and summary["total_bets"] > 0:
        pnl     = summary["total_pnl"] or 0
        staked  = summary["total_staked"] or 1
        roi     = (pnl / staked * 100) if staked > 0 else 0
        bank    = BANK_START + pnl
        settled = (summary["wins"] or 0) + (summary["losses"] or 0)
        wr      = (summary["wins"] / settled * 100) if settled > 0 else 0

        log.info(f"  Mode:      {mode}")
        log.info(f"  Total:     {summary['total_bets']} bets  ({summary['pending']} pending)")
        log.info(f"  Settled:   {settled}  ({summary['wins']} W / {summary['losses']} L)  WR={wr:.0f}%")
        log.info(f"  P&L:       £{pnl:+.2f}  ROI={roi:+.1f}%")
        log.info(f"  Bank:      £{bank:.2f}  (started £{BANK_START:.0f})")
    else:
        log.info(f"  No {mode} bets settled yet.")

    log.info("")
    log.info("Done. Open http://127.0.0.1:5000 and settle pending bets in the Bet Log tab.")
    log.info("To start the dashboard server: PYTHONUTF8=1 python run_server.py")

    return bets


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="JOB-006 daily refresh pipeline")
    parser.add_argument("--date",       default=str(date_type.today()),
                        help="Match date YYYY-MM-DD (default: today)")
    parser.add_argument("--bankroll",   type=float, default=BANK_START)
    parser.add_argument("--mode",       choices=["PAPER", "LIVE"], default="PAPER")
    parser.add_argument("--no-scrape",  action="store_true",
                        help="Skip Betfair poll — use existing betfair_markets rows")
    parser.add_argument("--dry-run",    action="store_true",
                        help="Preview only — no writes to DB")
    args = parser.parse_args()

    run(
        match_date=args.date,
        bankroll=args.bankroll,
        mode=args.mode,
        no_scrape=args.no_scrape,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
