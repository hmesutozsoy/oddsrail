"""Bounded, public Polymarket account reads; never an authentication service.

The Gamma public-profile lookup accepts a user or account wallet and returns
``proxyWallet``. That documented association is the only account discovery used
here: a missing mapping is unknown, not an empty owner-wallet portfolio. It does
not prove ownership or discover every account associated with a signer.

Current API references (checked 2026-09-12):
https://docs.polymarket.com/api-reference/profiles/get-public-profile-by-wallet-address
https://docs.polymarket.com/api-reference/wallet/list-positions-for-a-user-or-market
https://docs.polymarket.com/api-reference/wallet/get-portfolio-value
https://docs.polymarket.com/api-reference/feeds/list-account-activity

V2 value includes single-market marks and unresolved combo cost, but excludes
cash. The authenticated CLOB balance API is deliberately not used. Cash and
cash-inclusive portfolio value therefore remain unknown. Never derive either
from activity, partial position pages, an operator account, or a native USDC
balance. No client keys, signing, account ledger or environment credentials.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timezone

import httpx

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
PAGE_SIZE = 100
MAX_PAGES = 2
MAX_BODY_BYTES = 1_000_000
REQUEST_SECONDS = 6
SECTION_SECONDS = 12
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
TOKEN_RE = re.compile(r"[0-9]{1,78}")
SLUG_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,299}")


class InvalidAddress(ValueError):
    pass


def normalize_address(address: str) -> str:
    if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address) or int(address[2:], 16) == 0:
        raise InvalidAddress("Provide one 0x wallet address with 40 hexadecimal characters.")
    return address.lower()


def _number(value, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        n = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(n) or abs(n) > 1e15 or (nonnegative and n < 0):
        return None
    return n


def _text(value, limit=500):
    return value[:limit] if isinstance(value, str) else ""


def _identifier(value, pattern):
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _common(row):
    slug = row.get("event_slug") or row.get("slug")
    return {
        "title": _text(row.get("title")), "outcome": _text(row.get("outcome"), 120),
        "url": "https://polymarket.com/event/" + slug if isinstance(slug, str) and SLUG_RE.fullmatch(slug) else None,
        "token_id": _identifier(row.get("token_id"), TOKEN_RE),
        "condition_id": _identifier(row.get("condition_id"), HASH_RE),
    }


def _position(row):
    item = _common(row)
    size = _number(row.get("current_size"), nonnegative=True)
    if not item["token_id"] or size is None or row.get("status") not in ("OPEN", "REDEEMABLE"):
        raise ValueError("Invalid position")
    item.update({
        "size": size,
        "avg_price": _number(row.get("avg_price"), nonnegative=True),
        "current_price": _number(row.get("current_price"), nonnegative=True),
        "current_value_usd": _number(row.get("current_value"), nonnegative=True),
        "cost_basis_usd": _number(row.get("total_cost_usdc"), nonnegative=True),
        "total_pnl_usd": _number(row.get("total_pnl")),
        "unrealized_pnl_usd": _number(row.get("unrealized_pnl")),
        "realized_pnl_usd": _number(row.get("realized_pnl")),
        "percent_pnl": _number(row.get("percent_pnl")),
        "redeemable": row.get("redeemable") is True,
    })
    return item


def _activity(row):
    item = _common(row)
    timestamp = _number(row.get("timestamp"), nonnegative=True)
    activity_type = _text(row.get("type"), 50)
    if timestamp is None or timestamp > 4_102_444_800 or not timestamp.is_integer() or not activity_type:
        raise ValueError("Invalid activity")
    item.update({
        "timestamp": int(timestamp), "type": activity_type,
        "side": _text(row.get("side"), 20), "size": _number(row.get("size"), nonnegative=True),
        "usdc_size": _number(row.get("usdc_size"), nonnegative=True),
        "price": _number(row.get("price"), nonnegative=True),
        "transaction_hash": _identifier(row.get("transaction_hash"), HASH_RE),
        "is_combo": row.get("is_combo") is True,
    })
    return item


def _section(status="unresolved", message=None):
    return {"status": status, "items": [], "truncated": False,
            "limit": PAGE_SIZE * MAX_PAGES, "message": message}


async def _json(client, origin, path, params):
    # Only the fixed calls in this module select origin/path. Neither query
    # values nor upstream cursors can become URLs. Redirects are never followed.
    async with asyncio.timeout(REQUEST_SECONDS):
        async with client.stream("GET", origin + path, params=params, follow_redirects=False) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    raise ValueError("Public response exceeds size bound")
    def invalid_constant(value):
        raise ValueError("Non-finite JSON number")
    return json.loads(body, parse_constant=invalid_constant)


async def _pages(client, address, path, params, mapper):
    result = _section("ok")
    cursor = None
    seen_cursors = set()
    seen_tokens = set()
    incomplete = False
    try:
        async with asyncio.timeout(SECTION_SECONDS):
            for page_index in range(MAX_PAGES):
                query = {"user": address, "limit": PAGE_SIZE, **params}
                if cursor:
                    query["cursor"] = cursor
                raw = await _json(client, DATA, path, query)
                if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
                    raise ValueError("Invalid page")
                rows, pagination = raw["data"], raw.get("pagination")
                if len(rows) > PAGE_SIZE or not isinstance(pagination, dict) or type(pagination.get("has_more")) is not bool:
                    raise ValueError("Unbounded or invalid page")
                for row in rows:
                    try:
                        if not isinstance(row, dict) or normalize_address(row.get("proxy_wallet")) != address:
                            raise ValueError("Wrong account in public response")
                        item = mapper(row)
                        if mapper is _position:
                            if item["token_id"] in seen_tokens:
                                raise ValueError("Repeated position")
                            seen_tokens.add(item["token_id"])
                        result["items"].append(item)
                    except (ValueError, TypeError):
                        incomplete = True
                has_more = pagination["has_more"]
                cursor = pagination.get("next_cursor")
                if not has_more:
                    if cursor not in (None, ""):
                        raise ValueError("Inconsistent page cursor")
                    break
                if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in seen_cursors or not rows:
                    raise ValueError("Invalid pagination cursor")
                seen_cursors.add(cursor)
                if page_index == MAX_PAGES - 1:
                    incomplete = True
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
        incomplete = True
        result["status"] = "partial" if result["items"] else "unavailable"
        result["message"] = "Some public data could not be loaded."
    if incomplete:
        if result["status"] == "ok":
            result["status"] = "partial"
        result["truncated"] = True
        result["message"] = result["message"] or "Showing a bounded or incomplete set of public records."
    return result


async def _holdings_value(client, address):
    try:
        raw = await _json(client, DATA, "/v2/value", {"user": address})
        row = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(row, dict) or normalize_address(row.get("proxy_wallet")) != address:
            return None
        return _number(row.get("value"), nonnegative=True)
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
        return None


async def load_portfolio(address: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Read public account data. ``transport`` is an offline test seam only.

    Does not consume bearer tokens or prove wallet ownership. Per-section errors
    preserve other successful reads. The time is retrieval time, not a venue
    freshness guarantee. Positions are single-market tokens; aggregate holdings
    also includes unresolved combos at cost. History includes deposits and
    withdrawals but uses the venue's default activity types (which exclude TIP).
    """
    address = normalize_address(address)
    result = {
        "ok": True, "address": address, "read_only": True, "authenticated": False,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "account": {"status": "unresolved", "connected_address": address,
                    "trading_address": None, "source": None, "scope": "public_profile_wallet",
                    "message": "No public profile wallet could be resolved for this address."},
        "summary": {"cash_usd": None, "holdings_value_usd": None, "portfolio_value_usd": None,
                    "holdings_status": "unresolved", "cash_status": "unavailable",
                    "valuation_scope": "single_market_marks_plus_unresolved_combo_cost"},
        "positions": _section(), "history": _section(),
    }
    async with httpx.AsyncClient(timeout=REQUEST_SECONDS, trust_env=False, follow_redirects=False,
                                 transport=transport, headers={"Accept": "application/json"}) as client:
        try:
            profile = await _json(client, GAMMA, "/public-profile", {"address": address})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return result
            profile = None
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
            profile = None
        if profile is None:
            result["account"].update(status="error", message="Polymarket account discovery is temporarily unavailable.")
            result["summary"]["holdings_status"] = "unavailable"
            result["positions"] = _section("unavailable")
            result["history"] = _section("unavailable")
            return result
        try:
            trading_address = normalize_address(profile.get("proxyWallet") if isinstance(profile, dict) else None)
        except InvalidAddress:
            return result
        result["account"].update(status="resolved", trading_address=trading_address,
                                 source="polymarket_public_profile",
                                 message="Public profile wallet only; other linked accounts are not discovered. Wallet ownership is not verified.")
        positions, history, value = await asyncio.gather(
            _pages(client, trading_address, "/v2/positions",
                   {"status": "OPEN", "filter_type": "TOKENS", "filter_amount": 0,
                    "include_archived": "true"}, _position),
            _pages(client, trading_address, "/v2/activity",
                   {"start": 1, "sort_direction": "DESC", "exclude_deposits_withdrawals": "false"}, _activity),
            _holdings_value(client, trading_address),
        )
    result["positions"], result["history"] = positions, history
    result["positions"]["scope"] = "single_market_positions"
    result["history"]["scope"] = "recent_default_activity_types_including_deposits_withdrawals"
    result["summary"].update(holdings_value_usd=value, holdings_status="ok" if value is not None else "unavailable")
    return result
