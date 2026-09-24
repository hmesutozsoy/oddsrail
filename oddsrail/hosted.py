"""Hosted profile: what changes when oddsrail runs as a shared remote MCP
server (mcp.oddsrail.app) instead of on the operator's own machine.

  ODDSRAIL_HOSTED=1   turns the profile on (oddsrail.cloud.app sets it)

The hosted server holds no trading keys and executes no financial
transaction: every order is papered against the live Polymarket book into
the signed-in account's own ledger. Live trading stays self-hosted
(pip install oddsrail) until hosted wallets ship.

Kalshi is not served at all. Its API Developer Agreement limits API use to a
member's own trading, so one shared service routing many members would
breach it; self-host with your own Kalshi key instead.
"""

from __future__ import annotations

import os


def enabled() -> bool:
    return os.environ.get("ODDSRAIL_HOSTED", "").strip().lower() in ("1", "true", "yes")


KALSHI_NOTE = ("Kalshi is not available on the hosted server: Kalshi's API Developer "
               "Agreement limits API use to a member's own trading, so a shared "
               "service cannot route it. Self-host for Kalshi: pip install oddsrail, "
               "then set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")

LIVE_NOTE = ("The hosted server sends nothing real: every order is papered against "
             "the live Polymarket book into your account's ledger. For live trading "
             "self-host oddsrail (pip install oddsrail) with your own key; the "
             "same tools then post real orders when ODDSRAIL_DRY_RUN=0.")

PLACE_ORDER_DESC = ("Place a PAPER limit order on Polymarket: filled against the "
                    "live order book into your account's paper ledger (virtual "
                    "bankroll, see paper_positions). Nothing real is sent and the "
                    "hosted server holds no keys. price is the implied probability "
                    "in (0,1); size is in SHARES (notional = price * size); the "
                    "exchange's $1 minimum on marketable orders is mirrored. "
                    "Returns the order intent plus the simulated fill. Live "
                    "trading is self-hosted: pip install oddsrail.")


class VenueUnavailable(Exception):
    """Raised at the Kalshi transport boundary in hosted mode, so every
    Kalshi tool fails the same way with the same explanation."""

    hint = KALSHI_NOTE

    def __init__(self, msg: str = "Kalshi is not served by the hosted oddsrail"):
        super().__init__(msg)


# Account-scoped or key-holding tools. On a shared server they would read or
# act on the OPERATOR's account, which is nobody's, so they are removed in
# hosted mode. What remains is public data plus the caller's own paper ledger.
HIDDEN_TOOLS = (
    "open_orders", "order_status", "my_fills", "my_positions", "cancel_all_orders",
    "split_position", "merge_positions", "redeem_positions", "redeemable_positions",
    "compare_venues", "settlement_audit",
    "kalshi_search_markets", "kalshi_get_market", "kalshi_get_orderbook",
    "kalshi_get_trades", "kalshi_balance", "kalshi_positions", "kalshi_open_orders",
    "kalshi_place_order", "kalshi_cancel_order",
)
HIDDEN_PROMPTS = ("check_cross_venue_edge", "settle_resolved", "daily_review")


def venues(want: str) -> tuple[str, str | None]:
    """Narrow a venues= argument for hosted mode.

    'both' becomes 'polymarket' with a note the tool should surface; 'kalshi'
    becomes an empty venue with the note as the error. Outside hosted mode
    the argument passes through untouched.
    """
    if not enabled():
        return want, None
    if want == "kalshi":
        return "", KALSHI_NOTE
    if want == "both":
        return "polymarket", KALSHI_NOTE
    return want, None
