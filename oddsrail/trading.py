"""Order routing with on-chain builder-code attribution (CLOB V2).

How attribution works (verified against docs.polymarket.com, Aug 2026):
the operator's bytes32 builder code — from polymarket.com/settings?tab=builder —
is placed in the `builder` field of the V2 order struct BEFORE signing, so
attribution is on-chain: every OrderFilled event on CTF Exchange V2 carries it,
and builder fees (taker <= 100 bps, maker <= 50 bps, additive to platform fees)
settle to the builder-profile wallet.

Attribution defaults (how this project sustains itself):
oddsrail ships with a project builder code set at 0 bps, so routed orders are
attributed by default. At 0 bps this costs the operator NOTHING — no fee is
added to any trade — and the project earns only a share of Polymarket's
weekly builder reward pool, which Polymarket pays out of its own program.
Set ODDSRAIL_BUILDER_CODE to your own code to claim that share instead; the
project code is a default, never a lock-in. server_info reports which is
in use.

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


# The project's own builder code ("oddsrail" builder profile), applied when the
# operator has not set one. Registered at 0 bps maker / 0 bps taker, so it
# attributes routed flow without adding a fee to anyone's trade.
DEFAULT_BUILDER_CODE = "0xa576c5ce9fabba322d8fa3a8d16738221d1b6b2b0c57b544f757fa9e45a09a90"


def builder_code() -> str | None:
    """Operator's code if set, else the project default (may be empty)."""
    return os.environ.get("ODDSRAIL_BUILDER_CODE") or DEFAULT_BUILDER_CODE or None


def builder_code_source() -> str:
    if os.environ.get("ODDSRAIL_BUILDER_CODE"):
        return "operator (ODDSRAIL_BUILDER_CODE)"
    if DEFAULT_BUILDER_CODE:
        return "oddsrail project default (0 bps — no fee added; override with ODDSRAIL_BUILDER_CODE)"
    return "none configured"


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
        "attribution": ("on-chain: builder code signed into the order "
                        f"[{builder_code_source()}]"
                        if code else
                        "NONE — no builder code configured"),
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
