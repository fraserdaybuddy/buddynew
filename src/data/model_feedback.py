"""
model_feedback.py — Rolling bias detection on model_errors
JOB-006 Sports Betting Model

Analyses the last 30 days of model_errors for systematic over/under prediction.
Produces a daily summary. Alerts if mean error > threshold on enough samples.

Telegram: if src/bot.py exists with send_telegram(), it will be called.
          Otherwise summary is printed only.

Run:  PYTHONUTF8=1 python -m src.data.model_feedback
Cron: 06:00 UTC daily
"""

import sys
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.database import get_conn, DB_PATH

log = logging.getLogger("model_feedback")

BIAS_THRESH = 1.5   # mean error in games/legs/frames to trigger alert
MIN_SAMPLES = 10    # minimum settled bets before flagging


# ── Bias detection ────────────────────────────────────────────────────────────

def check_bias(db_path=DB_PATH) -> list[dict]:
    """
    Return list of {sport, market, surface, mean_error, n, win_rate, direction}
    for any segment where |mean_error| > BIAS_THRESH and n >= MIN_SAMPLES.
    """
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT sport, market_type, surface,
               AVG(error)        AS mean_err,
               COUNT(*)          AS n,
               SUM(COALESCE(won, 0)) AS wins
        FROM model_errors
        WHERE logged_at >= date('now', '-30 days')
        GROUP BY sport, market_type, surface
        HAVING n >= ?
        ORDER BY ABS(AVG(error)) DESC
    """, (MIN_SAMPLES,)).fetchall()
    conn.close()

    flags = []
    for row in rows:
        mean_err = row["mean_err"] or 0.0
        n        = row["n"]
        wins     = row["wins"] or 0
        if abs(mean_err) > BIAS_THRESH:
            flags.append({
                "sport":      row["sport"],
                "market":     row["market_type"],
                "surface":    row["surface"] or "all",
                "mean_error": round(mean_err, 2),
                "n":          n,
                "win_rate":   round(wins / n, 2) if n else 0.0,
                "direction":  "over-predicting" if mean_err < 0 else "under-predicting",
            })
    return flags


# ── Daily summary ─────────────────────────────────────────────────────────────

def format_daily_summary(db_path=DB_PATH) -> str:
    conn = get_conn(db_path)

    today = conn.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END),
               COALESCE(SUM(actual_profit), 0.0)
        FROM ledger
        WHERE date(settled_at) = date('now')
    """).fetchone()

    open_bets = conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE settled = 0 AND mode = 'LIVE'"
    ).fetchone()[0]

    snap = conn.execute("""
        SELECT balance, cumulative_pl FROM account_snapshots
        ORDER BY snapshot_date DESC LIMIT 1
    """).fetchone()

    mae_rows = conn.execute("""
        SELECT sport, market_type,
               ROUND(AVG(ABS(error)), 1) AS mae,
               COUNT(*) AS n
        FROM model_errors
        WHERE logged_at >= date('now', '-30 days')
        GROUP BY sport, market_type
        ORDER BY sport, market_type
    """).fetchall()

    conn.close()

    n_settled = today[0] or 0
    wins      = today[1] or 0
    pl_today  = today[2] or 0.0
    balance   = snap["balance"]  if snap else 0.0
    cum_pl    = snap["cumulative_pl"] if snap else 0.0

    mae_lines = "\n".join(
        f"  {r['sport']:<8} {r['market_type'] or '?':<18}  MAE {r['mae']}  (n={r['n']})"
        for r in mae_rows
    ) or "  No error data yet."

    flags      = check_bias(db_path)
    bias_block = "No bias flags." if not flags else "\nBIAS FLAGS:\n" + "\n".join(
        f"  [{f['sport']} {f['market']} {f['surface']}]  "
        f"mean_err={f['mean_error']:+.1f}  {f['direction']}  "
        f"n={f['n']}  win_rate={f['win_rate']:.0%}"
        for f in flags
    )

    summary = (
        f"DAILY SUMMARY — {datetime.utcnow().strftime('%d %b %Y')}\n"
        f"{'─'*45}\n"
        f"Bets settled today:   {n_settled}\n"
        f"Won / Lost:           {wins} / {n_settled - wins}\n"
        f"P&L today:            £{pl_today:+.2f}\n"
        f"Cumulative P&L:       £{cum_pl:+.2f}\n"
        f"Balance:              £{balance:.2f}\n"
        f"Open live bets:       {open_bets}\n"
        f"\nModel MAE (last 30 days):\n{mae_lines}\n"
        f"\n{bias_block}\n"
    )
    return summary


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    summary = format_daily_summary()
    print(summary)

    flags = check_bias()
    if flags:
        print(f"WARNING: {len(flags)} bias flag(s) — review signal weights")

    # Telegram hook — uncomment when bot.py is available
    # try:
    #     from src.bot import send_telegram
    #     send_telegram(summary)
    # except ImportError:
    #     pass
