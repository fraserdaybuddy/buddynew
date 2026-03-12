"""
edge.py — Edge calculation and Kelly sizing for tennis, darts, snooker
JOB-006 Sports Betting Model

Input per match:
  - Model probability from simulate.py (or direct regression for darts)
  - Market line from betfair_markets table (or synthetic median in paper mode)
  - Player form and tiers from player_form table
  - ELO ratings from elo_ratings / matches table

Output:
  - BetSignal dataclass with stake, direction, edge
  - Written to ledger in PAPER mode; sent to Betfair API in LIVE mode

Kelly formula (full Kelly with tiered cap):
  p = (1 / odds) * (1 + edge)           — model p derived from market implied
  full_kelly = (b * p - (1-p)) / b      — standard Kelly fraction
  where b = decimal_odds - 1
  fraction = tier_mult * elo_confidence  — confidence scaling
  stake = bankroll * full_kelly * fraction, capped at tiered_cap(edge) % of bank

Tier multipliers (from MODEL_STRATEGY.md):
  T1 (≥10 matches): 1.00
  T2 (3-9 matches):  0.70
  T3 (0-2 matches):  0.40

Tier D filters (never bet if any triggered):
  - Last match > 30 days old
  - Either player < 3 surface matches
  - ELO gap < 50 (model has no edge for near-equal)
  - Liquidity < £50 on market
  - Edge < min_edge threshold

Mode:
  PAPER — compute stakes, log to ledger with mode='PAPER', no real orders
  LIVE  — send order to Betfair API (only after Gate 3 paper criteria met)
"""

import sqlite3
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("edge")

DB_PATH = Path(__file__).parent.parent.parent / "data" / "universe.db"

# ── Kelly constants ────────────────────────────────────────────────────────────

TIER_MULT = {1: 1.00, 2: 0.70, 3: 0.40}
MIN_EDGE = 0.05             # 5% minimum edge
MIN_ELO_GAP = 50.0          # minimum ELO separation (Gate 4 validated)
MIN_LIQUIDITY_GBP = 50.0    # minimum matched volume on Betfair
STALE_DAYS = 30             # last match must be within 30 days
MIN_SURFACE_MATCHES = 3     # minimum surface-specific matches for T1 treatment


# ── Bet signal ─────────────────────────────────────────────────────────────────

@dataclass
class BetSignal:
    match_id:       str
    sport:          str
    market_type:    str          # 'total_games_ou' | 'total_sets_ou' | 'first_set'
    direction:      str          # 'OVER' | 'UNDER' | 'HOME' | 'AWAY'
    line:           float        # the market line (e.g. 22.5 for total games)
    model_p:        float        # model probability this direction wins
    market_p:       float        # implied probability from market odds
    edge:           float        # model_p - market_p
    odds:           float        # decimal odds offered
    kelly_frac:     float        # raw Kelly fraction
    stake_gbp:      float        # recommended stake after caps
    tier:           int          # 1 / 2 / 3 (worse player tier)
    mode:           str          # 'PAPER' | 'LIVE'
    synthetic_line: bool         # True if market line was synthesised (no real Betfair data)
    filters_passed: list = field(default_factory=list)
    reject_reason:  Optional[str] = None
    fair_line:      float = 0.0  # model's fair line (e.g. 21.8 games)
    event_name:     str = ""     # human-readable "P1 v P2" from Betfair event


# ── Kelly calculation ──────────────────────────────────────────────────────────

def elo_confidence(abs_elo_gap: float) -> float:
    """
    Scale factor in [0, 1] based on ELO gap magnitude.
    At gap = MIN_ELO_GAP (50): 0.0   At gap = 350+: 1.0
    """
    lo, hi = MIN_ELO_GAP, 350.0
    if abs_elo_gap <= lo:
        return 0.0
    if abs_elo_gap >= hi:
        return 1.0
    return (abs_elo_gap - lo) / (hi - lo)


def recommended_stake(
    edge: float,
    decimal_odds: float,
    bankroll: float,
    tier: int,
    abs_elo_gap: float,
) -> tuple:
    """
    Returns (fraction_used, stake_gbp) via governor.kelly_stake().

    Full Kelly with confidence scaling and tiered cap:
      fraction = tier_mult × elo_confidence  (KELLY_FRACTION=1.0, full Kelly base)
      tier_mult      — data quality: T1=1.0  T2=0.70  T3=0.40
      elo_confidence — ELO separation: 0.0 at gap=50 → 1.0 at gap=350+

    Governor applies tiered bankroll % cap (8–22% depending on edge)
    and absolute £500 ceiling. Floored at £5.
    """
    from src.execution.governor import kelly_stake, KELLY_FRACTION
    tier_mult = TIER_MULT.get(tier, 0.40)
    elo_conf  = elo_confidence(abs_elo_gap)
    fraction  = KELLY_FRACTION * tier_mult * elo_conf
    stake     = kelly_stake(bankroll, edge, decimal_odds, fraction=fraction)
    return (fraction, stake)


# ── Market line helpers ────────────────────────────────────────────────────────

def implied_probability(decimal_odds: float) -> float:
    """Convert decimal odds to implied probability (no margin removed)."""
    if decimal_odds <= 1.0:
        return 1.0
    return 1.0 / decimal_odds


