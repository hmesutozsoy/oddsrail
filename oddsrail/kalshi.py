"""Kalshi venue: signed REST access for trading agents.

Auth is RSA-PSS(SHA256, MGF1-SHA256, salt=digest length) over the exact string
    str(unix_ms) + METHOD_UPPER + path
where `path` INCLUDES the /trade-api/v2 prefix and EXCLUDES the query string,
sent as KALSHI-ACCESS-KEY / -SIGNATURE / -TIMESTAMP headers.

Deliberately built on httpx rather than the official SDK: kalshi-python-sync
requires Python >=3.13 (this runs on 3.12), it re-releases weekly in lockstep
with the spec version, and the surface used here is small enough that a
pinned dependency costs more than it saves. Read endpoints need no key at all.

Two shapes on this API bite hard, so both are normalised here:

1. PRICES ARE DOLLAR STRINGS, NOT CENTS. Fields carry a `_dollars` suffix
   ("0.5600") and sizes an `_fp` suffix ("10.00"); the legacy integer-cent
   fields were removed in 2026-03. All arithmetic uses Decimal — float
   rounding on a 1-tick market is a real money bug.

2. THE ORDERBOOK IS BIDS-ONLY ON BOTH SIDES. `orderbook_fp.yes_dollars` and
   `.no_dollars` are both BID ladders, ascending, so the best bid is the LAST
   element. A NO bid at $0.99 IS a YES ask at $0.01. get_orderbook() converts
   this into a conventional best-first bid/ask view of the YES book; the raw
   ladders are returned alongside so nothing is hidden.

Compliance note: Kalshi's Developer Agreement limits API use to a member's
own trading and restricts storing/sharing API data. oddsrail is self-hosted
and single-tenant — the operator supplies their own key and trades their own
account — and this module caches nothing.
"""

from __future__ import annotations

import base64
import os
import time
from decimal import Decimal

import httpx

PROD = "https://external-api.kalshi.com"
DEMO = "https://external-api.demo.kalshi.co"
PREFIX = "/trade-api/v2"


def base_url() -> str:
    return DEMO if os.environ.get("KALSHI_DEMO", "").lower() in ("1", "true", "yes") else PROD


def dry_run() -> bool:
    return os.environ.get("ODDSRAIL_DRY_RUN", "1") not in ("0", "false", "no")


def has_credentials() -> bool:
    return bool(os.environ.get("KALSHI_KEY_ID")) and bool(
        os.environ.get("KALSHI_PRIVATE_KEY_PATH") or os.environ.get("KALSHI_PRIVATE_KEY"))


def _private_key():
    from cryptography.hazmat.primitives import serialization
    pem = os.environ.get("KALSHI_PRIVATE_KEY")
    if not pem:
        path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not path:
            raise RuntimeError(
                "no Kalshi key: set KALSHI_PRIVATE_KEY_PATH (PKCS#8 PEM) or "
                "KALSHI_PRIVATE_KEY. Read tools work without one.")
        with open(path, "rb") as fh:
            pem_bytes = fh.read()
    else:
        pem_bytes = pem.encode()
    return serialization.load_pem_private_key(pem_bytes, password=None)


