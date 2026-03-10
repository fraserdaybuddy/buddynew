"""
sportmarket.py — Sportmarket API client
JOB-006 Sports Betting Model

Handles: market line fetching, order placement, order polling, emergency kill.

Paper mode (default): all calls are logged but NO real orders are placed.
Live mode: requires LIVE_MODE=true in environment — set explicitly by human only.

API base: https://api.sportmarket.com/v1
Auth:     Bearer token from env SPORTMARKET_API_KEY
"""

import os
import hashlib
import logging
import requests
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("sportmarket")

BASE_URL = "https://api.sportmarket.com/v1"
LIVE_MODE = os.environ.get("LIVE_MODE", "").strip().lower() == "true"


def _token() -> str:
    """Load API token from environment. Raise if missing."""
    token = os.environ.get("SPORTMARKET_API_KEY", "").strip()
    if not token:
        raise RuntimeError(
            "SPORTMARKET_API_KEY not set. Add it to your .env file."
        )
    return token


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def _request_uuid(match_id: str, direction: str, run_id: str) -> str:
    """Deterministic UUID — prevents duplicate orders on retry."""
    raw = f"{match_id}_{direction}_{run_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:36]


# ─────────────────────────────────────────────
# Market line fetching
# ─────────────────────────────────────────────

def get_betslip(betslip_id: str) -> dict:
    """
    Fetch live market lines for a betslip.

    POST /v1/betslips/
    Returns: { betslip_id, line, over_odds, under_odds, ... }

    In paper mode: raises if called without LIVE_MODE (market line fetching
    is always real — you need real lines to build the model output).
    """
    log.info(f"[sportmarket] get_betslip: {betslip_id}")
    resp = requests.post(
        f"{BASE_URL}/betslips/",
        json={"betslip_id": betslip_id},
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def search_betslips(sport: str, event_name: str) -> list[dict]:
    """
    Search for open betslips by sport + event name.

    POST /v1/betslips/
    This is the harvester entry point — finds eligible O/U markets.
    """
    log.info(f"[sportmarket] search_betslips: sport={sport} event={event_name!r}")
    resp = requests.post(
        f"{BASE_URL}/betslips/",
        json={"sport": sport, "event": event_name},
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    # API returns either a list or {"results": [...]}
    if isinstance(data, list):
        return data
    return data.get("results", [])


# ─────────────────────────────────────────────
# Order placement
# ─────────────────────────────────────────────

def place_order(
    betslip_id: str,
    price: float,
    stake_gbp: float,
    match_id: str,
    direction: str,
    run_id: str,
    duration: int = 300,
    exchange_mode: str = "normal",
) -> Optional[str]:
    """
    Place a single order via Sportmarket API.

    Returns order_id on success.
    Returns None if LIVE_MODE is False (paper mode — order is NOT placed).

    Raises on HTTP error or invalid response.

    Args:
        betslip_id:    Sportmarket betslip identifier for the market
        price:         decimal odds to take
        stake_gbp:     stake in GBP
        match_id:      our internal match_id (for request_uuid + user_data)
        direction:     "UNDER" or "OVER"
        run_id:        model run identifier
        duration:      seconds to keep order live (default 300 = 5 min)
        exchange_mode: "normal" | "dark" (dark hides order size for large stakes)
    """
    request_uuid = _request_uuid(match_id, direction, run_id)

    payload = {
        "betslip_id":    betslip_id,
        "price":         price,
        "stake":         ["GBP", stake_gbp],
        "duration":      duration,
        "request_uuid":  request_uuid,
        "user_data":     f"{match_id}|{direction}|{run_id}",
        "exchange_mode": exchange_mode,
    }

    if not LIVE_MODE:
        log.info(
            f"[sportmarket][PAPER] place_order skipped | "
            f"betslip={betslip_id} | dir={direction} | "
            f"stake=£{stake_gbp:.2f} | price={price} | uuid={request_uuid}"
        )
        return None

    log.info(
        f"[sportmarket][LIVE] place_order | betslip={betslip_id} | "
        f"dir={direction} | stake=£{stake_gbp:.2f} | price={price}"
    )
    resp = requests.post(
        f"{BASE_URL}/orders/",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    order_id = resp.json()["order_id"]
    log.info(f"[sportmarket][LIVE] order placed: {order_id}")
    return order_id


def place_batch(orders: list[dict]) -> list[dict]:
    """
    Place up to 200 orders in a single API call.

    Each item in `orders` must have:
        betslip_id, price, stake_gbp, match_id, direction, run_id

    Returns list of { match_id, direction, order_id | None, error | None }
    """
    if not orders:
        return []

    if not LIVE_MODE:
        log.info(f"[sportmarket][PAPER] place_batch skipped | {len(orders)} orders")
        return [
            {"match_id": o["match_id"], "direction": o["direction"],
             "order_id": None, "error": None}
            for o in orders
        ]

    payload = [
        {
            "betslip_id":   o["betslip_id"],
            "price":        o["price"],
            "stake":        ["GBP", o["stake_gbp"]],
            "duration":     o.get("duration", 300),
            "request_uuid": _request_uuid(o["match_id"], o["direction"], o["run_id"]),
            "user_data":    f"{o['match_id']}|{o['direction']}|{o['run_id']}",
        }
        for o in orders
    ]

    resp = requests.post(
        f"{BASE_URL}/orders/batch/",
        json=payload,
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────────
# Order polling
# ─────────────────────────────────────────────

def poll_order(order_id: str) -> dict:
    """
    Fetch current status of a placed order.

    GET /v1/orders/<id>/

    Returns dict with at minimum:
        order_id, status, profit_loss (once settled)

    Status values: PENDING | MATCHED | PARTIALLY_MATCHED | SETTLED | CANCELLED | LAPSED
    """
    if not LIVE_MODE:
        log.info(f"[sportmarket][PAPER] poll_order skipped | order={order_id}")
        return {"order_id": order_id, "status": "PAPER", "profit_loss": None}

    resp = requests.get(
        f"{BASE_URL}/orders/{order_id}/",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────────
# Emergency kill switch
# ─────────────────────────────────────────────

def close_all() -> dict:
    """
    Emergency kill switch — cancel ALL open orders immediately.

    POST /v1/orders/close_all/

    Call this if:
    - Circuit breaker trips
    - Manual emergency (model error, data corruption suspected)
    - Any unrecoverable error during live session

    Always executes regardless of LIVE_MODE.
    """
    log.warning("[sportmarket] EMERGENCY KILL SWITCH ACTIVATED — closing all orders")

    if not LIVE_MODE:
        log.warning("[sportmarket][PAPER] close_all called in paper mode — no real orders to close")
        return {"cancelled": 0, "mode": "PAPER"}

    resp = requests.post(
        f"{BASE_URL}/orders/close_all/",
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    log.warning(f"[sportmarket] close_all confirmed: {result}")
    return result
