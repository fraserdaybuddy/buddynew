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
  GET  /api/scrape-status        When odds were last scraped + row counts per sport
  POST /api/scrape-now           Trigger immediate Betfair scrape (~10s)
  POST /api/settle-auto         Auto-settle PENDING paper bets from Betfair outcomes

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

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

log = logging.getLogger("api")
app = Flask(__name__)
CORS(app)

DB_PATH = Path(__file__).parent.parent.parent / "data" / "universe.db"


# ── DB helper ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_list(rows) -> list:
    return [dict(r) for r in rows]


DASHBOARD = Path(__file__).parent.parent.parent / "dashboard" / "betting-dashboard.html"


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    resp = send_file(DASHBOARD)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


# ── CORS (needed for file:// origin) ──────────────────────────────────────────

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
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
    # Only flag data_stale when betfair markets are also empty (live screener bypasses DB)
    if bm_total == 0 and last_tennis and last_tennis < "2026-01-01":
        blockers.append(f"data_stale — tennis DB last updated {last_tennis} (live screener active)")
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
    """Return today's date if betfair_markets has data, else most recent match date."""
    from datetime import date as _date
    sport = request.args.get("sport", "tennis")
    conn  = get_db()
    # Prefer the most recent scraped betfair date — that's what has live lines
    bf_row = conn.execute(
        "SELECT MAX(created_at) as d FROM betfair_markets WHERE sport=?", (sport,)
    ).fetchone()
    if bf_row and bf_row["d"]:
        date = bf_row["d"][:10]  # trim to YYYY-MM-DD
    else:
        row  = conn.execute(
            "SELECT MAX(match_date) as d FROM matches WHERE sport=?", (sport,)
        ).fetchone()
        date = row["d"]
    conn.close()
    return jsonify({"date": date})


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
        from src.model.edge import screen_from_db, screen_from_betfair_markets
        raw = screen_from_db(match_date, bankroll, mode, sport=sport)
        # Fall back to live Betfair event screener when no DB matches for date
        if not raw:
            raw = screen_from_betfair_markets(
                sport=sport,
                bankroll=bankroll, mode=mode, min_liquidity=50.0,
            )
    except Exception as e:
        log.exception("screener failed")
        return jsonify({"error": str(e), "signals": [], "bets": 0, "total": 0})

    def sig_to_dict(s):
        # Derive friendly match name: event_name if set, else strip BF: prefix
        ev = getattr(s, "event_name", "") or ""
        if not ev and s.match_id.startswith("BF:"):
            ev = s.match_id[3:].replace("_", " ")
        return {
            "match_id":      s.match_id,
            "event_name":    ev,
            "sport":         s.sport,
            "market_type":   s.market_type,
            "direction":     s.direction,
            "line":          s.line,
            "fair_line":     getattr(s, "fair_line", 0.0),
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
    # Deduplicate: keep only the most recent bet per (match_id, bet_direction)
    dedup_conditions = list(where)  # copy the conditions list
    dedup_conditions.append(
        "l.placed_at = (SELECT MAX(l2.placed_at) FROM ledger l2 "
        "WHERE l2.match_id = l.match_id AND l2.bet_direction = l.bet_direction)"
    )
    dedup_where_sql = "WHERE " + " AND ".join(dedup_conditions)

    rows = conn.execute(
        f"""SELECT l.rowid, l.bet_id, l.match_id, l.sport, l.bet_direction, l.line,
                  l.odds_taken, l.stake_gbp, l.status, l.profit_loss_gbp,
                  l.placed_at, l.settled_at, l.mode,
                  m.match_date, m.player1_id, m.player2_id
           FROM ledger l
           LEFT JOIN matches m ON l.match_id = m.match_id
           {dedup_where_sql}
           ORDER BY l.placed_at DESC
           LIMIT ?""",
        params + [limit]
    ).fetchall()

    # Summary — count only unique bets (same dedup logic)
    dedup_summary_conds = list(where) + [
        "l.placed_at = (SELECT MAX(l2.placed_at) FROM ledger l2 "
        "WHERE l2.match_id = l.match_id AND l2.bet_direction = l.bet_direction)"
    ]
    dedup_summary_where = "WHERE " + " AND ".join(dedup_summary_conds)
    summary_rows = conn.execute(
        f"""SELECT COUNT(*) as n,
                  SUM(CASE WHEN profit_loss_gbp IS NOT NULL THEN profit_loss_gbp ELSE 0 END) as total_pnl,
                  SUM(stake_gbp) as total_staked
           FROM ledger l {dedup_summary_where}""",
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


# ── POST /api/ledger — place a manual paper bet ──────────────────────────────

@app.route("/api/ledger", methods=["POST"])
def place_bet():
    """
    Log a manually placed paper bet.

    Body JSON:
      match    (str)   — display name e.g. "Price v Littler"
      sport    (str)   — tennis | darts | snooker
      market   (str)   — e.g. "total_180s"
      direction (str)  — OVER | UNDER
      line     (float) — e.g. 8.5
      odds     (float) — decimal odds
      stake    (float) — stake in GBP
      edge     (float) — edge as fraction e.g. 0.12
      kelly_frac (float) — kelly fraction
      mode     (str)   — PAPER | LIVE  (default PAPER)
    """
    import hashlib, time as _time
    body = request.get_json(force=True) or {}

    match     = body.get("match", "")
    sport     = body.get("sport", "tennis")
    market    = body.get("market", "")
    direction = (body.get("direction") or "").upper()
    line      = body.get("line", 0)
    odds      = body.get("odds", 0)
    stake     = body.get("stake", 0)
    edge      = body.get("edge", 0)
    kelly_frac = body.get("kelly_frac", 0)
    mode      = (body.get("mode") or "PAPER").upper()

    if not match or not direction or not odds or not stake:
        return jsonify({"error": "match, direction, odds, stake required"}), 400

    bet_id = hashlib.sha256(f"manual_{match}_{market}_{direction}_{_time.time()}".encode()).hexdigest()[:16]
    bet_dir = f"{market} {direction} {line}"

    conn = get_db()
    conn.execute(
        """INSERT INTO ledger
             (bet_id, match_id, sport, bet_direction, line, odds_taken,
              stake_gbp, status, mode, placed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, datetime('now'))""",
        (bet_id, match, sport, bet_dir, line, odds, stake, mode)
    )
    conn.commit()
    rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    log.info(f"[manual bet] rowid={rowid} {match} {bet_dir} @ {odds}  £{stake}")
    return jsonify({"rowid": rowid, "bet_id": bet_id, "status": "PENDING"})


# ── /api/ledger/settle ────────────────────────────────────────────────────────

@app.route("/api/ledger/settle", methods=["POST"])
def settle_bet():
    """
    Settle a paper bet. Updates status, P&L, and settled_at timestamp.

    Body JSON:
      rowid  (int)  — ledger rowid (from /api/ledger response)
      result (str)  — WON | LOST | VOID
    """
    body   = request.get_json(force=True) or {}
    rowid  = body.get("rowid")
    result = (body.get("result") or "").upper()

    if not rowid or result not in ("WON", "LOST", "VOID"):
        return jsonify({"error": "rowid (int) and result (WON/LOST/VOID) required"}), 400

    conn = get_db()
    row  = conn.execute(
        "SELECT rowid, stake_gbp, odds_taken, status FROM ledger WHERE rowid=?",
        (rowid,)
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"error": f"Bet rowid={rowid} not found"}), 404

    if row["status"] not in ("PENDING",):
        conn.close()
        return jsonify({"error": f"Bet already settled as {row['status']}"}), 409

    if result == "WON":
        pnl = round((row["odds_taken"] - 1.0) * row["stake_gbp"], 2)
    elif result == "LOST":
        pnl = round(-row["stake_gbp"], 2)
    else:
        pnl = 0.0

    conn.execute(
        """UPDATE ledger
           SET status=?, profit_loss_gbp=?, settled_at=datetime('now')
           WHERE rowid=?""",
        (result, pnl, rowid)
    )
    conn.commit()
    conn.close()

    log.info(f"[settle] rowid={rowid} → {result}  pnl=£{pnl:+.2f}")
    return jsonify({"rowid": rowid, "result": result, "pnl": pnl})


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

        # Kelly stake — use governor for consistent formula
        from src.execution.governor import kelly_stake, KELLY_FRACTION
        tier = 1  # assume T1 for manual analysis
        elo_conf   = elo_confidence(abs_gap)
        tier_mult  = TIER_MULT[tier]
        fraction   = KELLY_FRACTION * tier_mult * elo_conf
        kelly_frac = fraction
        stake = kelly_stake(bankroll, edge, book_odds, fraction=fraction) if edge > 0 else 0.0

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
                f"Edge {edge:+.1%} below {MIN_EDGE:.0%} threshold"
                if edge < MIN_EDGE else
                None
            ),
        })
    except Exception as e:
        log.exception("simulation failed")
        return jsonify({"error": str(e)}), 500


