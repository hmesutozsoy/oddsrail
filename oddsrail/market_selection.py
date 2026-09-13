"""Resolve explicit Polymarket selections without fetching user-supplied URLs.

An event is a collection of selectable markets, never an implicit choice of
one outcome. Trading configuration stores exact decimal outcome token IDs.
"""

from __future__ import annotations

import asyncio
import math
import re
from urllib.parse import urlsplit

from . import polymarket as pm

MAX_MARKET_IDS = 20
_HOSTS = {"polymarket.com", "www.polymarket.com"}
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z")
_TOKEN = re.compile(r"[1-9][0-9]{0,77}\Z")


class SelectionError(ValueError):
    def __init__(self, message: str, code: str = "invalid_input"):
        super().__init__(message)
        self.code = code


def valid_token_id(value: object) -> bool:
    return (isinstance(value, str) and bool(_TOKEN.fullmatch(value))
            and int(value) < 2**256)


def validate_market_ids(values: object, *, allow_empty: bool = False) -> list[str]:
    """Reject malformed selections in full; never drop invalid IDs silently."""
    if not isinstance(values, list) or len(values) > MAX_MARKET_IDS:
        raise SelectionError(f"Select at most {MAX_MARKET_IDS} outcome token IDs.")
    if any(not valid_token_id(value) for value in values):
        raise SelectionError("Every selected outcome must have an exact decimal token ID.")
    ids = list(dict.fromkeys(values))
    if not ids and not allow_empty:
        raise SelectionError("Choose at least one specific market outcome.")
    return ids


