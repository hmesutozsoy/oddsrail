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

Gasless position management (relayer):
split / merge / redeem go through Polymarket's relayer, which needs a
per-operator Relayer API key (polymarket.com -> Settings -> Relayer API keys)
in POLYMARKET_RELAYER_API_KEY + POLYMARKET_RELAYER_API_KEY_ADDRESS. That is
the pattern Polymarket's builder team recommends for self-hosted tools: no
builder secret ships with the product, each operator authenticates the
relayer as themselves. Without the key these tools return a structured
"not configured" answer and send nothing — they never fall back to a
gas-paying EOA broadcast, because a surprise gas spend is not a feature.

Safety model:
- ODDSRAIL_DRY_RUN=1 (default): place_order returns the exact order it WOULD
  post, never touches the exchange. Set ODDSRAIL_DRY_RUN=0 to trade.
- Trading requires POLYMARKET_PRIVATE_KEY (+ POLYMARKET_WALLET_ADDRESS for
  proxy/deposit wallets). Keys stay on this machine — oddsrail is self-hosted
  and non-custodial by design.
"""

import os

from . import geo

_secure = None


# Exactly these three values turn off the safety net; each reads unambiguously
# as "dry run: off". Anything else — unset, empty, a typo, a different case —
# stays dry, so a mistake fails to "no trade" rather than "wrong trade".
_LIVE_VALUES = ("0", "false", "no")


def dry_run() -> bool:
    return os.environ.get("ODDSRAIL_DRY_RUN", "1") not in _LIVE_VALUES


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


def relayer_key() -> tuple[str, str] | None:
    k = os.environ.get("POLYMARKET_RELAYER_API_KEY")
    a = os.environ.get("POLYMARKET_RELAYER_API_KEY_ADDRESS")
    return (k, a) if k and a else None


def relayer_configured() -> bool:
    return relayer_key() is not None


async def _client():
    global _secure
    if _secure is None:
        from polymarket import AsyncSecureClient
        key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not key:
            raise RuntimeError(
                "POLYMARKET_PRIVATE_KEY not set — trading tools are disabled. "
                "Read-only tools work without it.")
        api_key = None
        rk = relayer_key()
        if rk:
            from polymarket.auth import RelayerApiKey
            api_key = RelayerApiKey(key=rk[0], address=rk[1])
        _secure = await AsyncSecureClient.create(
            private_key=key,
            wallet=os.environ.get("POLYMARKET_WALLET_ADDRESS"),
            api_key=api_key,
        )
    return _secure


def _intent(token_id, side, price, size, post_only):
    code = builder_code()
    return {
        "exchange": "polymarket",
        "token_id": token_id,
        "side": side,
        "price": price,
        "size": size,
        "size_unit": "shares (notional = price * size, in USDC)",
        "post_only": post_only,
        "builder_code": code,
        "attribution": ("on-chain: builder code signed into the order "
                        f"[{builder_code_source()}]"
                        if code else
                        "NONE — no builder code configured"),
    }


async def place_order(token_id: str, side: str, price: float, size: float,
                      post_only: bool = False):
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if not (0 < price < 1):
        raise ValueError("price is an implied probability in (0, 1)")
    if size <= 0:
        raise ValueError("size must be positive (number of shares)")

    intent = _intent(token_id, side, price, size, post_only)
    if dry_run():
        return {"dry_run": True, "would_post": intent,
                "note": "set ODDSRAIL_DRY_RUN=0 to post real orders"}

    from .polymarket import dump
    try:
        # _client() belongs INSIDE the try: its first outbound call is the
        # CLOB auth handshake, so a network block lands here — and this tool
        # is not idempotent, so the agent must get a structured answer, not a
        # bare MCP error it might retry into a doubled position.
        client = await _client()
        resp = await client.place_limit_order(
            token_id=token_id, price=str(price), size=str(size), side=side,
            post_only=post_only, builder_code=builder_code())
    except Exception as e:
        out = {"dry_run": False, "accepted": False,
               "error_type": type(e).__name__, "error": str(e),
               "submitted": intent,
               "note": ("no confirmation was received. If this was a timeout "
                        "the order MAY still have posted — call open_orders "
                        "before retrying. Any other failure happened before "
                        "the exchange accepted anything.")}
        cls = geo.classify(e, getattr(e, "status", None), venue="polymarket")
        if cls:
            out["failure_class"] = cls
            out["hint"] = geo.HINTS[cls].format(host="the Polymarket CLOB")
        return out
    try:
        d = dump(resp)
    except Exception as e:
        # The exchange ANSWERED — a failure here is ours, not a rejection,
        # and the order may well rest. Never report this as "not posted".
        return {"dry_run": False, "accepted": False,
                "response_shape": "unparseable",
                "error_type": type(e).__name__, "error": str(e),
                "submitted": intent,
                "note": ("the exchange answered but the response could not "
                         "be parsed — the order MAY rest. Call open_orders "
                         "before retrying.")}
    return _interpret_order_response(d, intent)


def _interpret_order_response(d, intent):
    """The SDK models rejection as a RETURN VALUE (ok=False), not an
    exception. Reporting that under a key that reads as success — next to an
    attribution string claiming the order was signed on-chain — would make an
    agent believe a rejected order rests. Branch on it, and treat a shape
    this version does not recognise as NOT CONFIRMED rather than as either
    success or rejection."""
    if not isinstance(d, dict) or "ok" not in d:
        return {"dry_run": False, "accepted": False,
                "response_shape": "unrecognised — no 'ok' field",
                "order": d, "submitted": intent,
                "note": ("the SDK returned a shape this version does not "
                         "recognise; NOT confirmed as posted and NOT "
                         "confirmed as rejected. Call open_orders or "
                         "order_status to see whether the order rests "
                         "before retrying.")}
    if not d.get("ok"):
        return {"dry_run": False, "accepted": False,
                "rejected_code": d.get("code"),
                "rejected_reason": d.get("message"),
                "submitted": intent,
                "note": "rejected by the exchange — no order rests, nothing was attributed"}
    return {"dry_run": False, "accepted": True, "order": d, "submitted": intent}


async def cancel_order(order_id: str):
    if dry_run():
        return {"dry_run": True, "would_cancel": order_id}
    from .polymarket import dump
    try:
        client = await _client()
        # SDK signature is cancel_order(*, order_id=...) — keyword-only.
        return {"ok": True, "order_id": order_id,
                "response": dump(await client.cancel_order(order_id=order_id))}
    except Exception as e:
        # An agent pulling a stale quote off a moving market needs to know
        # whether it is still exposed, not just that Python raised.
        return {"ok": False, "order_id": order_id,
                "error_type": type(e).__name__, "error": str(e),
                "still_resting": "unknown — call open_orders to confirm"}


async def open_orders():
    if not os.environ.get("POLYMARKET_PRIVATE_KEY"):
        return {"note": "no trading key configured; nothing to list"}
    client = await _client()
    from .polymarket import dump
    page = await client.list_open_orders().first_page()
    return dump(list(page.items))


async def order_status(order_id: str):
    """Lifecycle answer for one order: resting / filled / gone.

    Without this an agent that placed an order was blind — open_orders shows
    presence but not fill progress, and a vanished id is ambiguous between
    "filled" and "cancelled". get_order reports size_matched directly.
    """
    if not os.environ.get("POLYMARKET_PRIVATE_KEY"):
        return {"note": "no trading key configured"}
    from .polymarket import dump
    try:
        client = await _client()
        d = dump(await client.get_order(order_id=order_id))
        matched = float(d.get("size_matched") or 0)
        size = float(d.get("original_size") or d.get("size") or 0)
        state = ("filled" if size and matched >= size else
                 "partially_filled" if matched > 0 else "resting")
        return {"found": True, "state": state, "size_matched": matched,
                "original_size": size, "order": d}
    except Exception as e:
        return {"found": False,
                "note": ("order not found — it either fully filled and left the "
                         "book, was cancelled, or never existed. my_fills() "
                         "shows recent executions."),
                "error_type": type(e).__name__, "error": str(e)[:200]}


async def cancel_all_orders():
    """Kill switch: pull every resting order on the operator's account."""
    if dry_run():
        return {"dry_run": True,
                "would_cancel": "ALL resting orders on the operator account"}
    from .polymarket import dump
    try:
        client = await _client()
        return {"ok": True, "response": dump(await client.cancel_all())}
    except Exception as e:
        return {"ok": False, "error_type": type(e).__name__, "error": str(e),
                "still_resting": "unknown — call open_orders to confirm"}