def devig_2way(over_odds: float, under_odds: float) -> tuple:
    """
    Remove bookmaker margin from a 2-way market.
    Returns (p_over, p_under) that sum to 1.0.
    Uses multiplicative devig.
    """
    raw_over  = 1.0 / over_odds
    raw_under = 1.0 / under_odds
    total = raw_over + raw_under
    return raw_over / total, raw_under / total


# ── Tier D filter ──────────────────────────────────────────────────────────────

def check_filters(
    elo_gap: float,
    p1_tier: int,
    p2_tier: int,
    p1_last_match: Optional[str],
    p2_last_match: Optional[str],
    match_date: str,
    p1_surface_n: int,
    p2_surface_n: int,
    liquidity_gbp: Optional[float],
    is_synthetic: bool,
) -> tuple:
    """
    Returns (passed: bool, reason: str | None, filters_passed: list).
    """
    passed = []
    tier = max(p1_tier, p2_tier)   # use weaker player's tier

    if elo_gap < MIN_ELO_GAP:
        return False, f"ELO gap {elo_gap:.0f} < {MIN_ELO_GAP:.0f} (model no edge)", passed

    if not is_synthetic and liquidity_gbp is not None and liquidity_gbp < MIN_LIQUIDITY_GBP:
        return False, f"Liquidity £{liquidity_gbp:.0f} < £{MIN_LIQUIDITY_GBP:.0f}", passed
    passed.append("liquidity_ok")

    min_surface = MIN_SURFACE_MATCHES
    if p1_surface_n < min_surface or p2_surface_n < min_surface:
        # Downgrade tier but don't block — surface form missing, use overall
        pass
    passed.append("surface_data_ok")

    # Stale form check
    for pid_last, label in [(p1_last_match, "p1"), (p2_last_match, "p2")]:
        if pid_last:
            try:
                from datetime import date
                last = date.fromisoformat(pid_last)
                today = date.fromisoformat(match_date)
                if (today - last).days > STALE_DAYS:
                    return False, f"{label} last match {pid_last} > {STALE_DAYS} days ago", passed
            except ValueError:
                pass
    passed.append("form_fresh")

    return True, None, passed


# ── Poisson fair-line helper (darts / snooker) ────────────────────────────────

def _p_poisson_over(mean: float, line: float) -> float:
    """
    P(X > line) where X ~ Poisson(mean).
    Handles half-lines: P(X > 4.5) = P(X >= 5).
    Uses log-space accumulation to avoid overflow for large means.
    """
    import math
    if mean <= 0:
        return 0.0
    k_max = int(line)  # floor — P(X <= k_max) = CDF at k_max
    log_mean = math.log(mean)
    log_pmf = -mean   # log P(X=0)
    cdf = 0.0
    for k in range(k_max + 1):
        cdf += math.exp(log_pmf)
        if k < k_max:
            log_pmf += log_mean - math.log(k + 1)
    return max(0.0, min(1.0, 1.0 - cdf))


# ── Tournament surface / format lookup ────────────────────────────────────────
#
# Maps tournament name keywords (lowercase) → (surface, best_of).
# Keys are substrings matched against the Betfair competition_name.
# Grand Slams use best_of=5 (ATP); WTA Grand Slams are BO3 but we can't
# distinguish WTA vs ATP from market data alone — BO5 is the safer default
# since most liquidity is ATP.  Non-Grand-Slam events are always BO3.
#
TOURNAMENT_SURFACE_MAP: list[tuple[str, str, int]] = [
    # Grass
    ("wimbledon",         "Grass", 5),
    ("queen's",           "Grass", 3),
    ("queens",            "Grass", 3),
    ("eastbourne",        "Grass", 3),
    ("halle",             "Grass", 3),
    ("s-hertogenbosch",   "Grass", 3),
    ("nottingham",        "Grass", 3),
    ("birmingham",        "Grass", 3),
    # Clay — Grand Slam
    ("roland garros",     "Clay",  5),
    ("french open",       "Clay",  5),
    # Clay — Masters / 500 / 250
    ("monte carlo",       "Clay",  3),
    ("monte-carlo",       "Clay",  3),
    ("madrid",            "Clay",  3),
    ("rome",              "Clay",  3),
    ("internazionali",    "Clay",  3),
    ("barcelona",         "Clay",  3),
    ("hamburg",           "Clay",  3),
    ("munich",            "Clay",  3),
    ("estoril",           "Clay",  3),
    ("lyon",              "Clay",  3),
    ("geneva",            "Clay",  3),
    ("istanbul",          "Clay",  3),
    ("buenos aires",      "Clay",  3),
    ("rio",               "Clay",  3),
    ("acapulco",          "Clay",  3),
    ("santiago",          "Clay",  3),
    ("marrakech",         "Clay",  3),
    ("bastad",            "Clay",  3),
    ("kitzbuhel",         "Clay",  3),
    ("umag",              "Clay",  3),
    ("gstaad",            "Clay",  3),
    # Hard — Grand Slams
    ("australian open",   "Hard",  5),
    ("us open",           "Hard",  5),
    # Hard — Masters / 500 / 250
    ("indian wells",      "Hard",  3),
    ("bnp paribas open",  "Hard",  3),
    ("miami",             "Hard",  3),
    ("canada",            "Hard",  3),
    ("cincinnati",        "Hard",  3),
    ("western & southern","Hard",  3),
    ("shanghai",          "Hard",  3),
    ("paris",             "Hard",  3),
    ("bercy",             "Hard",  3),
    ("vienna",            "Hard",  3),
    ("basel",             "Hard",  3),
    ("stockholm",         "Hard",  3),
    ("moscow",            "Hard",  3),
    ("doha",              "Hard",  3),
    ("dubai",             "Hard",  3),
    ("abu dhabi",         "Hard",  3),
    ("adelaide",          "Hard",  3),
    ("auckland",          "Hard",  3),
    ("sydney",            "Hard",  3),
    ("brisbane",          "Hard",  3),
    ("hong kong",         "Hard",  3),
    ("beijing",           "Hard",  3),
    ("tokyo",             "Hard",  3),
    ("antwerp",           "Hard",  3),
    ("washington",        "Hard",  3),
    ("atlanta",           "Hard",  3),
    ("los cabos",         "Hard",  3),
    ("winston-salem",     "Hard",  3),
    ("metz",              "Hard",  3),
    ("gijon",             "Hard",  3),
    ("nur-sultan",        "Hard",  3),
]

