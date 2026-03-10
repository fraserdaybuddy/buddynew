"""
ledger_writer.py — Writes to the ledger table before and after settlement
JOB-006 Sports Betting Model

Rule: ledger entry is written BEFORE polling for settlement.
A bet that has no ledger row must never be polled or settled.

Ledger lifecycle:
  write_pre_placement()   → INSERT row, status=PENDING, order_id=NULL
  write_order_placed()    → UPDATE order_id
  write_settlement()      → UPDATE status + profit_loss_gbp + settled_at
"""

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from database import get_conn, DB_PATH

log = logging.getLogger("ledger_writer")


def _bet_id(match_id: str, direction: str, run_id: str) -> str:
    """Deterministic bet_id — same inputs always produce same ID."""
    raw = f"{match_id}|{direction}|{run_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def write_pre_placement(
    match_id: str,
    direction: str,
    run_id: str,
    sport: str,
    line: float,
    odds_taken: float,
    stake_gbp: float,
    mode: str,
    db_path: Path = DB_PATH,
) -> str:
    """
    Insert a new ledger row BEFORE the order is placed.

    Returns bet_id.
    Raises if a row with the same bet_id already exists (duplicate prevention).
    """
    bet_id = _bet_id(match_id, direction, run_id)

    with get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT bet_id FROM ledger WHERE bet_id = ?", (bet_id,)
        ).fetchone()

        if existing:
            log.warning(f"[ledger] duplicate write_pre_placement ignored: {bet_id}")
            return bet_id

        conn.execute(
            """INSERT INTO ledger
               (bet_id, run_id, match_id, sport, bet_direction,
                line, odds_taken, stake_gbp, status, mode)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
            (bet_id, run_id, match_id, sport, direction,
             line, odds_taken, stake_gbp, mode),
        )

    log.info(
        f"[ledger] pre_placement written | bet={bet_id} | "
        f"{direction} {sport} line={line} @ {odds_taken} | "
        f"stake=£{stake_gbp:.2f} | mode={mode}"
    )
    return bet_id


def write_order_placed(
    bet_id: str,
    order_id: Optional[str],
    db_path: Path = DB_PATH,
) -> None:
    """
    Update ledger with the Sportmarket order_id after successful placement.

    order_id is None in paper mode — that's valid; row still gets updated.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE ledger SET sportmarket_order_id = ? WHERE bet_id = ?",
            (order_id, bet_id),
        )

    log.info(f"[ledger] order_placed | bet={bet_id} | order={order_id}")


def write_settlement(
    bet_id: str,
    status: str,
    profit_loss_gbp: Optional[float],
    db_path: Path = DB_PATH,
) -> None:
    """
    Update ledger at settlement.

    Args:
        bet_id:          the ledger row to update
        status:          WON | LOST | VOID | CANCELLED
        profit_loss_gbp: final P&L in GBP (positive = profit, negative = loss)
                         NULL for VOID / CANCELLED
    """
    if status not in ("WON", "LOST", "VOID", "CANCELLED"):
        raise ValueError(f"Invalid settlement status: {status!r}")

    settled_at = datetime.now(timezone.utc).isoformat()

    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE ledger
               SET status = ?, profit_loss_gbp = ?, settled_at = ?
               WHERE bet_id = ?""",
            (status, profit_loss_gbp, settled_at, bet_id),
        )

    log.info(
        f"[ledger] settlement | bet={bet_id} | "
        f"status={status} | P&L=£{profit_loss_gbp}"
    )


def get_pending_bets(db_path: Path = DB_PATH) -> list[dict]:
    """Return all PENDING ledger rows (placed but not yet settled)."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE status = 'PENDING' ORDER BY placed_at"
        ).fetchall()
    return [dict(r) for r in rows]


def ledger_summary(db_path: Path = DB_PATH) -> dict:
    """Return counts and total P&L grouped by status."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT status,
                      COUNT(*) AS n,
                      SUM(stake_gbp) AS total_staked,
                      SUM(profit_loss_gbp) AS total_pnl
               FROM ledger
               GROUP BY status"""
        ).fetchall()
    return {r["status"]: dict(r) for r in rows}