def parse_polymarket_url(value: object) -> dict:
    """Accept only known Polymarket page routes, not arbitrary API URLs."""
    if not isinstance(value, str) or len(value) > 2048:
        raise SelectionError("Paste a Polymarket event or market URL.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or "\\" in value:
        raise SelectionError("The Polymarket URL contains invalid characters.")
    value = value.strip()
    if value.startswith(("polymarket.com/", "www.polymarket.com/")):
        value = "https://" + value
    try:
        url = urlsplit(value)
        host = url.hostname
    except ValueError as exc:
        raise SelectionError("The Polymarket URL is malformed.") from exc
    if (url.scheme != "https" or host not in _HOSTS
            or url.netloc.lower() != host):
        raise SelectionError("Use an HTTPS polymarket.com URL without a login or port.")
    parts = url.path.rstrip("/").split("/")[1:]
    if (len(parts) not in (2, 3) or parts[0] not in ("event", "market")
            or (parts[0] == "market" and len(parts) != 2)
            or any(not _SLUG.fullmatch(part) for part in parts[1:])):
        raise SelectionError("Use /event/EVENT, /event/EVENT/MARKET, or /market/MARKET.")
    return {"kind": parts[0], "slug": parts[1],
            "market_slug": parts[2] if len(parts) == 3 else None}


def _slim(market: dict) -> dict:
    return pm.slim_market(market) if any(k in market for k in ("state", "metrics", "prices")) else market


def _choice(market: dict, event_slug: str | None = None) -> dict | None:
    market = _slim(market)
    slug = market.get("slug")
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        return None
    outcomes = []
    for side in ("yes", "no"):
        leg = (market.get("outcomes") or {}).get(side) or {}
        if valid_token_id(leg.get("token_id")):
            outcomes.append({"label": leg.get("label") or side.upper(),
                             "token_id": leg["token_id"], "price": leg.get("price"),
                             "side": side})
    if not outcomes:
        return None
    path = f"/event/{event_slug}/{slug}" if event_slug else f"/market/{slug}"
    return {"title": market.get("question") or slug, "slug": slug,
            "url": "https://polymarket.com" + path,
            "condition_id": market.get("condition_id"), "outcomes": outcomes,
            "closed": bool(market.get("closed")),
            "accepting_orders": bool(market.get("accepting_orders"))}


async def _browse_choices() -> list[dict]:
    """Show a liquid cross-section, with at most one market per event.

    Keep the venue's event references before slimming so a large multi-market
    event cannot occupy the landing page. The candidate pool is bounded to one
    page, and only markets currently accepting orders are offered for browsing.
    """
    client = await pm.public()
    page = await client.list_markets(closed=False, order="volume24hr", ascending=False,
                                     page_size=100).first_page()
    markets = [m for m in pm.dump(list(page.items)) if isinstance(m, dict)]

    def volume(market):
        try:
            value = float(_slim(market).get("volume_24hr") or 0)
            return value if math.isfinite(value) else 0
        except (TypeError, ValueError):
            return 0

    markets.sort(key=volume, reverse=True)
    choices, seen_events, seen_markets = [], set(), set()
    for market in markets:
        events = [event for event in market.get("events") or [] if isinstance(event, dict)]
        event_keys = {str(event.get("id") or event.get("slug")) for event in events
                      if event.get("id") or event.get("slug")}
        event_slug = next((event["slug"] for event in events
                           if isinstance(event.get("slug"), str)
                           and _SLUG.fullmatch(event["slug"])), None)
        choice = _choice(market, event_slug)
        if not choice or choice["closed"] or not choice["accepting_orders"]:
            continue
        market_key = choice["condition_id"] or choice["slug"]
        if market_key in seen_markets or event_keys & seen_events:
            continue
        choices.append(choice)
        seen_markets.add(market_key)
        seen_events.update(event_keys)
        if len(choices) == 20:
            break
    return choices


async def search_selection(query: object) -> dict:
    if not isinstance(query, str) or len(query) > 160:
        raise SelectionError("Enter a market search of at most 160 characters.")
    query = query.strip()
    try:
        if query:
            markets = await pm.search_markets(query=query, limit=20)
            choices = [choice for m in markets if isinstance(m, dict) and (choice := _choice(m))]
        else:
            choices = await _browse_choices()
    except Exception as exc:
        raise SelectionError("Polymarket search is unavailable; try again.", "upstream_unavailable") from exc
    return {"ok": True, "source": "search", "query": query, "markets": choices}


async def resolve_selection(value: object) -> dict:
    parsed = parse_polymarket_url(value)
    try:
        if parsed["kind"] == "market":
            raw = await pm.get_market(parsed["slug"])
            markets = [raw] if raw else []
            event_slug = None
        else:
            client = await pm.public()
            event = pm.dump(await client.get_event(slug=parsed["slug"]))
            markets = (event or {}).get("markets") or []
            event_slug = parsed["slug"]
    except Exception as exc:
        raise SelectionError("Polymarket could not resolve that URL; check it and try again.",
                             "upstream_unavailable") from exc
    expected_slug = parsed["market_slug"] or (parsed["slug"] if not event_slug else None)
    if expected_slug:
        markets = [m for m in markets if isinstance(m, dict) and m.get("slug") == expected_slug]
    choices = [choice for m in markets if isinstance(m, dict) and (choice := _choice(m, event_slug))]
    if not choices:
        raise SelectionError("No selectable market was found at that Polymarket URL.", "not_found")
    return {"ok": True, "source": parsed["kind"], "markets": choices,
            "requires_selection": True}


async def selected_markets(token_ids: object) -> list[dict]:
    """Resolve the complete explicit selection, validating API token membership."""
    ids = validate_market_ids(token_ids)
    results = await asyncio.gather(*(pm.get_market_by_token(tid) for tid in ids),
                                   return_exceptions=True)
    markets, seen = [], set()
    for tid, result in zip(ids, results):
        if isinstance(result, BaseException):
            raise SelectionError("A selected market could not be loaded; no new entries were made.",
                                 "upstream_unavailable")
        market = _slim(result) if isinstance(result, dict) else {}
        outcomes = market.get("outcomes") or {}
        tokens = [(outcomes.get(side) or {}).get("token_id") for side in ("yes", "no")]
        if tid not in tokens or any(not valid_token_id(token) for token in tokens) or tokens[0] == tokens[1]:
            raise SelectionError(f"Selected token {tid} does not resolve to that outcome.", "not_found")
        key = tuple(tokens)
        if key not in seen:
            seen.add(key)
            markets.append(market)
    return markets
