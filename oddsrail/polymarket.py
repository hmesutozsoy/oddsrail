"""Polymarket data access via the official unified SDK (polymarket-client).

Read paths need no keys (AsyncPublicClient). Trading lives in trading.py.

Field mappings below were verified against the live API (2026-08-23), not
guessed from docs: `outcomes` is a DICT keyed yes/no (not a list), search
returns {events, tags, profiles} with markets nested inside each event, and
book/volume/resolution data live in the prices/metrics/state/resolution
sub-objects rather than at the top level.
"""

import httpx

CLOB = "https://clob.polymarket.com"

_public = None


async def public():
    global _public
    if _public is None:
        from polymarket import AsyncPublicClient
        _public = AsyncPublicClient()
    return _public


def dump(obj):
    """Pydantic model / tuple / list -> plain JSON-able structure."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, (list, tuple)):
        return [dump(x) for x in obj]
    if isinstance(obj, dict):
        return {k: dump(v) for k, v in obj.items()}
    return obj


def slim_market(m: dict) -> dict:
    """Reduce a dumped Market model to what an agent needs to decide."""
    outs = m.get("outcomes") or {}
    prices = m.get("prices") or {}
    metrics = m.get("metrics") or {}
    state = m.get("state") or {}
    res = m.get("resolution") or {}

    def leg(key):
        o = outs.get(key) or {}
        return {"label": o.get("label"), "token_id": o.get("token_id"),
                "price": o.get("price")}

    return {
        "id": m.get("id"),
        "slug": m.get("slug"),
        "question": m.get("question"),
        "condition_id": m.get("condition_id"),
        "outcomes": {"yes": leg("yes"), "no": leg("no")} if outs else None,
        "best_bid": prices.get("best_bid"),
        "best_ask": prices.get("best_ask"),
        "spread": prices.get("spread"),
        "last_trade_price": prices.get("last_trade_price"),
        "volume": metrics.get("volume"),
        "volume_24hr": metrics.get("volume_24hr"),
        "liquidity": metrics.get("liquidity"),
        "active": state.get("active"),
        "closed": state.get("closed"),
        "accepting_orders": state.get("accepting_orders"),
        "neg_risk": state.get("neg_risk"),
        "end_date": state.get("end_date"),
        "uma_resolution_status": res.get("uma_resolution_status"),
        "resolution_source": res.get("source"),
    }


def _markets_from_search(results) -> list:
    """search() yields {events, tags, profiles}; the markets hang off events."""
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        for ev in r.get("events") or []:
            for mk in ev.get("markets") or []:
                if isinstance(mk, dict):
                    out.append(mk)
    return out


async def search_markets(query: str = "", limit: int = 10):
    c = await public()
    if query:
        page = await c.search(q=query, sort="volume_24hr",
                              page_size=max(limit, 5)).first_page()
        found = _markets_from_search(dump(list(page.items)))
        # open markets first, then by 24h volume
        def vol(m):
            try:
                return float(((m.get("metrics") or {}).get("volume_24hr")) or 0)
            except (TypeError, ValueError):
                return 0.0
        found.sort(key=lambda m: (bool((m.get("state") or {}).get("closed")), -vol(m)))
        return [slim_market(m) for m in found[:limit]]

    page = await c.list_markets(closed=False, page_size=limit).first_page()
    return [slim_market(m) for m in dump(list(page.items))]


async def get_market(id_or_slug: str, full: bool = False):
    c = await public()
    try:
        m = await c.get_market(slug=id_or_slug)
    except Exception:
        m = await c.get_market(id=id_or_slug)
    d = dump(m)
    return d if full else slim_market(d)


async def get_orderbook(token_id: str):
    c = await public()
    return dump(await c.get_order_book(token_id=token_id))


async def price_history(token_id: str, hours: float = 6.0,
                        fidelity_minutes: int = 1):
    """Returns (times, prices) oldest->newest."""
    c = await public()
    interval = "1h" if hours <= 1 else "6h" if hours <= 6 else \
               "1d" if hours <= 24 else "1w"
    pts = dump(await c.get_price_history(token_id=token_id, interval=interval,
                                         fidelity=fidelity_minutes))
    times, prices = [], []
    for p in pts:
        t = p.get("t", p.get("timestamp"))
        v = p.get("p", p.get("price"))
        if t is not None and v is not None:
            times.append(float(t))
            prices.append(float(v))
    return times, prices


async def get_positions(address: str, limit: int = 25):
    c = await public()
    page = await c.list_positions(user=address, page_size=limit).first_page()
    return dump(list(page.items))


# The SDK accepts only these; agents will say "weekly"/"7d"/"week", so map.
_PERIODS = {"day": "DAY", "daily": "DAY", "1d": "DAY", "24h": "DAY",
            "week": "WEEK", "weekly": "WEEK", "7d": "WEEK",
            "month": "MONTH", "monthly": "MONTH", "30d": "MONTH",
            "all": "ALL", "alltime": "ALL", "all_time": "ALL"}


def normalize_period(p: str) -> str:
    return _PERIODS.get(str(p or "").strip().lower().replace("-", "_"), "WEEK")


async def builder_leaderboard(time_period: str = "WEEK", limit: int = 25):
    c = await public()
    period = normalize_period(time_period)
    page = await c.list_builder_leaderboard(
        time_period=period, page_size=limit).first_page()
    return dump(list(page.items))


async def builder_trades(builder_code: str):
    """Public attribution check: matched trades carrying this builder code."""
    async with httpx.AsyncClient(timeout=15.0) as h:
        r = await h.get(f"{CLOB}/builder/trades",
                        params={"builder_code": builder_code})
        r.raise_for_status()
        return r.json()
