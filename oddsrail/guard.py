"""Operator guardrails: hard limits the agent cannot argue with.

Anyone handing keys to an agent wants three things before anything else: a
cap on how big one order can be, a cap on how much can go out in a session,
and a way to fence the agent into specific markets. These are set by the
OPERATOR through environment variables and enforced before any network call,
in dry-run as well as live, so an agent discovers the fence in rehearsal
rather than in production.

  ODDSRAIL_MAX_ORDER_NOTIONAL    max USDC notional per order (price * size)
  ODDSRAIL_MAX_SESSION_NOTIONAL  max cumulative USDC notional of LIVE orders
                                 submitted by this server process
  ODDSRAIL_MAX_OPEN_ORDERS       max resting orders on the account (live only;
                                 checked against the venue before placing)
  ODDSRAIL_ALLOWED_MARKETS       comma-separated Polymarket token ids and/or
                                 Kalshi tickers; when set, anything else is
                                 refused

Unset means unlimited. A refusal is a structured answer, never an exception,
and it names the rule, the limit and the request so the agent can report
rather than retry. The session counter lives in this process: restarting the
server resets it, which is the operator's call to make.
"""

from __future__ import annotations

import os

_session_notional = 0.0
_session_orders = 0


def _f(name: str) -> float | None:
    v = os.environ.get(name, "").strip()
    if not v:
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return x if x > 0 else None


def _i(name: str) -> int | None:
    x = _f(name)
    return int(x) if x is not None else None


def allowed_markets() -> set[str] | None:
    raw = os.environ.get("ODDSRAIL_ALLOWED_MARKETS", "").strip()
    if not raw:
        return None
    return {m.strip() for m in raw.split(",") if m.strip()}


def status() -> dict:
    """What server_info reports."""
    return {
        "max_order_notional_usd": _f("ODDSRAIL_MAX_ORDER_NOTIONAL"),
        "max_session_notional_usd": _f("ODDSRAIL_MAX_SESSION_NOTIONAL"),
        "max_open_orders": _i("ODDSRAIL_MAX_OPEN_ORDERS"),
        "allowed_markets": sorted(allowed_markets()) if allowed_markets() else None,
        "session_notional_used_usd": round(_session_notional, 2),
        "session_live_orders": _session_orders,
        "note": ("operator-set via ODDSRAIL_MAX_ORDER_NOTIONAL, "
                 "ODDSRAIL_MAX_SESSION_NOTIONAL, ODDSRAIL_MAX_OPEN_ORDERS, "
                 "ODDSRAIL_ALLOWED_MARKETS; unset means unlimited. Enforced in "
                 "dry-run too. The agent cannot change them."),
    }


def _refusal(rule: str, limit, requested, dry: bool, detail: str = "") -> dict:
    return {
        "dry_run": dry, "accepted": False, "blocked_by": "guardrail",
        "rule": rule, "limit": limit, "requested": requested,
        "note": (f"refused by an operator-set guardrail ({rule}). {detail}"
                 "Nothing was sent. The agent cannot change this limit; "
                 "report it to the operator.").replace("  ", " "),
    }


def check_order(venue: str, market_id: str, notional_usd: float,
                dry: bool, open_orders_now: int | None = None) -> dict | None:
    """Return a refusal dict if any guardrail blocks this order, else None.

    open_orders_now is supplied by the caller only in live mode (it costs a
    venue call); None skips that check.
    """
    allowed = allowed_markets()
    if allowed is not None and str(market_id) not in allowed:
        return _refusal("allowed_markets", sorted(allowed), str(market_id), dry,
                        f"{venue} market {market_id!r} is not on the operator's "
                        "ODDSRAIL_ALLOWED_MARKETS list. ")
    cap = _f("ODDSRAIL_MAX_ORDER_NOTIONAL")
    if cap is not None and notional_usd > cap:
        return _refusal("max_order_notional", cap, round(notional_usd, 2), dry,
                        f"order notional ${notional_usd:,.2f} exceeds the "
                        f"per-order cap ${cap:,.2f}. ")
    scap = _f("ODDSRAIL_MAX_SESSION_NOTIONAL")
    if scap is not None and not dry and _session_notional + notional_usd > scap:
        return _refusal("max_session_notional", scap,
                        round(_session_notional + notional_usd, 2), dry,
                        f"${_session_notional:,.2f} already submitted this "
                        f"session; this order would take it to "
                        f"${_session_notional + notional_usd:,.2f}. ")
    ocap = _i("ODDSRAIL_MAX_OPEN_ORDERS")
    if ocap is not None and open_orders_now is not None and open_orders_now >= ocap:
        return _refusal("max_open_orders", ocap, open_orders_now, dry,
                        f"{open_orders_now} orders already rest on the account. ")
    return None


def record_live_submission(notional_usd: float) -> None:
    """Call after a LIVE order was handed to the venue (accepted or not — the
    intent went out, and a rejected order can still be resubmitted)."""
    global _session_notional, _session_orders
    _session_notional += float(notional_usd)
    _session_orders += 1


def reset_session() -> None:
    global _session_notional, _session_orders
    _session_notional = 0.0
    _session_orders = 0
