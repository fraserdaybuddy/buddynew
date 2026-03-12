"""
governor.py — Stake calculator + circuit breaker + paper/live switch
JOB-006 Sports Betting Model

Owns:
  - Kelly fraction stake sizing
  - Per-session circuit breaker (consecutive losses + drawdown limit)
  - LIVE_MODE gate (env var must be explicitly true)
  - Stake flooring / capping
"""

import os
import logging
from dataclasses import dataclass, field

log = logging.getLogger("governor")

# ─────────────────────────────────────────────
# Constants — override via env
# ─────────────────────────────────────────────

LIVE_MODE          = os.environ.get("LIVE_MODE", "").strip().lower() == "true"
KELLY_FRACTION     = float(os.environ.get("KELLY_FRACTION",    "1.0"))   # full Kelly
MIN_STAKE_GBP      = float(os.environ.get("MIN_STAKE_GBP",     "5.0"))
MAX_STAKE_GBP      = float(os.environ.get("MAX_STAKE_GBP",     "500.0")) # absolute £ ceiling
MAX_CONSEC_LOSSES  = int(  os.environ.get("MAX_CONSEC_LOSSES", "5"))     # halt after N in a row
MAX_DRAWDOWN_PCT   = float(os.environ.get("MAX_DRAWDOWN_PCT",  "0.20"))  # 20% of bankroll


# ─────────────────────────────────────────────
# Tiered bankroll cap — derived from simulation
# Replaces the old flat 5% cap. Caps grow with edge because at higher edges
# the Kelly stake is proportionally more justified. Source: run_simulations.py
# ─────────────────────────────────────────────

def tiered_cap(edge: float) -> float:
    """
    Return the maximum bankroll fraction for a given edge (decimal, e.g. 0.08).
    Derived from Monte Carlo simulations optimising median growth subject to
    ruin probability < 5% with ±5% model noise.

    Edge       Full Kelly    Cap
    5–9%       3–6%          8%
    10–14%     6–10%         10%
    15–19%     9–14%         12%
    20–24%     12–17%        16%
    25–29%     15–22%        20%
    30%+       20–25%        22%
    """
    if edge < 0.10:  return 0.08
    if edge < 0.15:  return 0.10
    if edge < 0.20:  return 0.12
    if edge < 0.25:  return 0.16
    if edge < 0.30:  return 0.20
    return 0.22


# ─────────────────────────────────────────────
# Stake sizing
# ─────────────────────────────────────────────

def kelly_stake(
    bankroll_gbp: float,
    edge: float,
    odds: float,
    fraction: float = KELLY_FRACTION,
    min_stake: float = MIN_STAKE_GBP,
    max_stake: float = MAX_STAKE_GBP,
) -> float:
    """
    Calculate Kelly criterion stake using the proper Kelly formula, with
    tiered bankroll cap and absolute £ ceiling.

    Kelly formula:
        p = (1 / odds) * (1 + edge)       — model probability derived from market
        q = 1 - p
        full_kelly = (b * p - q) / b      — standard Kelly fraction
        where b = decimal_odds - 1

    Cap applied (whichever is smaller):
        1. tiered_cap(edge)  — bankroll % cap that grows with edge (e.g. 8–22%)
        2. max_stake         — absolute £ ceiling (default £500)

    Args:
        bankroll_gbp: current bankroll in GBP
        edge:         model edge as decimal e.g. 0.08 = 8%
        odds:         decimal odds e.g. 1.85
        fraction:     Kelly multiplier (1.0 = full Kelly, default)

    Returns:
        Stake in GBP, rounded to nearest £1, floored at min_stake.
        Returns 0.0 if edge <= 0 or full_kelly <= 0.
    """
    if edge <= 0:
        return 0.0

    b = odds - 1.0
    if b <= 0:
        return 0.0

    # Proper Kelly: derive p from market implied probability scaled by edge
    p = (1.0 / odds) * (1.0 + edge)
    q = 1.0 - p
    full_kelly = (b * p - q) / b

    if full_kelly <= 0:
        return 0.0

    # Apply fraction multiplier (tier_mult × elo_confidence from edge.py)
    adjusted_kelly = full_kelly * fraction

    # Apply tiered bankroll cap (smaller of: cap% of bank, or £max_stake)
    cap_frac  = tiered_cap(edge)
    capped_frac = min(adjusted_kelly, cap_frac)
    raw_stake = bankroll_gbp * capped_frac

    if raw_stake <= 0:
        return 0.0

    return float(round(max(min_stake, min(max_stake, raw_stake))))


def half_stake(stake_gbp: float, min_stake: float = MIN_STAKE_GBP) -> float:
    """Return half stake, floored at min_stake."""
    return float(max(min_stake, round(stake_gbp / 2)))


# ─────────────────────────────────────────────
# Circuit breaker
# ─────────────────────────────────────────────

@dataclass
class CircuitBreaker:
    """
    Tracks session P&L and consecutive losses.
    Trips (halts all new bets) if limits are breached.

    Reset between sessions — do not persist across days.
    """
    bankroll_start:    float
    max_consec_losses: int   = MAX_CONSEC_LOSSES
    max_drawdown_pct:  float = MAX_DRAWDOWN_PCT

    _consec_losses: int   = field(default=0,     init=False)
    _session_pnl:   float = field(default=0.0,   init=False)
    _tripped:       bool  = field(default=False, init=False)
    _trip_reason:   str   = field(default="",    init=False)

    def record(self, profit_loss_gbp: float) -> None:
        """Record a settled bet P&L. May trip the breaker."""
        if self._tripped:
            return

        self._session_pnl += profit_loss_gbp

        if profit_loss_gbp < 0:
            self._consec_losses += 1
        else:
            self._consec_losses = 0

        drawdown = abs(self._session_pnl) / self.bankroll_start if self._session_pnl < 0 else 0.0

        if self._consec_losses >= self.max_consec_losses:
            self._trip(f"{self._consec_losses} consecutive losses (limit: {self.max_consec_losses})")
        elif drawdown >= self.max_drawdown_pct:
            self._trip(f"Drawdown {drawdown:.1%} (limit: {self.max_drawdown_pct:.0%})")

    def _trip(self, reason: str) -> None:
        self._tripped = True
        self._trip_reason = reason
        log.error(f"[governor] CIRCUIT BREAKER TRIPPED: {reason}")

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def trip_reason(self) -> str:
        return self._trip_reason

    @property
    def session_pnl(self) -> float:
        return self._session_pnl

    def status(self) -> dict:
        return {
            "tripped":       self._tripped,
            "trip_reason":   self._trip_reason,
            "session_pnl":   round(self._session_pnl, 2),
            "consec_losses": self._consec_losses,
            "drawdown_pct":  round(
                abs(self._session_pnl) / self.bankroll_start
                if self._session_pnl < 0 else 0.0, 4
            ),
        }


# ─────────────────────────────────────────────
# Live mode gate
# ─────────────────────────────────────────────

def assert_live_mode_safe() -> None:
    """Raise if LIVE_MODE is not explicitly true. Call before any real order placement."""
    if not LIVE_MODE:
        raise RuntimeError(
            "LIVE_MODE is not set. "
            "Set LIVE_MODE=true in your environment to enable real order placement."
        )


def get_mode() -> str:
    return "LIVE" if LIVE_MODE else "PAPER"
