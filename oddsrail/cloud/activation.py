"""Authenticated, read-only setup checks; there is no live activation route.

Sign-in proves the owner address. Public account discovery does not prove that
the owner controls a compatible trading wallet or authorize an order. The
funding, delegated permission and execution checks deliberately remain blocked
until those services exist; neither client JSON nor an environment switch can
turn this report into permission to trade.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from starlette.routing import Route

from .configuration import validate_config
from .portfolio import InvalidAddress, normalize_address
from .wallet_auth import WalletAuth, WalletRequestError

MAX_BODY_BYTES = 20000
ACCOUNT_TIMEOUT_SECONDS = 21.0
POLYMARKET_SETUP_URL = "https://polymarket.com/"


def _check(identifier: str, status: str, label: str, detail: str, *, href: str | None = None) -> dict:
    result = {"id": identifier, "status": status, "label": label, "detail": detail}
    if href:
        result["href"] = href
    return result


def _account_check(result: object, owner: str) -> dict:
    unavailable = _check(
        "trading_account", "unavailable", "Polymarket trading account",
        "Account discovery could not be completed. Refresh and try again; this does not mean you have no account.")
    if not isinstance(result, dict) or result.get("ok") is not True:
        return unavailable
    account = result.get("account")
    if not isinstance(account, dict):
        return unavailable
    try:
        if (normalize_address(result.get("address")) != owner or
                normalize_address(account.get("connected_address")) != owner):
            return unavailable
    except InvalidAddress:
        return unavailable
    if account.get("status") == "unresolved":
        return _check(
            "trading_account", "action_required", "Polymarket trading account",
            "No public trading account was found for this wallet. Open Polymarket and sign in with the same wallet to complete account setup, then return and check again. If you already have an account, refresh or check which wallet it uses.",
            href=POLYMARKET_SETUP_URL)
    if account.get("status") != "resolved" or account.get("source") != "polymarket_public_profile":
        return unavailable
    try:
        trading_address = normalize_address(account.get("trading_address"))
    except InvalidAddress:
        return unavailable
    return _check(
        "trading_account", "ready", "Polymarket trading account",
        f"A public profile links this wallet to {trading_address}. This is account discovery only; trading-wallet ownership and compatibility still need verification.")


class ActivationCheck:
    """Receive a bounded public loader from the app's shared read admission."""

    def __init__(self, load_portfolio: Callable[[str], Awaitable[dict]]):
        self._load_portfolio = load_portfolio

    def route(self, auth: WalletAuth) -> Route:
        return Route("/activation/check", auth.authenticated_post(
            self.check, keys={"config"}, max_body_bytes=MAX_BODY_BYTES), methods=["POST", "OPTIONS"])

    async def check(self, identity: dict, body: dict) -> dict:
        config = body["config"]
        try:
            if not isinstance(config, dict) or config.get("mode") != "draft":
                raise ValueError("Provide a draft configuration. This check does not authorize trading.")
            normalized = validate_config(config, allow_draft=True)
        except ValueError as exc:
            raise WalletRequestError("invalid_configuration", 400, detail=str(exc)) from None
        owner = normalize_address(identity["address"])
        checks = [
            _check("wallet", "ready", "Wallet sign-in",
                   "Your wallet signature and current OddsRail sign-in are verified. This is not trading permission."),
            _check("configuration", "ready", "Strategy and limits",
                   ("Your strategy settings, limits and selected outcome IDs are valid. Live market eligibility has not been checked."
                    if normalized["market_mode"] == "specific" else
                    "Your strategy settings, limits and market universe are valid. Live market eligibility has not been checked.")),
        ]
        # The shared app admission caps and coalesces these public reads. This
        # outer deadline also bounds injected loaders, without queueing threads
        # or making account-scoped CLOB calls.
        try:
            async with asyncio.timeout(ACCOUNT_TIMEOUT_SECONDS):
                portfolio = await self._load_portfolio(owner)
        except Exception:
            portfolio = None
        account = _account_check(portfolio, owner)
        checks.extend([
            account,
            _check("funding", "unavailable", "Trading balance",
                   "Available trading cash and allowances have not been verified. A public portfolio value cannot confirm funds available for this agent."),
            _check("permission", "unavailable", "Trading permission",
                   "Polymarket session-key authorization is not connected yet. Wallet sign-in does not grant an agent access to trade."),
            _check("execution", "unavailable", "Hosted runner",
                   "Hosted live execution is not enabled. This check does not save, start or trade with an agent."),
        ])
        blockers = [row["id"] for row in checks if row["status"] != "ready"]
        return {"ok": True, "address": identity["address"], "can_activate": False,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "checks": checks, "blockers": blockers}