_DEFAULT_SURFACE = "Hard"
_DEFAULT_BEST_OF = 3


def _detect_surface_and_format(competition_name: str) -> tuple[str, int]:
    """
    Infer (surface, best_of) from Betfair competition_name using keyword matching.
    Returns (_DEFAULT_SURFACE, _DEFAULT_BEST_OF) if no match found.
    """
    if not competition_name:
        return _DEFAULT_SURFACE, _DEFAULT_BEST_OF
    lower = competition_name.lower()
    for keyword, surface, best_of in TOURNAMENT_SURFACE_MAP:
        if keyword in lower:
            return surface, best_of
    return _DEFAULT_SURFACE, _DEFAULT_BEST_OF


# ── Tennis edge screener ───────────────────────────────────────────────────────

def screen_tennis_match(
    match_id: str,
    match_date: str,
    p1_id: str,
    p2_id: str,
    p1_elo_surface: float,
    p2_elo_surface: float,
    surface: str,
    best_of: int,
    tiebreak_rule: str,
    p1_serve_str: Optional[float],
    p2_serve_str: Optional[float],
    p1_tier: int,
    p2_tier: int,
    p1_last_match: Optional[str],
    p2_last_match: Optional[str],
    p1_surface_n: int,
    p2_surface_n: int,
    market_lines: dict,
    bankroll: float,
    mode: str = "PAPER",
    min_edge: float = MIN_EDGE,
) -> list:
    """
    Screen a tennis match for bet signals across all available markets.

    market_lines format:
      {
        'total_games': {'line': 22.5, 'over_odds': 1.85, 'under_odds': 2.05,
                        'liquidity': 500, 'synthetic': False},
        'total_sets':  {'line': 2.5,  'over_odds': 1.90, 'under_odds': 1.95,
                        'liquidity': 300, 'synthetic': False},
        'first_set':   {'p1_odds': 1.70, 'p2_odds': 2.10,
                        'liquidity': 800, 'synthetic': False},
      }
    If market_lines is empty or a key is missing, synthetic lines are generated.

    Returns list of BetSignal (0-3 signals per match).
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from src.model.simulate import simulate, elo_to_hold_probs, elo_to_p_match, p_match_to_p_set

    signals = []
    elo_gap = p1_elo_surface - p2_elo_surface
    abs_gap = abs(elo_gap)

    # ── Tier D check ──────────────────────────────────────────────────────
    tier = max(p1_tier, p2_tier)
    for market_key in ['total_games', 'total_sets', 'first_set']:
        ml = market_lines.get(market_key, {})
        is_synth = ml.get("synthetic", True)
        liq = ml.get("liquidity") if not is_synth else None

        ok, reject_reason, filters = check_filters(
            abs_gap, p1_tier, p2_tier,
            p1_last_match, p2_last_match, match_date,
            p1_surface_n, p2_surface_n,
            liq, is_synth,
        )
        if not ok:
            signals.append(BetSignal(
                match_id=match_id, sport="tennis", market_type=market_key,
                direction="NONE", line=0, model_p=0, market_p=0, edge=0,
                odds=0, kelly_frac=0, stake_gbp=0,
                tier=tier, mode=mode, synthetic_line=is_synth,
                reject_reason=reject_reason,
            ))
            continue

    # ── Simulation ────────────────────────────────────────────────────────
    s_a, s_b = elo_to_hold_probs(elo_gap, surface, best_of)
    sim = simulate(s_a, s_b, best_of, tiebreak_rule=tiebreak_rule, n=10_000, seed=42)

    # ── Market: total_games ────────────────────────────────────────────────
    ml_games = market_lines.get("total_games", {})
    is_synth_g = ml_games.get("synthetic", True)

    if is_synth_g:
        # Synthetic: use simulation median as fair line
        line_g = sim.fair_line_games()
        # Use equal odds (no edge to exploit synthetically)
        over_odds_g = under_odds_g = 1.909  # 52.5% implied each
    else:
        line_g = ml_games["line"]
        over_odds_g  = ml_games.get("over_odds",  1.909)
        under_odds_g = ml_games.get("under_odds", 1.909)

    p_over_g  = sim.p_games_over(line_g)
    p_under_g = sim.p_games_under(line_g)

    # Devig market
    mkt_p_over_g, mkt_p_under_g = devig_2way(over_odds_g, under_odds_g)

    edge_over_g  = p_over_g  - mkt_p_over_g
    edge_under_g = p_under_g - mkt_p_under_g

    for direction, model_p, market_p, edge, odds in [
        ("OVER",  p_over_g,  mkt_p_over_g,  edge_over_g,  over_odds_g),
        ("UNDER", p_under_g, mkt_p_under_g, edge_under_g, under_odds_g),
    ]:
        if edge >= min_edge and not is_synth_g:
            fraction, stake = recommended_stake(edge, odds, bankroll, tier, abs_gap)
            signals.append(BetSignal(
                match_id=match_id, sport="tennis", market_type="total_games",
                direction=direction, line=line_g,
                model_p=round(model_p, 4), market_p=round(market_p, 4),
                edge=round(edge, 4), odds=odds,
                kelly_frac=round(fraction, 4), stake_gbp=stake,
                tier=tier, mode=mode, synthetic_line=False,
                filters_passed=["elo_gap_ok", "liquidity_ok", "form_fresh"],
            ))

    # ── Market: total_sets ────────────────────────────────────────────────
    ml_sets = market_lines.get("total_sets", {})
    is_synth_s = ml_sets.get("synthetic", True)

    if is_synth_s:
        line_s = sim.fair_line_sets()
        over_odds_s = under_odds_s = 1.909
    else:
        line_s = ml_sets["line"]
        over_odds_s  = ml_sets.get("over_odds",  1.909)
        under_odds_s = ml_sets.get("under_odds", 1.909)

    p_over_s  = sim.p_sets_over(line_s)
    p_under_s = sim.p_sets_under(line_s)
    mkt_p_over_s, mkt_p_under_s = devig_2way(over_odds_s, under_odds_s)
    edge_over_s  = p_over_s  - mkt_p_over_s
    edge_under_s = p_under_s - mkt_p_under_s

    for direction, model_p, market_p, edge, odds in [
        ("OVER",  p_over_s,  mkt_p_over_s,  edge_over_s,  over_odds_s),
        ("UNDER", p_under_s, mkt_p_under_s, edge_under_s, under_odds_s),
    ]:
        if edge >= min_edge and not is_synth_s:
            fraction, stake = recommended_stake(edge, odds, bankroll, tier, abs_gap)
            signals.append(BetSignal(
                match_id=match_id, sport="tennis", market_type="total_sets",
                direction=direction, line=line_s,
                model_p=round(model_p, 4), market_p=round(market_p, 4),
                edge=round(edge, 4), odds=odds,
                kelly_frac=round(fraction, 4), stake_gbp=stake,
                tier=tier, mode=mode, synthetic_line=False,
                filters_passed=["elo_gap_ok", "liquidity_ok", "form_fresh"],
            ))

    # ── Market: first_set ─────────────────────────────────────────────────
    ml_fs = market_lines.get("first_set", {})
    is_synth_fs = ml_fs.get("synthetic", True)

    p_match = elo_to_p_match(elo_gap)
    p_set_p1 = p_match_to_p_set(p_match, best_of)

    if is_synth_fs:
        p1_odds = 1.0 / p_set_p1 if p_set_p1 > 0 else 99
        p2_odds = 1.0 / (1 - p_set_p1) if p_set_p1 < 1 else 99
    else:
        p1_odds = ml_fs.get("p1_odds", 1.0 / p_set_p1)
        p2_odds = ml_fs.get("p2_odds", 1.0 / (1 - p_set_p1))

    mkt_p1_fs = 1.0 / p1_odds
    mkt_p2_fs = 1.0 / p2_odds
    total_mkt = mkt_p1_fs + mkt_p2_fs
    mkt_p1_fs /= total_mkt   # devig
    mkt_p2_fs /= total_mkt

    edge_p1 = p_set_p1 - mkt_p1_fs
    edge_p2 = (1 - p_set_p1) - mkt_p2_fs

    for player, model_p, market_p, edge, odds in [
        ("HOME", p_set_p1,       mkt_p1_fs, edge_p1, p1_odds),
        ("AWAY", 1-p_set_p1,     mkt_p2_fs, edge_p2, p2_odds),
    ]:
        if edge >= min_edge and not is_synth_fs:
            fraction, stake = recommended_stake(edge, odds, bankroll, tier, abs_gap)
            signals.append(BetSignal(
                match_id=match_id, sport="tennis", market_type="first_set",
                direction=player, line=0,
                model_p=round(model_p, 4), market_p=round(market_p, 4),
                edge=round(edge, 4), odds=odds,
                kelly_frac=round(fraction, 4), stake_gbp=stake,
                tier=tier, mode=mode, synthetic_line=False,
                filters_passed=["elo_gap_ok", "liquidity_ok", "form_fresh"],
            ))

    # If no real market lines, return synthetic report (paper diagnostics)
    if not signals or all(s.stake_gbp == 0 and s.reject_reason is None for s in signals):
        # Return a synthetic preview signal for paper mode diagnostics
        best_edge = max([edge_over_g, edge_under_g, edge_over_s, edge_under_s,
                         edge_p1, edge_p2], key=abs)
        best_dir  = max(
            [("OVER_G", edge_over_g), ("UNDER_G", edge_under_g),
             ("OVER_S", edge_over_s), ("UNDER_S", edge_under_s),
             ("P1_FS",  edge_p1),     ("P2_FS",   edge_p2)],
            key=lambda x: x[1]
        )
        signals = [BetSignal(
            match_id=match_id, sport="tennis", market_type="synthetic_preview",
            direction=best_dir[0], line=line_g,
            model_p=round(sim.p_games_under(line_g), 4),
            market_p=round(mkt_p_under_g, 4),
            edge=round(best_dir[1], 4),
            odds=0, kelly_frac=0, stake_gbp=0,
            tier=tier, mode="PAPER", synthetic_line=True,
            filters_passed=["elo_gap_ok"],
            reject_reason="No real market lines — synthetic preview only",
        )]

    return signals


# ── DB-driven screener (runs against all upcoming matches) ───────────────────

def screen_from_db(
    match_date: str,
    bankroll: float = 1000.0,
    mode: str = "PAPER",
    sport: str = "tennis",
) -> list:
    """
    Load today's matches from DB, look up ELO + form, query market lines,
    and return all BetSignals.

    In paper mode: synthetic lines are used when betfair_markets is empty.
    In live mode: only real market lines are used.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if sport == "tennis":
        signals = _screen_tennis_from_db(conn, match_date, bankroll, mode)
    else:
        signals = []

    conn.close()
    return signals