# ── Scrape state (shared with background scheduler) ───────────────────────────

_scrape_state = {
    "last_scraped_at": None,   # ISO string UTC
    "last_sport_counts": {},   # {sport: rows}
    "in_progress": False,
}


def _run_scrape() -> dict:
    """Run a Betfair scrape for all sports. Updates _scrape_state. Returns counts dict."""
    from datetime import datetime as _dt, timezone as _tz
    if _scrape_state["in_progress"]:
        return {}

    _scrape_state["in_progress"] = True
    counts = {}
    try:
        from src.execution.betfair import BetfairSession
        from src.execution.scraper import poll_sport
        session = BetfairSession()
        session.login()
        try:
            for sport in ("tennis", "darts", "snooker"):
                try:
                    n = poll_sport(session, sport=sport, days_ahead=2)
                    counts[sport] = n
                    log.info("[scrape] %s: %d rows", sport, n)
                except Exception as se:
                    log.warning("[scrape] %s failed: %s", sport, se)
                    counts[sport] = -1
        finally:
            session.logout()
        _scrape_state["last_scraped_at"] = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _scrape_state["last_sport_counts"] = counts
    except Exception as e:
        log.warning("[scrape] failed: %s", e)
    finally:
        _scrape_state["in_progress"] = False
    return counts


