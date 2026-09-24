"""Six things the prediction-market APIs get wrong, shown live, no keys.

    pip install oddsrail && python examples/footguns.py

Each section hits the public venue API raw, shows the trap, then shows the
oddsrail call that returns the right answer. Everything here is read-only
and needs no credentials. Three further footguns need keys to demonstrate
(rejections modelled as return values, the undocumented $1 minimum notional,
and the "account trades" endpoint that returns the public tape); they are
documented in the README and encoded in the trading tools.

Runtime: about ten seconds. Network-dependent; a section that cannot reach
its venue says so and the rest carry on.
"""

import asyncio
import json

import httpx

from oddsrail import crossvenue as xv
from oddsrail import geo
from oddsrail import kalshi as kx
from oddsrail import polymarket as pm

RAW, OK, SKIP = "  ✗ raw     ", "  ✓ oddsrail", "  – skipped "


def head(n, title):
    print(f"\n{n}. {title}")


async def one_book_arrives_worst_first():
    head(1, "Polymarket's order book arrives worst-first")
    mk = next((m for m in await pm.search_markets("bitcoin", 6)
               if m.get("accepting_orders") and (m.get("outcomes") or {}).get("yes", {}).get("token_id")), None)
    if not mk:
        print(SKIP, "no open market found"); return
    tok = mk["outcomes"]["yes"]["token_id"]
    async with httpx.AsyncClient(timeout=15) as h:
        raw = (await h.get(f"{pm.CLOB}/book", params={"token_id": tok})).json()
    bids = [float(b["price"]) for b in raw.get("bids", [])]
    asks = [float(a["price"]) for a in raw.get("asks", [])]
    if not bids or not asks:
        print(SKIP, "empty book right now"); return
    print(f'     market: {mk["question"]!r}')
    print(RAW, f"bids[0] = {bids[0]:.3f} but the best bid is {max(bids):.3f}; "
               f"asks[0] = {asks[0]:.3f} but the best ask is {min(asks):.3f}")
    print("            (raw bids ascend, raw asks descend: index 0 is the WORST level)")
    ob = await pm.get_orderbook(tok)
    print(OK, f"get_orderbook -> best_bid {ob['best_bid']}, best_ask {ob['best_ask']}, "
              f"bids[0] {ob['bids'][0]['price']} (best-first, spread {ob['spread']})")


async def two_kalshi_has_no_asks():
    head(2, "Kalshi publishes bid ladders only, ascending, as dollar strings")
    ms = await kx.search_markets("", limit=3, min_volume=1000)
    if not ms:
        print(SKIP, "no Kalshi market found"); return
    t = ms[0]["ticker"]
    raw = (await kx._get(f"/markets/{t}/orderbook", {"depth": 5})).get("orderbook_fp") or {}
    y, n = raw.get("yes_dollars") or [], raw.get("no_dollars") or []
    if not y or not n:
        print(SKIP, f"{t}: ladder empty right now"); return
    print(f"     market: {t}")
    print(RAW, f"yes_dollars = {y[:3]}... , no_dollars = {n[:3]}...")
    print("            both are BID ladders (there is no ask side), ascending (best is LAST),")
    print("            and prices are strings like '0.5600', not integer cents")
    ob = await kx.get_orderbook(t, 5)
    print(OK, f"kalshi_get_orderbook -> best_yes_bid {ob['best_yes_bid']}, best_yes_ask {ob['best_yes_ask']} "
              f"(ask derived as 1 - NO bid, best-first, Decimal math)")


async def three_geoblocks_look_healthy():
    head(3, "A geoblocked machine gets healthy reads right up to the order")
    async with httpx.AsyncClient(timeout=15) as h:
        r = await h.get("https://gamma-api.polymarket.com/markets/keyset", params={"limit": 1, "closed": "false"})
        g = (await h.get(geo.GEOBLOCK_URL)).json()
    print(RAW, f"public reads answer HTTP {r.status_code} from anywhere; the block lands at ORDER placement,")
    print(f"            so a restricted operator sees a working server until the first trade")
    print(f"            polymarket.com/api/geoblock says this machine is country={g.get('country')} blocked={g.get('blocked')}")
    pf = await geo.preflight()
    print(OK, f"server_info preflight -> polymarket_orders={pf['polymarket_orders']!r} "
              f"(advisory; a 403 on an order is classified geo_suspected/geo_blocked with a stop-retrying hint)")