def _screen_tennis_from_db(
    conn: sqlite3.Connection,
    match_date: str,
    bankroll: float,
    mode: str,
) -> list:
    """Load tennis matches for match_date and screen them."""
    matches = conn.execute(
        """SELECT m.match_id, m.match_date, m.player1_id, m.player2_id,
                  m.p1_elo_surface, m.p2_elo_surface,
                  m.best_of, m.round,
                  t.surface, t.name as tournament_name
           FROM matches m
           JOIN tournaments t ON m.tournament_id = t.tournament_id
           WHERE m.sport = 'tennis'
             AND m.match_date = ?
             AND m.p1_elo_surface IS NOT NULL
           ORDER BY m.match_id""",
        (match_date,)
    ).fetchall()

    signals = []

    for m in matches:
        m = dict(m)
        p1_id = m["player1_id"]
        p2_id = m["player2_id"]
        surface = m["surface"] or "Hard"
        best_of = m["best_of"] or 3

        # Tiebreak rule
        tiebreak_rule = "advantage" if m["tournament_name"] == "Roland Garros" else "standard"

        # Load most recent form for each player
        def get_form(pid):
            row = conn.execute(
                """SELECT player_tier, matches_counted, avg_serve_str_hard,
                          avg_serve_str_clay, avg_serve_str_grass,
                          avg_serve_strength, as_of_date
                   FROM player_form
                   WHERE player_id = ? AND sport = 'tennis'
                     AND as_of_date < ?
                   ORDER BY as_of_date DESC LIMIT 1""",
                (pid, match_date)
            ).fetchone()
            if row:
                return dict(row)
            return {"player_tier": 3, "matches_counted": 0,
                    "avg_serve_strength": None, "as_of_date": None}

        p1_form = get_form(p1_id)
        p2_form = get_form(p2_id)

        # Surface-specific serve strength
        surf_key = {"Hard": "avg_serve_str_hard", "Clay": "avg_serve_str_clay",
                    "Grass": "avg_serve_str_grass"}.get(surface, "avg_serve_strength")
        p1_serve = p1_form.get(surf_key) or p1_form.get("avg_serve_strength")
        p2_serve = p2_form.get(surf_key) or p2_form.get("avg_serve_strength")

        # Market lines from betfair_markets
        mkt_rows = conn.execute(
            """SELECT market_type, line, over_odds, under_odds, total_matched
               FROM betfair_markets
               WHERE match_id = ? AND verified = 1""",
            (m["match_id"],)
        ).fetchall()
        market_lines = {}
        for row in mkt_rows:
            row = dict(row)
            mtype = row["market_type"].lower()
            market_lines[mtype] = {
                "line": row["line"],
                "over_odds": row["over_odds"],
                "under_odds": row["under_odds"],
                "liquidity": row["total_matched"],
                "synthetic": False,
            }

        match_signals = screen_tennis_match(
            match_id=m["match_id"],
            match_date=match_date,
            p1_id=p1_id,
            p2_id=p2_id,
            p1_elo_surface=m["p1_elo_surface"],
            p2_elo_surface=m["p2_elo_surface"],
            surface=surface,
            best_of=best_of,
            tiebreak_rule=tiebreak_rule,
            p1_serve_str=p1_serve,
            p2_serve_str=p2_serve,
            p1_tier=p1_form["player_tier"],
            p2_tier=p2_form["player_tier"],
            p1_last_match=p1_form["as_of_date"],
            p2_last_match=p2_form["as_of_date"],
            p1_surface_n=p1_form["matches_counted"],
            p2_surface_n=p2_form["matches_counted"],
            market_lines=market_lines,
            bankroll=bankroll,
            mode=mode,
        )
        signals.extend(match_signals)

    return signals


