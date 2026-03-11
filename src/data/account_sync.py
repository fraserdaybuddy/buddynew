"""
account_sync.py — Daily Betfair account balance snapshot
JOB-006 Sports Betting Model

Pulls balance + exposure from Betfair Accounts API.
Writes one row per day to account_snapshots table.

Run:  PYTHONUTF8=1 python -m src.data.account_sync
Cron: 02:30 UTC daily
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, date

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.execution.betfair import BetfairClient, get_account_funds
from src.database import get_conn, DB_PATH

log = logging.getLogger("account_sync")


def sync_account(db_path=DB_PATH) -> dict:
    """
    Pull account funds from Betfair and write daily snapshot.
    Returns dict with balance and cumulative_pl.
    """
    session = BetfairClient()
    session.login()

    try:
        funds = get_account_funds(session)
    except Exception as e:
        log.error(f"[account_sync] get_account_funds failed: {e}")
        session.logout()
        raise
    finally:
        session.logout()

    balance  = funds.get("availableToBetBalance", 0.0)
    exposure = funds.get("exposure", 0.0)
    retained = funds.get("retainedCommission", 0.0)

    conn = get_conn(db_path)

    # Cumulative P&L from settled ledger rows
    row    = conn.execute(
        "SELECT COALESCE(SUM(actual_profit), 0.0) FROM ledger WHERE settled = 1"
    ).fetchone()
    cum_pl = float(row[0]) if row else 0.0

    today = date.today().isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO account_snapshots
          (snapshot_date, exchange, balance, exposure, retained_comm, cumulative_pl, snapped_at)
        VALUES (?,?,?,?,?,?,?)
    """, (today, "betfair", balance, exposure, retained, cum_pl,
          datetime.utcnow().isoformat()))

    conn.commit()
    conn.close()

    print(f"[account_sync] {today}  Balance: £{balance:.2f}  "
          f"Exposure: £{exposure:.2f}  Cum P&L: £{cum_pl:+.2f}")
    return {"balance": balance, "exposure": exposure, "cumulative_pl": cum_pl}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    sync_account()