def operator_wallet() -> str | None:
    return (os.environ.get("POLYMARKET_WALLET_ADDRESS")
            or os.environ.get("POLYMARKET_FUNDER") or None)


async def my_fills(limit: int = 25):
    """Recent executions for the operator wallet, from the Data API activity
    feed. Deliberately NOT list_account_trades(): that SDK call returns the
    market's PUBLIC tape (other people's trades) — a verified footgun."""
    wallet = operator_wallet()
    if not wallet:
        return {"note": "set POLYMARKET_WALLET_ADDRESS to see fills"}
    import httpx
    async with httpx.AsyncClient(timeout=20.0) as h:
        r = await h.get("https://data-api.polymarket.com/activity",
                        params={"user": wallet, "limit": limit, "type": "TRADE"})
        r.raise_for_status()
        acts = r.json()
    return {"wallet": wallet, "fills": [
        {"time": a.get("timestamp"), "side": a.get("side"),
         "size": a.get("size"), "price": a.get("price"),
         "market": str(a.get("title"))[:80], "tx": a.get("transactionHash")}
        for a in (acts if isinstance(acts, list) else [])]}


# ------------------------- gasless position management ------------------- #

_USDC_BASE = 1_000_000  # USDC has 6 decimals; the SDK takes base units

_RELAYER_NOT_CONFIGURED = (
    "POLYMARKET_RELAYER_API_KEY and POLYMARKET_RELAYER_API_KEY_ADDRESS are not "
    "set. Gasless split/merge/redeem go through Polymarket's relayer, which "
    "needs YOUR OWN Relayer API key: polymarket.com -> Settings -> Relayer API "
    "keys. Nothing was sent. (Unverified builders get 100 relayer requests a "
    "day; verified builders 10,000.)")


