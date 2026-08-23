"""Order routing with on-chain builder-code attribution (CLOB V2).

How attribution works (verified against docs.polymarket.com, Aug 2026):
the operator's bytes32 builder code — from polymarket.com/settings?tab=builder —
is placed in the `builder` field of the V2 order struct BEFORE signing, so
attribution is on-chain: every OrderFilled event on CTF Exchange V2 carries it,
and builder fees (taker <= 100 bps, maker <= 50 bps, additive to platform fees)
settle to the builder-profile wallet.

Safety model:
- ODDSRAIL_DRY_RUN=1 (default): place_order returns the exact order it WOULD
  post, never touches the exchange. Set ODDSRAIL_DRY_RUN=0 to trade.
- Trading requires POLYMARKET_PRIVATE_KEY (+ POLYMARKET_WALLET_ADDRESS for
  proxy/deposit wallets). Keys stay on this machine — oddsrail is self-hosted
  and non-custodial by design.
"""

import os

_secure = None


def dry_run() -> bool:
    return os.environ.get("ODDSRAIL_DRY_RUN", "1") not in ("0", "false", "no")


def builder_code() -> str | None:
    return os.environ.get("ODDSRAIL_BUILDER_CODE") or None


async def _client():
    global _secure
    if _secure is None:
        from polymarket import AsyncSecureClient
        key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not key:
            raise RuntimeError(
                "POLYMARKET_PRIVATE_KEY not set — trading tools are disabled. "
                "Read-only tools work without it.")
        _secure = await AsyncSecureClient.create(
            private_key=key,
            wallet=os.environ.get("POLYMARKET_WALLET_ADDRESS"),
        )
    return _secure


def _intent(token_id, side, price, size, order_type):
    code = builder_code()
    return {
        "exchange": "polymarket",
        "token_id": token_id,
        "side": side,
        "price": price,
        "size": size,
        "order_type": order_type,
        "builder_code": code,
        "attribution": ("on-chain: builder code signed into the order"
                        if code else
                        "NONE — set ODDSRAIL_BUILDER_CODE to attribute flow"),
    }


async def place_order(token_id: str, side: str, price: float, size: float,
                      order_type: str = "GTC"):
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if not (0 < price < 1):
        raise ValueError("price is an implied probability in (0, 1)")
    if size <= 0:
        raise ValueError("size must be positive (number of shares)")

    intent = _intent(token_id, side, price, size, order_type)
    if dry_run():
        return {"dry_run": True, "would_post": intent,
                "note": "set ODDSRAIL_DRY_RUN=0 to post real orders"}

    client = await _client()
    resp = await client.place_limit_order(
        token_id=token_id, price=str(price), size=str(size), side=side,
        builder_code=builder_code())
    from .polymarket import dump
    return {"dry_run": False, "posted": intent, "response": dump(resp)}


async def cancel_order(order_id: str):
    if dry_run():
        return {"dry_run": True, "would_cancel": order_id}
    client = await _client()
    from .polymarket import dump
    return dump(await client.cancel_order(order_id))


async def open_orders():
    if not os.environ.get("POLYMARKET_PRIVATE_KEY"):
        return {"note": "no trading key configured; nothing to list"}
    client = await _client()
    from .polymarket import dump
    page = await client.list_open_orders().first_page()
    return dump(list(page.items))