# ── Live screener — reads betfair_markets event names directly ────────────────

def _lookup_elo_by_name(conn: sqlite3.Connection, query: str, surface: str) -> tuple:
    """
    Find the best-matching player_id and ELO for a name fragment.
    Betfair names are like "A Sabalenka", "Djokovic", "Le Tien", "Carlos Alcaraz".
    Searches player full_name and player_aliases raw_name, tries the last word
    (surname) as a fallback.

    Returns (player_id, elo, full_name, match_count) or (None, 1500, query, 0).
    """
    surf_map = {"Hard": "Hard", "Clay": "Clay", "Grass": "Grass"}
    surf_key = surf_map.get(surface, "Hard")

    def _search(fragment):
        rows = conn.execute(
            """SELECT pa.player_id, p.full_name, er.elo, er.match_count
               FROM player_aliases pa
               JOIN players p ON pa.player_id = p.player_id
               LEFT JOIN elo_ratings er
                     ON pa.player_id = er.player_id AND er.surface = ?
               WHERE pa.raw_name LIKE ? COLLATE NOCASE
                  OR p.full_name LIKE ? COLLATE NOCASE
               ORDER BY er.match_count DESC NULLS LAST, p.full_name
               LIMIT 1""",
            (surf_key, f"%{fragment}%", f"%{fragment}%")
        ).fetchone()
        if rows and rows["elo"] is None:
            # Try Overall ELO as fallback
            overall = conn.execute(
                "SELECT elo, match_count FROM elo_ratings WHERE player_id=? AND surface='Overall'",
                (rows["player_id"],)
            ).fetchone()
            if overall:
                return rows["player_id"], overall["elo"], rows["full_name"], overall["match_count"]
        return (rows["player_id"], rows["elo"], rows["full_name"], rows["match_count"]) if rows else None

    result = _search(query)
    if result:
        return result

    # Fallback: try surname only (last word)
    surname = query.strip().split()[-1]
    if len(surname) >= 3 and surname != query.strip():
        result = _search(surname)
        if result:
            return result

    # Last resort: try each word of the name
    for word in query.strip().split():
        if len(word) >= 4:
            result = _search(word)
            if result:
                return result

    return None, 1500.0, query, 0


