"""
results_puller.py — Pull yesterday's settled bets from Betfair
JOB-006 Sports Betting Model

Matches Betfair betId → ledger.sportmarket_order_id.
Updates: settled=1, won, actual_profit, settled_at.

Run:  PYTHONUTF8=1 python -m src.data.results_puller
Cron: 02:00 UTC daily
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.execution.betfair import BetfairClient, list_cleared_orders
from src.database import get_conn, DB_PATH

log = logging.getLogger("results_puller")


def pull_settled(db_path=DB_PATH, days_back: int = 1) -> int:
    """
    Pull Betfair settled bets from N days ago and write to ledger.

    Matches on sportmarket_order_id = Betfair betId.
    Returns count of ledger rows updated.
    """
    session = BetfairClient()
    session.login()

    window_start = (datetime.now(timezone.utc) - timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    window_end = window_start + timedelta(days=1)

    settled_from = window_start.isoformat().replace("+00:00", "Z")
    settled_to   = window_end.isoformat().replace("+00:00", "Z")

    log.info(f"[results_puller] Pulling settled bets {window_start.date()}")
    orders = list_cleared_orders(
        session,
        bet_status="SETTLED",
        settled_from=settled_from,
        settled_to=settled_to,
    )
    session.logout()

    if not orders:
        print(f"[results_puller] No settled bets found for {window_start.date()}")
        return 0

    conn = get_conn(db_path)
    updated = 0

    for order in orders:
        betfair_bet_id = order.get("betId")
        profit         = order.get("profit")
        settled_date   = order.get("settledDate")

        if profit is None:
            continue

        won = 1 if float(profit) > 0 else 0

        rows = conn.execute(
            """UPDATE ledger
               SET settled=1, won=?, actual_profit=?, settled_at=?,
                   status = CASE WHEN ? = 1 THEN 'WON' ELSE 'LOST' END
               WHERE sportmarket_order_id = ? AND settled = 0""",
            (won, float(profit), settled_date, won, betfair_bet_id)
        ).rowcount
        updated += rows

    conn.commit()
    conn.close()

    print(f"[results_puller] Settled {updated} bets from {window_start.date()} "
          f"({len(orders)} orders from Betfair)")
    return updated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    pull_settled()
