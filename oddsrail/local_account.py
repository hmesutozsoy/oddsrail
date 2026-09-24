"""Read authenticated collateral without mistaking holdings for trading cash.

polymarket-client 0.6.0 exposes ``client.wallet`` and
``get_balance_allowance(asset_type="COLLATERAL")``. Its BalanceAllowance
model holds integer base units; pUSD has six decimals. This module takes an
existing client and only calls that read endpoint. It never provisions a
wallet, changes allowances, signs an order, or falls back to public holdings.

The balance is not a reconciled spending budget: outstanding orders and
unsettled fills can reserve cash. Keep available_to_trade_usd unknown rather
than promising that the entire reported balance can fund another order.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

_READ_TIMEOUT_SECONDS = 10.0
_MICRO = 1_000_000
_MAX_UINT256 = 2**256 - 1
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")


def resolved_wallet(client: Any) -> str:
    """Return the SDK's authenticated account wallet, never its signer."""
    wallet = client.wallet
    if not isinstance(wallet, str) or _ADDRESS.fullmatch(wallet) is None:
        raise ValueError("The authenticated client has no valid account wallet")
    return wallet


def unavailable_balance(wallet: str | None = None) -> dict:
    """Unknown values are explicitly different from an account with zero cash."""
    return {
        "wallet": wallet if isinstance(wallet, str) and _ADDRESS.fullmatch(wallet) else None,
        "asset": "pUSD",
        "balance_status": "unavailable",
        "cash_balance_usd": None,
        "cash_balance_base_units": None,
        "collateral_decimals": 6,
        "allowances_base_units": None,
        "available_to_trade_usd": None,
        "note": (
            "Authenticated collateral could not be verified. Unknown does not mean zero. "
            "Public positions value is not cash. Confirm the account balance and "
            "outstanding orders before sizing a real order."
        ),
    }


def _base_units(value: Any) -> int:
    # The pinned SDK normalizes integer strings. Reject other shapes instead
    # of rounding a float or allowing a negative/infinite amount to look valid.
    if type(value) is not int or not 0 <= value <= _MAX_UINT256:
        raise ValueError("Invalid collateral base units")
    return value


async def read_cash_balance(client: Any) -> dict:
    """Read the SDK's cash snapshot; failure yields unknown, never zero."""
    wallet = None
    try:
        wallet = resolved_wallet(client)
        async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
            response = await client.get_balance_allowance(asset_type="COLLATERAL")
        balance = _base_units(response.balance)
        if not isinstance(response.allowances, dict):
            raise ValueError("Invalid collateral allowances")
        allowances = {}
        for spender, amount in response.allowances.items():
            if not isinstance(spender, str) or _ADDRESS.fullmatch(spender) is None:
                raise ValueError("Invalid allowance spender")
            allowances[spender] = str(_base_units(amount))
    except Exception as exc:
        # SDK/auth exceptions can contain request details. Expose their class,
        # not raw text, to the model and client logs.
        return {**unavailable_balance(wallet), "error_type": type(exc).__name__}

    whole, fractional = divmod(balance, _MICRO)
    return {
        "wallet": wallet,
        "asset": "pUSD",
        "balance_status": "verified",
        "cash_balance_usd": f"{whole}.{fractional:06d}",
        "cash_balance_base_units": str(balance),
        "collateral_decimals": 6,
        "allowances_base_units": allowances,
        "available_to_trade_usd": None,
        "source": "authenticated CLOB balance-allowance",
        "note": (
            "Cash is the authenticated pUSD collateral balance, not positions value. "
            "The amount available for a new order remains unverified: reconcile "
            "outstanding orders, unsettled fills and the applicable exchange allowance "
            "before sizing. This read does not change approvals or place an order."
        ),
    }


async def read_configured_cash(private_key: str, wallet: str) -> dict:
    """Authenticate a balance read without the SDK's wallet deployment step.

    The pinned 0.6.0 public create() calls _ensure_wallet_ready(), which can
    deploy an account. Its lower-level factory authenticates and resolves the
    supplied wallet only. Keep this boundary explicit and tested on SDK updates.
    Never provide relayer credentials to this read client.
    """
    if not isinstance(wallet, str) or _ADDRESS.fullmatch(wallet) is None:
        return unavailable_balance()
    client = None
    try:
        from polymarket import AsyncSecureClient
        async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
            client = await AsyncSecureClient._create(
                private_key=private_key, wallet=wallet,
                validate_credentials=True, api_key=None,
            )
            if resolved_wallet(client).lower() != wallet.lower():
                raise ValueError("Authenticated wallet does not match requested account")
            return await read_cash_balance(client)
    except Exception as exc:
        return {**unavailable_balance(wallet), "error_type": type(exc).__name__}
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