def _lookup_form_by_name(conn: sqlite3.Connection, query: str, sport: str) -> tuple:
    """
    Find player_id and form stats for a darts/snooker player by name fragment.
    Returns (player_id, full_name, avg_stat_per_leg, match_count) or (None, query, 0.30, 0).
    avg_stat_per_leg = avg_180s_per_leg for darts, avg_centuries_per_frame for snooker.
    """
    stat_col = "avg_180s_per_leg" if sport == "darts" else "avg_centuries_per_frame"
    default  = 0.30 if sport == "darts" else 0.08

    def _search(fragment):
        row = conn.execute(
            f"""SELECT p.player_id, p.full_name,
                       pf.{stat_col} as stat, pf.matches_counted
                FROM players p
                LEFT JOIN player_aliases pa ON p.player_id = pa.player_id
                LEFT JOIN player_form pf
                       ON p.player_id = pf.player_id AND pf.sport = ?
                WHERE (p.full_name LIKE ? COLLATE NOCASE
                    OR pa.raw_name  LIKE ? COLLATE NOCASE)
                ORDER BY pf.as_of_date DESC NULLS LAST, pf.matches_counted DESC NULLS LAST
                LIMIT 1""",
            (sport, f"%{fragment}%", f"%{fragment}%")
        ).fetchone()
        return dict(row) if row else None

    result = _search(query)
    if not result:
        surname = query.strip().split()[-1]
        if len(surname) >= 3 and surname != query.strip():
            result = _search(surname)
    if not result:
        for word in query.strip().split():
            if len(word) >= 4:
                result = _search(word)
                if result:
                    break

    if result and result["player_id"]:
        stat  = result["stat"] or default
        count = result["matches_counted"] or 0
        return result["player_id"], result["full_name"], stat, count
    return None, query, default, 0