async def four_the_verdict_is_advisory():
    head(4, "That geoblock verdict is IP-based and takes unvalidated overrides")
    async with httpx.AsyncClient(timeout=15) as h:
        zz = (await h.get(geo.GEOBLOCK_URL, params={"country": "ZZ"})).json()
        ir = (await h.get(geo.GEOBLOCK_URL, params={"country": "IR"})).json()
    print(RAW, f"?country=ZZ -> blocked={zz.get('blocked')} ; ?country=IR -> blocked={ir.get('blocked')}")
    print("            'ZZ' is not a country; 'blocked: false' means absent from a list, not permitted.")
    print("            And the terms bind on residence and citizenship, not on egress IP.")
    print(OK, "oddsrail never gates a trade on it: preflight is reported as advisory, with the")
    print("            docs URL as the authority, and the venue's own rejection stays the arbiter")


async def five_the_endpoint_is_sunset():
    head(5, "The obvious Gamma endpoint is deprecated; the SDK path is not")
    async with httpx.AsyncClient(timeout=15) as h:
        old = await h.get("https://gamma-api.polymarket.com/markets", params={"limit": 1})
        new = await h.get("https://gamma-api.polymarket.com/markets/keyset", params={"limit": 1})
    print(RAW, f"/markets        -> deprecation={old.headers.get('deprecation')} sunset={old.headers.get('sunset')}")
    print(f"            warning: {old.headers.get('warning')}")
    print(OK, f"/markets/keyset -> {new.status_code}, no deprecation header; oddsrail reads through the "
              f"official SDK, which already uses keyset")


async def six_a_price_gap_is_not_an_edge():
    head(6, "Naive title matching pairs unrelated markets across venues")
    pmk = [xv.unify_polymarket(m) for m in await pm.search_markets("election", 8)]
    kal = [xv.unify_kalshi(m) for m in (await kx.search_markets_detailed("election", 8))["markets"]]
    if not pmk or not kal:
        print(SKIP, "one venue returned nothing for 'election'"); return
    words = lambda s: set(w for w in str(s).lower().replace("?", "").split() if len(w) > 3)
    naive = []
    for a in pmk:
        best = max(kal, key=lambda b: len(words(a["title"]) & words(b["title"])))
        if words(a["title"]) & words(best["title"]):
            naive.append((a["title"], best["title"], (a.get("yes_price") or 0) - (best.get("yes_price") or 0)))
    if naive:
        a, b, gap = max(naive, key=lambda x: abs(x[2]))
        print(RAW, f"{len(naive)} 'pairs' by word overlap; the widest 'gap' is {gap:+.2f} between")
        print(f"            {a[:70]!r}")
        print(f"            {b[:70]!r}")
    gated = xv.pair_across_venues(pmk + kal, min_similarity=0.5, max_close_days_apart=7)
    print(OK, f"compare_venues (similarity >= 0.5 AND close dates within 7 days) -> {len(gated)} candidate(s);")
    print("            and settlement_audit still has to say 'ok' before a difference counts as anything")


async def main():
    print("oddsrail footguns: live, read-only, no keys. Numbers are the venues' own, right now.")
    for fn in (one_book_arrives_worst_first, two_kalshi_has_no_asks, three_geoblocks_look_healthy,
               four_the_verdict_is_advisory, five_the_endpoint_is_sunset, six_a_price_gap_is_not_an_edge):
        try:
            await fn()
        except Exception as e:
            print(SKIP, f"{type(e).__name__}: {str(e)[:120]}")
    print("\nNeed keys to demonstrate, documented in the README and encoded in the trading tools:")
    print("  - the exchange models a rejected order as an ok:false RETURN VALUE, not an exception")
    print("  - an undocumented $1 minimum notional on marketable orders")
    print("  - the SDK's list_account_trades returns the market's PUBLIC tape, not your fills")
    print("\nhttps://github.com/hmesutozsoy/oddsrail   |   pip install oddsrail")


if __name__ == "__main__":
    asyncio.run(main())