# ── /api/scrape-status ────────────────────────────────────────────────────────

@app.route("/api/scrape-status")
def scrape_status():
    """Return when odds were last scraped and how many rows per sport."""
    return jsonify({
        "last_scraped_at": _scrape_state["last_scraped_at"],
        "sport_counts":    _scrape_state["last_sport_counts"],
        "in_progress":     _scrape_state["in_progress"],
    })


# ── /api/scrape-now ───────────────────────────────────────────────────────────

@app.route("/api/scrape-now", methods=["POST"])
def scrape_now():
    """Trigger an immediate Betfair scrape (runs synchronously, ~10s)."""
    if _scrape_state["in_progress"]:
        return jsonify({"error": "Scrape already in progress"}), 409
    try:
        counts = _run_scrape()
        return jsonify({
            "last_scraped_at": _scrape_state["last_scraped_at"],
            "sport_counts":    counts,
        })
    except Exception as e:
        log.exception("scrape-now failed")
        return jsonify({"error": str(e)}), 500


# ── /api/settle-auto ──────────────────────────────────────────────────────────

@app.route("/api/settle-auto", methods=["POST"])
def settle_auto():
    """
    Trigger auto-settlement of PENDING paper bets from Betfair market outcomes.
    Calls listMarketBook for each pending bet's market; settles if CLOSED.

    Body JSON (optional):
      dry_run (bool) — preview only, no DB writes
    """
    body    = request.get_json(force=True) or {}
    dry_run = bool(body.get("dry_run", False))

    try:
        from src.data.auto_settle import settle_pending_paper_bets
        result = settle_pending_paper_bets(dry_run=dry_run)
        return jsonify(result)
    except Exception as e:
        log.exception("auto-settle failed")
        return jsonify({"error": str(e)}), 500


# ── Background scheduler ───────────────────────────────────────────────────────

def _background_scheduler(scrape_interval_hours: float = 0.5):
    """
    Background thread — keeps data fresh while the server is running.

    Schedule:
      - Starts 20 seconds after server launch (let Flask initialise first)
      - Runs a daily DB backup once per calendar day
      - Re-scrapes Betfair markets for all 3 sports every scrape_interval_hours
        (default 2h) so odds on the dashboard stay current
    """
    import time
    from datetime import date as _date

    time.sleep(20)
    log.info("[scheduler] Background scheduler started (interval=%dh)", scrape_interval_hours)

    last_backup_date = None

    while True:
        today = str(_date.today())

        # ── Daily backup (once per calendar day) ──────────────────────────────
        if last_backup_date != today:
            try:
                from src.database import backup
                dest = backup(label="auto")
                log.info("[scheduler] Daily backup: %s", dest.name)
                last_backup_date = today
            except Exception as e:
                log.warning("[scheduler] Backup failed: %s", e)

        # ── Betfair scrape ─────────────────────────────────────────────────────
        log.info("[scheduler] Auto-scrape starting...")
        _run_scrape()
        log.info("[scheduler] Scrape complete. Next run in %.0fmin.", scrape_interval_hours * 60)

        # ── Auto-settle pending paper bets ─────────────────────────────────────
        try:
            from src.data.auto_settle import settle_pending_paper_bets
            r = settle_pending_paper_bets()
            if r["settled"] > 0:
                log.info("[scheduler] Auto-settled %d bets", r["settled"])
        except Exception as e:
            log.warning("[scheduler] Auto-settle failed: %s", e)

        time.sleep(scrape_interval_hours * 3600)


# ── run ────────────────────────────────────────────────────────────────────────

def main():
    import threading
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    log.info("[api] Starting JOB-006 dashboard API on http://127.0.0.1:5000")
    log.info("[api] Background scheduler: backup daily + re-scrape every 2h")

    t = threading.Thread(target=_background_scheduler, daemon=True, name="scheduler")
    t.start()

    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