def screen_from_betfair_markets(
    sport: str = "tennis",
    surface: str = "",
    best_of: int = 0,
    bankroll: float = 1000.0,
    mode: str = "PAPER",
    min_liquidity: float = 50.0,
) -> list:
    """
    Live screener that works WITHOUT needing 2026 matches in the DB.

    Tennis: ELO lookup → Monte Carlo simulation → edge vs Betfair line.
    Darts:  player_form avg_180s_per_leg + Poisson fair-line model.
    Snooker: player_form avg_centuries_per_frame + Poisson fair-line model.

    Surface and best_of are auto-detected from competition_name using
    TOURNAMENT_SURFACE_MAP (tennis only).  Pass surface/best_of explicitly to override.

    Only processes markets with total_matched >= min_liquidity.
    Returns list of BetSignal — one per event per market type.
    """
    from src.model.simulate import simulate, elo_to_hold_probs

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Get distinct events with their best lines (by liquidity)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(betfair_markets)").fetchall()]
    ev_col   = "event_name"       if "event_name"       in cols else "NULL"
    comp_col = "competition_name" if "competition_name" in cols else "NULL"

    rows = conn.execute(
        f"""SELECT {ev_col} as event_name, {comp_col} as competition_name,
                  market_type, line, over_odds, under_odds, total_matched
           FROM betfair_markets
           WHERE sport = ?
             AND over_odds IS NOT NULL
             AND under_odds IS NOT NULL
             AND (total_matched >= ? OR total_matched IS NULL)
           ORDER BY event_name, market_type, total_matched DESC""",
        (sport, min_liquidity)
    ).fetchall()

    if not rows:
        conn.close()
        return []

    # Group rows: (event_name, competition_name) → market_type → list of rows
    from collections import defaultdict
    events: dict = defaultdict(lambda: defaultdict(list))
    event_competition: dict = {}
    for r in rows:
        key = r["event_name"]
        events[key][r["market_type"]].append(dict(r))
        if key not in event_competition:
            event_competition[key] = r["competition_name"] or ""

    signals = []

    for event_name, markets in events.items():
        # Parse "PlayerA v PlayerB"
        parts = [p.strip() for p in event_name.split(" v ")]
        if len(parts) != 2:
            continue
        name_a, name_b = parts

        match_id = f"BF:{event_name[:24].replace(' ','_')}"

        # ── Darts / Snooker — form-based Poisson model ────────────────────
        if sport in ("darts", "snooker"):
            from src.execution.governor import kelly_stake as kelly_stake_fn
            stat_mkt = "total_180s" if sport == "darts" else "total_centuries"
            if stat_mkt not in markets:
                continue

            p1_id, p1_name, p1_stat, p1_n = _lookup_form_by_name(conn, name_a, sport)
            p2_id, p2_name, p2_stat, p2_n = _lookup_form_by_name(conn, name_b, sport)

            # Average legs/frames per match from historical DB
            legs_col = "legs_sets_total"
            avg_row = conn.execute(
                f"SELECT AVG({legs_col}) FROM matches WHERE sport=? AND {legs_col} IS NOT NULL",
                (sport,)
            ).fetchone()
            avg_legs = (avg_row[0] or 0) if avg_row else 0
            if avg_legs < 5:
                avg_legs = 18.0 if sport == "darts" else 25.0  # safe defaults

            fair = (p1_stat + p2_stat) * avg_legs
            tier = 1 if min(p1_n, p2_n) >= 10 else (2 if min(p1_n, p2_n) >= 3 else 3)

            market_rows_list = markets[stat_mkt]
            best_row = min(market_rows_list, key=lambda r: abs(r["line"] - fair))
            line       = best_row["line"]
            over_odds  = best_row["over_odds"]
            under_odds = best_row["under_odds"]
            matched    = best_row["total_matched"] or 0

            if not over_odds or not under_odds:
                continue

            p_over  = _p_poisson_over(fair, line)
            p_under = 1.0 - p_over
            mkt_p_over, mkt_p_under = devig_2way(over_odds, under_odds)

            from datetime import date as _date
            for direction, model_p, market_p, edge_val, odds in [
                ("OVER",  p_over,  mkt_p_over,  p_over  - mkt_p_over,  over_odds),
                ("UNDER", p_under, mkt_p_under, p_under - mkt_p_under, under_odds),
            ]:
                fraction = (TIER_MULT.get(tier, 0.40) *
                            min(max((p1_n + p2_n) / 20.0, 0.0), 1.0))  # scale by data volume
                stake = (kelly_stake_fn(bankroll, edge_val, odds, fraction=fraction)
                         if edge_val >= MIN_EDGE else 0.0)
                signals.append(BetSignal(
                    match_id=match_id, sport=sport,
                    market_type=stat_mkt,
                    direction=direction, line=line,
                    model_p=round(model_p, 4), market_p=round(market_p, 4),
                    edge=round(edge_val, 4), odds=odds,
                    kelly_frac=round(fraction, 4), stake_gbp=stake,
                    tier=tier, mode=mode, synthetic_line=False,
                    filters_passed=["form_data_ok"],
                    reject_reason=None if edge_val >= MIN_EDGE else f"edge {edge_val:+.1%} < {MIN_EDGE:.0%}",
                    fair_line=round(fair, 1), event_name=event_name,
                ))
            continue  # skip tennis logic below

        # ── Tennis — ELO + Monte Carlo ─────────────────────────────────────
        comp_name = event_competition.get(event_name, "")
        ev_surface, ev_best_of = _detect_surface_and_format(comp_name)
        if surface:     ev_surface = surface
        if best_of > 0: ev_best_of = best_of
        log.debug(f"[edge] {event_name!r} comp={comp_name!r} → surface={ev_surface} BO{ev_best_of}")

        p1_id, p1_elo, p1_name, p1_n = _lookup_elo_by_name(conn, name_a, ev_surface)
        p2_id, p2_elo, p2_name, p2_n = _lookup_elo_by_name(conn, name_b, ev_surface)

        p1_elo = p1_elo or 1500.0
        p2_elo = p2_elo or 1500.0
        elo_gap = p1_elo - p2_elo
        abs_gap = abs(elo_gap)

        try:
            s_a, s_b = elo_to_hold_probs(elo_gap, ev_surface, ev_best_of)
            sim = simulate(s_a, s_b, ev_best_of, tiebreak_rule="standard", n=10_000, seed=42)
        except Exception as e:
            log.warning(f"[edge] simulation failed for {event_name}: {e}")
            continue

        tier = 1 if min(p1_n or 0, p2_n or 0) >= 10 else (2 if min(p1_n or 0, p2_n or 0) >= 3 else 3)

        for market_type, market_rows in markets.items():
            if market_type == "total_games":
                fair = sim.fair_line_games()
                p_over_fn  = sim.p_games_over
                p_under_fn = sim.p_games_under
            elif market_type == "total_sets":
                fair = sim.fair_line_sets()
                p_over_fn  = sim.p_sets_over
                p_under_fn = sim.p_sets_under
            else:
                continue

            # Find the row with line closest to fair
            best_row = min(market_rows, key=lambda r: abs(r["line"] - fair))
            line      = best_row["line"]
            over_odds  = best_row["over_odds"]
            under_odds = best_row["under_odds"]
            matched    = best_row["total_matched"] or 0

            if not over_odds or not under_odds:
                continue

            p_over  = p_over_fn(line)
            p_under = p_under_fn(line)
            mkt_p_over, mkt_p_under = devig_2way(over_odds, under_odds)

            for direction, model_p, market_p, edge_val, odds in [
                ("OVER",  p_over,  mkt_p_over,  p_over  - mkt_p_over,  over_odds),
                ("UNDER", p_under, mkt_p_under, p_under - mkt_p_under, under_odds),
            ]:
                if abs(edge_val) < 0.001:
                    continue

                fraction, stake = recommended_stake(edge_val, odds, bankroll, tier, abs_gap) \
                    if edge_val >= MIN_EDGE else (0.0, 0.0)

                # ELO gap filter
                from datetime import date as _date
                ok, reject_reason, filters = check_filters(
                    abs_gap, tier, tier,
                    None, None, str(_date.today()),
                    p1_n or 0, p2_n or 0,
                    matched if matched > 0 else None,
                    False,
                )

                signals.append(BetSignal(
                    match_id=match_id,
                    sport=sport,
                    market_type=market_type,
                    direction=direction,
                    line=line,
                    model_p=round(model_p, 4),
                    market_p=round(market_p, 4),
                    edge=round(edge_val, 4),
                    odds=odds,
                    kelly_frac=round(fraction, 4),
                    stake_gbp=stake if (ok and edge_val >= MIN_EDGE) else 0.0,
                    tier=tier,
                    mode=mode,
                    synthetic_line=False,
                    filters_passed=filters,
                    reject_reason=None if ok else reject_reason,
                    fair_line=round(fair, 1),
                    event_name=event_name,
                ))

    conn.close()
    return signals


