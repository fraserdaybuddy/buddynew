"""
server.py — JOB-006 Dashboard API
Flask server that bridges the SQLite DB and the betting-dashboard.html frontend.

Endpoints:
  GET  /api/status              DB health, match counts, pipeline state
  GET  /api/signals?date=       Run edge screener for a date, return signals JSON
  GET  /api/markets             betfair_markets rows (linked + unlinked)
  GET  /api/ledger?sport=&limit= Recent ledger entries + summary
  POST /api/analyse             Analyse a match by player name (name lookup → ELO → sim)
  GET  /api/latest-date         Most recent date in matches table (fallback for UI)

CORS: all origins allowed — required for file:// dashboard.

Usage:
    PYTHONUTF8=1 python run_server.py
    Then open dashboard/betting-dashboard.html in browser.
"""

import sys
import json
import sqlite3
import logging
from pathlib import Path
from datetime import date as date_type, datetime

from flask import Flask, jsonify, request

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

log = logging.getLogger("api")
app = Flask(__name__)

DB_PATH = Path(__file__).parent.parent.parent / "data" / "universe.db"


# ── DB helper ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_list(rows) -> list:
    return [dict(r) for r in rows]


# ── CORS (needed for file:// origin) ──────────────────────────────────────────

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return "", 204


# ── /api/status ────────────────────────────────────────────────────────────────