def usdc_to_base(amount_usdc) -> int:
    """USDC -> integer base units, refusing anything that is not a positive
    amount. Rounds to the nearest micro-USDC rather than truncating."""
    try:
        v = float(amount_usdc)
    except (TypeError, ValueError):
        raise ValueError("amount_usdc must be a number")
    if not v > 0:
        raise ValueError("amount_usdc must be positive")
    return int(round(v * _USDC_BASE))


async def _relayed(action: str, intent: dict, run):
    """Shared path for the three relayer operations.

    `run(client)` returns the SDK transaction handle. The outcome is awaited
    so the agent gets a terminal answer; if waiting itself fails, the handle
    ids are still returned so the agent can report rather than resubmit.
    """
    if dry_run():
        return {"dry_run": True, "would_submit": intent,
                "note": "set ODDSRAIL_DRY_RUN=0 to submit real relayer transactions"}
    if not relayer_configured():
        return {"dry_run": False, "accepted": False, "submitted": intent,
                "error": _RELAYER_NOT_CONFIGURED}
    from .polymarket import dump
    try:
        client = await _client()
        handle = await run(client)
    except Exception as e:
        out = {"dry_run": False, "accepted": False,
               "error_type": type(e).__name__, "error": str(e),
               "submitted": intent,
               "note": (f"{action} was not confirmed. If this was a timeout it "
                        "MAY have been relayed — check positions before "
                        "resubmitting; any other failure happened before the "
                        "relayer accepted it.")}
        cls = geo.classify(e, getattr(e, "status", None), venue="polymarket")
        if cls:
            out["failure_class"] = cls
            out["hint"] = geo.HINTS[cls].format(host="the Polymarket relayer")
        return out
    result = {"dry_run": False, "accepted": True, "submitted": intent,
              "transaction_id": getattr(handle, "transaction_id", None),
              "transaction_hash": getattr(handle, "transaction_hash", None)}
    try:
        result["outcome"] = dump(await handle.wait())
    except Exception as e:
        result["outcome"] = "unknown"
        result["error_type"] = type(e).__name__
        result["error"] = str(e)
        result["note"] = ("the relayer ACCEPTED the transaction but waiting for "
                          "its terminal state failed — do not resubmit; check "
                          "my_positions and the transaction hash instead")
    return result


async def split_position(condition_id: str, amount_usdc: float):
    """USDC collateral -> a full YES+NO set for one market (gasless)."""
    if not condition_id:
        raise ValueError("condition_id is required")
    base = usdc_to_base(amount_usdc)
    intent = {"action": "split", "condition_id": condition_id,
              "amount_usdc": float(amount_usdc), "amount_base_units": base,
              "via": "relayer (gasless)"}
    return await _relayed("split", intent,
                          lambda c: c.split_position(condition_id=condition_id,
                                                     amount=base))


async def merge_positions(condition_id: str, amount="max"):
    """Matching YES+NO shares -> USDC collateral (gasless). amount is in
    shares, or "max" for the largest balanced amount held."""
    if not condition_id:
        raise ValueError("condition_id is required")
    if isinstance(amount, str) and amount.strip().lower() == "max":
        amt = "max"
    else:
        amt = usdc_to_base(amount)  # shares carry the same 6-decimal scale
    intent = {"action": "merge", "condition_id": condition_id,
              "amount": amt if amt == "max" else float(amount),
              "amount_base_units": None if amt == "max" else amt,
              "via": "relayer (gasless)"}
    return await _relayed("merge", intent,
                          lambda c: c.merge_positions(condition_id=condition_id,
                                                      amount=amt))


async def redeem_positions(condition_id: str = "", market_id: str = ""):
    """Winning shares of a RESOLVED market -> USDC (gasless). Pass exactly one
    of condition_id or market_id."""
    if bool(condition_id) == bool(market_id):
        raise ValueError("pass exactly one of condition_id or market_id")
    intent = {"action": "redeem", "condition_id": condition_id or None,
              "market_id": market_id or None, "via": "relayer (gasless)"}
    if condition_id:
        run = lambda c: c.redeem_positions(condition_id=condition_id)
    else:
        run = lambda c: c.redeem_positions(market_id=market_id)
    return await _relayed("redeem", intent, run)