# ── Ledger writer ─────────────────────────────────────────────────────────────

def write_to_ledger(signals: list, run_id: str, conn: sqlite3.Connection) -> int:
    """Write BetSignals to ledger table. Returns number of bets written."""
    import hashlib as _hashlib
    written = 0
    # Disable FK enforcement so synthetic BF: match_ids (live screener) can be stored
    conn.execute("PRAGMA foreign_keys = OFF")
    for s in signals:
        if s.stake_gbp <= 0:
            continue
        bet_direction = f"{s.market_type}_{s.direction}"
        bet_id = _hashlib.sha256(
            f"{s.match_id}|{bet_direction}|{run_id}".encode()
        ).hexdigest()[:16]
        conn.execute(
            """INSERT OR IGNORE INTO ledger
               (bet_id, run_id, match_id, sport, bet_direction, line, odds_taken,
                stake_gbp, status, mode, placed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (bet_id, run_id, s.match_id, s.sport,
             bet_direction, s.line, s.odds, s.stake_gbp, "PENDING", s.mode)
        )
        written += 1
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    return written


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from src.model.simulate import simulate, elo_to_hold_probs, elo_to_p_match, p_match_to_p_set

    parser = argparse.ArgumentParser(description="Tennis edge screener")
    parser.add_argument("--date", default="2024-06-01", help="Match date YYYY-MM-DD")
    parser.add_argument("--bankroll", type=float, default=1000.0)
    parser.add_argument("--mode", choices=["PAPER", "LIVE"], default="PAPER")
    parser.add_argument("--demo", action="store_true",
                        help="Run a synthetic demo with known big ELO gap")
    args = parser.parse_args()

    if args.demo:
        print("=== EDGE.PY DEMO (synthetic) ===\n")

        # Simulate: Sinner (ELO ~1990) vs qualifier (ELO ~1550) on Hard, BO3
        signal_list = screen_tennis_match(
            match_id="DEMO-001",
            match_date="2024-01-15",
            p1_id="ATP-SINNER-J",
            p2_id="ATP-QUALIFIER-X",
            p1_elo_surface=1990.0,
            p2_elo_surface=1550.0,
            surface="Hard",
            best_of=3,
            tiebreak_rule="standard",
            p1_serve_str=78.0,
            p2_serve_str=58.0,
            p1_tier=1,
            p2_tier=3,
            p1_last_match="2024-01-10",
            p2_last_match="2024-01-08",
            p1_surface_n=45,
            p2_surface_n=4,
            market_lines={
                "total_games": {
                    "line": 21.5,
                    "over_odds": 2.10,
                    "under_odds": 1.75,
                    "liquidity": 3500,
                    "synthetic": False,
                },
                "first_set": {
                    "p1_odds": 1.30,
                    "p2_odds": 3.40,
                    "liquidity": 8000,
                    "synthetic": False,
                },
            },
            bankroll=args.bankroll,
            mode="PAPER",
        )

        for s in signal_list:
            if s.reject_reason:
                print(f"  REJECTED [{s.market_type}]: {s.reject_reason}")
            else:
                flag = ">>> BET" if s.stake_gbp > 0 else "  preview"
                print(f"{flag} [{s.market_type} {s.direction}]")
                print(f"  Line:      {s.line}")
                print(f"  Model p:   {s.model_p:.3f}  Market p: {s.market_p:.3f}")
                print(f"  Edge:      {s.edge:+.1%}")
                print(f"  Odds:      {s.odds}  Kelly: {s.kelly_frac:.3f}  Stake: £{s.stake_gbp:.2f}")
                print(f"  Tier:      T{s.tier}  Mode: {s.mode}  Synthetic: {s.synthetic_line}")
                print()
    else:
        signals = screen_from_db(args.date, args.bankroll, args.mode)
        print(f"[edge] {args.date}: {len(signals)} signals ({sum(1 for s in signals if s.stake_gbp>0)} bets)")
        for s in signals:
            if s.stake_gbp > 0:
                print(f"  BET {s.market_type} {s.direction}  edge={s.edge:+.1%}  £{s.stake_gbp:.2f}")