@app.route("/api/status")
def status():
    conn = get_db()

    def count(q, *args):
        return conn.execute(q, args).fetchone()[0]

    tennis  = count("SELECT COUNT(*) FROM matches WHERE sport='tennis'")
    darts   = count("SELECT COUNT(*) FROM matches WHERE sport='darts'")
    snooker = count("SELECT COUNT(*) FROM matches WHERE sport='snooker'")

    last_tennis  = conn.execute("SELECT MAX(match_date) FROM matches WHERE sport='tennis'").fetchone()[0]
    last_darts   = conn.execute("SELECT MAX(match_date) FROM matches WHERE sport='darts'").fetchone()[0]

    bm_total    = count("SELECT COUNT(*) FROM betfair_markets")
    bm_verified = count("SELECT COUNT(*) FROM betfair_markets WHERE verified=1")
    bm_today    = count(
        "SELECT COUNT(*) FROM betfair_markets b "
        "JOIN matches m ON b.match_id=m.match_id "
        "WHERE m.match_date=?", str(date_type.today())
    )

    led_total   = count("SELECT COUNT(*) FROM ledger")
    led_paper   = count("SELECT COUNT(*) FROM ledger WHERE mode='PAPER'")
    led_live    = count("SELECT COUNT(*) FROM ledger WHERE mode='LIVE'")
    led_pending = count("SELECT COUNT(*) FROM ledger WHERE status='PENDING'")

    elo_players = count("SELECT COUNT(DISTINCT player_id) FROM elo_ratings")
    form_rows   = count("SELECT COUNT(*) FROM player_form WHERE sport='tennis'")
    last_form   = conn.execute("SELECT MAX(as_of_date) FROM player_form WHERE sport='tennis'").fetchone()[0]

    conn.close()

    # Determine blockers
    blockers = []
    if bm_total == 0:
        blockers.append("betfair_markets_empty — run run_presession.py first")
    if last_tennis and last_tennis < "2026-01-01":
        blockers.append(f"data_stale — tennis last updated {last_tennis}")
    if led_total == 0:
        blockers.append("no_bets_yet — no recommendations generated")

    return jsonify({
        "matches": {"tennis": tennis, "darts": darts, "snooker": snooker},
        "last_match_date": {"tennis": last_tennis, "darts": last_darts},
        "betfair_markets": {
            "total": bm_total,
            "verified": bm_verified,
            "today": bm_today,
        },
        "ledger": {
            "total": led_total,
            "paper": led_paper,
            "live": led_live,
            "pending": led_pending,
        },
        "model": {
            "elo_players": elo_players,
            "form_rows": form_rows,
            "last_form_date": last_form,
        },
        "blockers": blockers,
        "server_time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


# ── /api/latest-date ──────────────────────────────────────────────────────────

@app.route("/api/latest-date")
def latest_date():
    """Return the most recent date in the matches table — useful as demo fallback."""
    sport = request.args.get("sport", "tennis")
    conn  = get_db()
    row   = conn.execute(
        "SELECT MAX(match_date) as d FROM matches WHERE sport=?", (sport,)
    ).fetchone()
    conn.close()
    return jsonify({"date": row["d"]})


# ── /api/signals ──────────────────────────────────────────────────────────────

@app.route("/api/signals")
def signals():
    """
    Run edge screener for a given date.
    ?date=YYYY-MM-DD  (default: today)
    ?bankroll=1000
    ?mode=PAPER
    ?sport=tennis

    Returns both qualifying bets and synthetic previews (when no real market lines).
    """
    match_date = request.args.get("date", str(date_type.today()))
    bankroll   = float(request.args.get("bankroll", 1000.0))
    mode       = request.args.get("mode", "PAPER")
    sport      = request.args.get("sport", "tennis")

    try:
        from src.model.edge import screen_from_db
        raw = screen_from_db(match_date, bankroll, mode, sport=sport)
    except Exception as e:
        log.exception("screen_from_db failed")
        return jsonify({"error": str(e), "signals": [], "bets": 0, "total": 0})

    def sig_to_dict(s):
        return {
            "match_id":      s.match_id,
            "sport":         s.sport,
            "market_type":   s.market_type,
            "direction":     s.direction,
            "line":          s.line,
            "model_p":       s.model_p,
            "market_p":      s.market_p,
            "edge":          s.edge,
            "odds":          s.odds,
            "kelly_frac":    s.kelly_frac,
            "stake_gbp":     s.stake_gbp,
            "tier":          s.tier,
            "mode":          s.mode,
            "synthetic_line": s.synthetic_line,
            "reject_reason": s.reject_reason,
        }

    out = [sig_to_dict(s) for s in raw]
    bets = sum(1 for s in raw if s.stake_gbp > 0)

    return jsonify({
        "date":    match_date,
        "sport":   sport,
        "signals": out,
        "bets":    bets,
        "total":   len(out),
    })


# ── /api/markets ──────────────────────────────────────────────────────────────

@app.route("/api/markets")
def markets():
    """Return betfair_markets rows, optionally filtered by sport/date."""
    sport = request.args.get("sport", "tennis")
    conn  = get_db()

    # Check if event_name column exists
    cols = [r[1] for r in conn.execute("PRAGMA table_info(betfair_markets)").fetchall()]
    ev_col = ", b.event_name" if "event_name" in cols else ", NULL as event_name"

    rows = conn.execute(
        f"""SELECT b.market_id, b.match_id, b.sport, b.market_type,
                  b.line, b.over_odds, b.under_odds, b.total_matched,
                  b.verified, b.created_at {ev_col},
                  m.match_date, m.player1_id, m.player2_id
           FROM betfair_markets b
           LEFT JOIN matches m ON b.match_id = m.match_id
           WHERE b.sport = ?
           ORDER BY b.created_at DESC
           LIMIT 200""",
        (sport,)
    ).fetchall()
    conn.close()
    return jsonify({"sport": sport, "rows": rows_to_list(rows), "count": len(rows)})


# ── /api/ledger ────────────────────────────────────────────────────────────────

@app.route("/api/ledger")
def ledger():
    """Return recent ledger entries with match context."""
    sport = request.args.get("sport", "all")
    limit = int(request.args.get("limit", 50))
    mode  = request.args.get("mode", "all")

    where = []
    params = []
    if sport != "all":
        where.append("l.sport = ?")
        params.append(sport)
    if mode != "all":
        where.append("l.mode = ?")
        params.append(mode)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_db()
    rows = conn.execute(
        f"""SELECT l.bet_id, l.match_id, l.sport, l.bet_direction, l.line,
                  l.odds_taken, l.stake_gbp, l.status, l.profit_loss_gbp,
                  l.placed_at, l.settled_at, l.mode,
                  m.match_date, m.player1_id, m.player2_id
           FROM ledger l
           LEFT JOIN matches m ON l.match_id = m.match_id
           {where_sql}
           ORDER BY l.placed_at DESC
           LIMIT ?""",
        params + [limit]
    ).fetchall()

    # Summary
    summary_rows = conn.execute(
        f"""SELECT COUNT(*) as n,
                  SUM(CASE WHEN profit_loss_gbp IS NOT NULL THEN profit_loss_gbp ELSE 0 END) as total_pnl,
                  SUM(stake_gbp) as total_staked
           FROM ledger l {where_sql}""",
        params
    ).fetchone()
    conn.close()

    total_pnl    = summary_rows["total_pnl"] or 0
    total_staked = summary_rows["total_staked"] or 1
    roi = total_pnl / total_staked if total_staked > 0 else 0

    return jsonify({
        "rows":    rows_to_list(rows),
        "count":   len(rows),
        "summary": {
            "total_bets":   summary_rows["n"],
            "total_pnl":    round(total_pnl, 2),
            "total_staked": round(total_staked, 2),
            "roi":          round(roi, 4),
        }
    })


# ── /api/analyse ──────────────────────────────────────────────────────────────

@app.route("/api/analyse", methods=["POST"])
def analyse():
    """
    Analyse a tennis match by player names.

    Body JSON:
      p1 (str)         — Player 1 name (partial match ok, e.g. "Sinner")
      p2 (str)         — Player 2 name
      surface (str)    — Hard | Clay | Grass (default: Hard)
      best_of (int)    — 3 or 5 (default: 3)
      book_line (float) — book's total games line (e.g. 22.5)
      book_odds (float) — decimal odds for that side (e.g. 1.88)
      direction (str)  — OVER | UNDER (default: UNDER)

    Returns edge, stake, simulation breakdown, and matched player info.
    """
    body = request.get_json(force=True) or {}

    p1_query  = (body.get("p1") or "").strip()
    p2_query  = (body.get("p2") or "").strip()
    surface   = body.get("surface", "Hard")
    best_of   = int(body.get("best_of", 3))
    book_line = float(body.get("book_line", 22.5))
    book_odds = float(body.get("book_odds", 1.88))
    direction = body.get("direction", "UNDER").upper()
    bankroll  = float(body.get("bankroll", 1000.0))

    if not p1_query or not p2_query:
        return jsonify({"error": "p1 and p2 are required"}), 400

    conn = get_db()

    def lookup_player(query: str):
        """Find best matching player_id + ELO by name fragment."""
        surf_map = {"Hard": "Hard", "Clay": "Clay", "Grass": "Grass"}
        surf_key = surf_map.get(surface, "Hard")

        # Search player_aliases raw_name LIKE %query%
        rows = conn.execute(
            """SELECT pa.player_id, pa.raw_name, p.full_name,
                      er.elo, er.match_count, er.surface
               FROM player_aliases pa
               JOIN players p ON pa.player_id = p.player_id
               LEFT JOIN elo_ratings er ON pa.player_id = er.player_id
                         AND er.surface = ?
               WHERE pa.raw_name LIKE ? COLLATE NOCASE
                  OR p.full_name LIKE ? COLLATE NOCASE
               ORDER BY er.match_count DESC NULLS LAST
               LIMIT 5""",
            (surf_key, f"%{query}%", f"%{query}%")
        ).fetchall()

        if not rows:
            # Try surname only (last word of query)
            surname = query.split()[-1]
            rows = conn.execute(
                """SELECT pa.player_id, pa.raw_name, p.full_name,
                          er.elo, er.match_count, er.surface
                   FROM player_aliases pa
                   JOIN players p ON pa.player_id = p.player_id
                   LEFT JOIN elo_ratings er ON pa.player_id = er.player_id
                             AND er.surface = ?
                   WHERE pa.raw_name LIKE ? COLLATE NOCASE
                      OR p.full_name LIKE ? COLLATE NOCASE
                   ORDER BY er.match_count DESC NULLS LAST
                   LIMIT 5""",
                (surf_key, f"%{surname}%", f"%{surname}%")
            ).fetchall()

        if not rows:
            return None, f"Player not found: {query!r}"

        best = dict(rows[0])
        elo  = best.get("elo")

        # Fall back to Overall ELO if surface ELO missing
        if elo is None:
            overall = conn.execute(
                "SELECT elo, match_count FROM elo_ratings WHERE player_id=? AND surface='Overall'",
                (best["player_id"],)
            ).fetchone()
            if overall:
                elo = overall["elo"]
                best["match_count"] = overall["match_count"]

        best["elo"] = elo
        return best, None

    p1_data, p1_err = lookup_player(p1_query)
    p2_data, p2_err = lookup_player(p2_query)

    conn.close()

    if p1_err:
        return jsonify({"error": p1_err}), 404
    if p2_err:
        return jsonify({"error": p2_err}), 404

    p1_elo = p1_data["elo"] or 1500.0
    p2_elo = p2_data["elo"] or 1500.0
    elo_gap = p1_elo - p2_elo
    abs_gap = abs(elo_gap)

    # Run simulation
    try:
        from src.model.simulate import simulate, elo_to_hold_probs
        from src.model.edge import devig_2way, implied_probability, elo_confidence, TIER_MULT, MIN_ELO_GAP, MIN_EDGE

        tiebreak = "standard"  # default; Roland Garros special case not handled here
        s_a, s_b = elo_to_hold_probs(elo_gap, surface, best_of)
        sim = simulate(s_a, s_b, best_of, tiebreak_rule=tiebreak, n=10_000, seed=42)

        fair_line  = sim.fair_line_games()
        p_over     = sim.p_games_over(book_line)
        p_under    = sim.p_games_under(book_line)
        model_p    = p_over if direction == "OVER" else p_under

        # Market implied prob
        mkt_p = implied_probability(book_odds)

        # Edge
        edge = model_p - mkt_p

        # Kelly stake
        tier = 1  # assume T1 for manual analysis
        elo_conf = elo_confidence(abs_gap)
        kelly_frac = 0.25 * TIER_MULT[tier] * elo_conf
        b = book_odds - 1.0
        raw_kelly = (model_p * b - (1 - model_p)) / b if b > 0 else 0
        raw_kelly = max(0.0, raw_kelly)
        stake = bankroll * raw_kelly * kelly_frac
        stake = round(max(0.0, min(stake, 500.0)), 2)

        return jsonify({
            "p1": {
                "query":     p1_query,
                "name":      p1_data.get("full_name", p1_data.get("raw_name")),
                "player_id": p1_data["player_id"],
                "elo":       round(p1_elo, 1),
                "matches":   p1_data.get("match_count"),
            },
            "p2": {
                "query":     p2_query,
                "name":      p2_data.get("full_name", p2_data.get("raw_name")),
                "player_id": p2_data["player_id"],
                "elo":       round(p2_elo, 1),
                "matches":   p2_data.get("match_count"),
            },
            "surface":     surface,
            "best_of":     best_of,
            "elo_gap":     round(elo_gap, 1),
            "abs_elo_gap": round(abs_gap, 1),
            "simulation": {
                "hold_a":        round(s_a, 4),
                "hold_b":        round(s_b, 4),
                "fair_line_games": round(fair_line, 1),
                "p_over":        round(p_over, 4),
                "p_under":       round(p_under, 4),
            },
            "book_line":   book_line,
            "book_odds":   book_odds,
            "direction":   direction,
            "model_p":     round(model_p, 4),
            "market_p":    round(mkt_p, 4),
            "edge":        round(edge, 4),
            "kelly_frac":  round(kelly_frac, 4),
            "stake_gbp":   stake,
            "qualifies":   edge >= MIN_EDGE and abs_gap >= MIN_ELO_GAP,
            "reject_reason": (
                f"ELO gap {abs_gap:.0f} < {MIN_ELO_GAP} (no edge for equal players)"
                if abs_gap < MIN_ELO_GAP else
                f"Edge {edge:+.1%} below 8% threshold"
                if edge < MIN_EDGE else
                None
            ),
        })
    except Exception as e:
        log.exception("simulation failed")
        return jsonify({"error": str(e)}), 500


# ── run ────────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    log.info("[api] Starting JOB-006 dashboard API on http://localhost:5000")
    log.info("[api] Open dashboard/betting-dashboard.html in your browser")
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