def _signed_headers(method: str, path: str) -> dict:
    """path must start with /trade-api/v2 and carry no query string."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key_id = os.environ.get("KALSHI_KEY_ID")
    if not key_id:
        raise RuntimeError("KALSHI_KEY_ID not set")
    ts = str(int(time.time() * 1000))
    msg = (ts + method.upper() + path.split("?")[0].split("#")[0]).encode()
    sig = _private_key().sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("ascii"),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


async def _get(path: str, params: dict | None = None, signed: bool = False):
    url = base_url() + PREFIX + path
    headers = _signed_headers("GET", PREFIX + path) if signed else {}
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(url, params=params or {}, headers=headers)
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict):
    url = base_url() + PREFIX + path
    headers = _signed_headers("POST", PREFIX + path)
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(url, json=body, headers=headers)
        r.raise_for_status()
        return r.json()


def _d(v) -> Decimal | None:
    try:
        return Decimal(str(v))
    except Exception:
        return None


def slim_market(m: dict) -> dict:
    """Kalshi market -> the same vocabulary oddsrail uses for Polymarket."""
    return {
        "venue": "kalshi",
        "ticker": m.get("ticker"),
        "event_ticker": m.get("event_ticker"),
        "title": m.get("title"),
        "yes_sub_title": m.get("yes_sub_title"),
        "status": m.get("status"),
        "yes_bid": m.get("yes_bid_dollars"),
        "yes_ask": m.get("yes_ask_dollars"),
        "no_bid": m.get("no_bid_dollars"),
        "no_ask": m.get("no_ask_dollars"),
        "last_price": m.get("last_price_dollars"),
        "volume": m.get("volume_fp"),
        "volume_24h": m.get("volume_24h_fp"),
        "open_interest": m.get("open_interest_fp"),
        "close_time": m.get("close_time"),
        "rules_primary": (m.get("rules_primary") or "")[:400],
        "market_type": m.get("market_type"),
    }


async def search_markets(query: str = "", limit: int = 10,
                         status: str = "open", min_volume: float = 0.0):
    """Kalshi has no text-search endpoint, so this pages recent open markets
    and filters client-side on title/ticker. MVE combo shards are excluded —
    they are auto-generated, illiquid, and drown real markets otherwise."""
    out, cursor, pages = [], None, 0
    q = (query or "").lower()
    while len(out) < limit and pages < 5:
        params = {"limit": 200, "status": status, "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        data = await _get("/markets", params)
        batch = data.get("markets") or []
        for m in batch:
            if float(_d(m.get("volume_fp")) or 0) < min_volume:
                continue
            hay = f"{m.get('title','')} {m.get('ticker','')} {m.get('yes_sub_title','')}".lower()
            if q and q not in hay:
                continue
            out.append(m)
        cursor = data.get("cursor")
        pages += 1
        if not cursor or not batch:
            break
    out.sort(key=lambda m: -float(_d(m.get("volume_fp")) or 0))
    return [slim_market(m) for m in out[:limit]]


async def get_market(ticker: str):
    d = await _get(f"/markets/{ticker}")
    return slim_market(d.get("market") or {})


async def get_orderbook(ticker: str, depth: int = 10):
    """Normalise Kalshi's bids-only ladders into a YES-book bid/ask view.

    yes_dollars is the YES bid ladder; no_dollars is the NO bid ladder, and a
    NO bid at q is a YES ask at (1 - q). Both arrive ascending, so the best
    levels are at the END — everything below is returned best-first.
    """
    d = await _get(f"/markets/{ticker}/orderbook", {"depth": depth})
    ob = d.get("orderbook_fp") or {}
    yes_raw = ob.get("yes_dollars") or []
    no_raw = ob.get("no_dollars") or []

    yes_bids = [{"price": str(p), "size": str(s)} for p, s in reversed(yes_raw)]
    yes_asks = [{"price": str(Decimal("1") - Decimal(p)), "size": str(s)}
                for p, s in reversed(no_raw)]
    return {
        "venue": "kalshi",
        "ticker": ticker,
        "yes_bids": yes_bids[:depth],
        "yes_asks": yes_asks[:depth],
        "best_yes_bid": yes_bids[0]["price"] if yes_bids else None,
        "best_yes_ask": yes_asks[0]["price"] if yes_asks else None,
        "note": ("Kalshi publishes bid ladders only; yes_asks are derived as "
                 "1 - (NO bid). Prices are dollar strings, not cents."),
        "raw_ladders": {"yes_bids": yes_raw, "no_bids": no_raw},
    }


async def get_trades(ticker: str, limit: int = 50):
    d = await _get("/markets/trades", {"ticker": ticker, "limit": limit})
    return d.get("trades") or []


async def get_balance():
    if not has_credentials():
        return {"note": "no Kalshi credentials configured"}
    return await _get("/portfolio/balance", signed=True)


async def get_positions(limit: int = 50):
    if not has_credentials():
        return {"note": "no Kalshi credentials configured"}
    return await _get("/portfolio/positions", {"limit": limit}, signed=True)


async def open_orders(limit: int = 50):
    if not has_credentials():
        return {"note": "no Kalshi credentials configured"}
    return await _get("/portfolio/orders", {"limit": limit, "status": "resting"},
                      signed=True)


def _to_yes_book(outcome: str, action: str, price: Decimal):
    """Translate (outcome, action, price-of-that-outcome) to the YES book.

    Kalshi V2 quotes everything from the YES leg as bid/ask — there is no
    yes/no side and no buy/sell action — so this is where an inverted-position
    bug would live if it were done implicitly at the call site.

        buy  YES @ p -> bid @ p          sell YES @ p -> ask @ p
        buy  NO  @ q -> ask @ (1 - q)    sell NO  @ q -> bid @ (1 - q)
    """
    outcome, action = outcome.lower(), action.lower()
    if outcome not in ("yes", "no"):
        raise ValueError("outcome must be 'yes' or 'no'")
    if action not in ("buy", "sell"):
        raise ValueError("action must be 'buy' or 'sell'")
    if outcome == "yes":
        return ("bid" if action == "buy" else "ask"), price
    return ("ask" if action == "buy" else "bid"), (Decimal("1") - price)


async def place_order(ticker: str, outcome: str, action: str, price: float,
                      count: float, time_in_force: str = "good_till_cancelled",
                      client_order_id: str | None = None):
    """Limit order. `price` is the probability of `outcome`, in (0,1)."""
    p = Decimal(str(price))
    if not (Decimal("0") < p < Decimal("1")):
        raise ValueError("price is a probability in (0, 1)")
    if count <= 0:
        raise ValueError("count must be positive")

    side, yes_price = _to_yes_book(outcome, action, p)
    body = {
        "ticker": ticker,
        "side": side,
        "price": f"{yes_price:.4f}",
        "count": f"{Decimal(str(count)):.2f}",
        "time_in_force": time_in_force,
        "self_trade_prevention_type": "cancel_resting",
    }
    if client_order_id:
        body["client_order_id"] = client_order_id

    intent = {
        "venue": "kalshi",
        "requested": {"ticker": ticker, "outcome": outcome, "action": action,
                      "price": str(p), "count": count},
        "translated_to_yes_book": body,
        "attribution": ("none — Kalshi has no builder-code field on REST; "
                        "its builder program is a Solana/DFlow integration"),
    }
    if dry_run():
        return {"dry_run": True, "would_post": intent,
                "note": "set ODDSRAIL_DRY_RUN=0 to post real orders"}
    if not has_credentials():
        raise RuntimeError("Kalshi credentials required to place real orders")
    resp = await _post("/portfolio/events/orders", body)
    return {"dry_run": False, "posted": intent, "response": resp}


async def cancel_order(order_id: str):
    if dry_run():
        return {"dry_run": True, "would_cancel": order_id}
    url = base_url() + PREFIX + f"/portfolio/events/orders/{order_id}"
    headers = _signed_headers("DELETE", PREFIX + f"/portfolio/events/orders/{order_id}")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.delete(url, headers=headers)
        r.raise_for_status()
        return r.json()
