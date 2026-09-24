"""Pinned-SDK account reads: exact units, correct wallet and unknown failures."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from polymarket.models import BalanceAllowance

from oddsrail import local_account

WALLET = "0x" + "12" * 20
SPENDER = "0x" + "34" * 20


class Account:
    wallet = WALLET
    # These deliberately differ; .wallet is the account holding funds.
    address = "0x" + "56" * 20
    wallet_address = "0x" + "78" * 20

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def get_balance_allowance(self, *, asset_type):
        self.calls.append(asset_type)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def refuse(*args, **kwargs):
        pytest.fail("account tests must not use HTTP or exchange credentials")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)


async def test_sdk_balance_is_cash_in_exact_six_decimal_units():
    maximum = 2**256 - 1
    # The real SDK normalizes wire strings to base-unit integers.
    client = Account(BalanceAllowance(balance="123456789", allowances={SPENDER: str(maximum)}))

    result = await local_account.read_cash_balance(client)

    assert client.calls == ["COLLATERAL"]
    assert result["wallet"] == WALLET
    assert result["balance_status"] == "verified"
    assert result["cash_balance_usd"] == "123.456789"
    assert result["cash_balance_base_units"] == "123456789"
    assert result["allowances_base_units"] == {SPENDER: str(maximum)}
    assert result["available_to_trade_usd"] is None
    assert "positions_value" not in result
    assert json.loads(json.dumps(result))["allowances_base_units"][SPENDER] == str(maximum)


@pytest.mark.parametrize("balance, dollars", [(0, "0.000000"), (1, "0.000001"), (1_000_000, "1.000000")])
async def test_zero_and_small_real_cash_balances_are_not_unknown(balance, dollars):
    result = await local_account.read_cash_balance(Account(BalanceAllowance(balance=balance, allowances={})))
    assert result["balance_status"] == "verified"
    assert result["cash_balance_usd"] == dollars
    # Even a successful cash read is not proof of market-specific allowance.
    assert result["available_to_trade_usd"] is None


@pytest.mark.parametrize("bad", [-1, True, 1.5, "1000000", None, float("nan"), float("inf"), 2**256])
async def test_bad_cash_shapes_never_report_a_verified_balance(bad):
    client = Account(SimpleNamespace(balance=bad, allowances={SPENDER: 100}))
    result = await local_account.read_cash_balance(client)
    assert result["balance_status"] == "unavailable"
    assert result["cash_balance_usd"] is None
    assert result["available_to_trade_usd"] is None


@pytest.mark.parametrize("bad", [None, [], {SPENDER: -1}, {SPENDER: True}, {"not-an-address": 1}])
async def test_bad_allowance_shapes_refuse_incomplete_account_snapshot(bad):
    result = await local_account.read_cash_balance(Account(SimpleNamespace(balance=1_000_000, allowances=bad)))
    assert result["balance_status"] == "unavailable"
    assert result["cash_balance_usd"] is None
    assert result["allowances_base_units"] is None


async def test_missing_authenticated_wallet_does_not_read_another_account():
    client = Account(BalanceAllowance(balance=1_000_000, allowances={}))
    client.wallet = None
    result = await local_account.read_cash_balance(client)
    assert result["wallet"] is None
    assert result["balance_status"] == "unavailable"
    assert client.calls == []


async def test_read_failure_does_not_leak_auth_exception_or_invent_zero():
    result = await local_account.read_cash_balance(Account(RuntimeError("sensitive auth response")))
    assert result["wallet"] == WALLET
    assert result["balance_status"] == "unavailable"
    assert result["cash_balance_usd"] is None
    assert result["error_type"] == "RuntimeError"
    assert "sensitive" not in json.dumps(result)


async def test_balance_read_has_a_deadline(monkeypatch):
    class Stalled(Account):
        async def get_balance_allowance(self, *, asset_type):
            await asyncio.Event().wait()

    monkeypatch.setattr(local_account, "_READ_TIMEOUT_SECONDS", 0.01)
    result = await local_account.read_cash_balance(Stalled(None))
    assert result["balance_status"] == "unavailable"
    assert result["error_type"] == "TimeoutError"


def test_unconfigured_cash_remains_explicitly_unknown():
    result = local_account.unavailable_balance(WALLET)
    assert result["wallet"] == WALLET
    assert result["cash_balance_usd"] is None
    assert result["cash_balance_base_units"] is None
    assert result["available_to_trade_usd"] is None


@pytest.mark.parametrize("wallet,key", [(None, None), ("0x" + "a" * 40, None), (None, "local-test-key")])
async def test_balance_tool_never_provisions_an_account(monkeypatch, wallet, key):
    from oddsrail import trading

    for name, value in (("POLYMARKET_WALLET_ADDRESS", wallet), ("POLYMARKET_PRIVATE_KEY", key)):
        monkeypatch.delenv(name, raising=False)
        if value:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(trading.hosted, "enabled", lambda: False)

    async def forbidden():
        raise AssertionError("A balance read must not initialize an unspecified account")

    monkeypatch.setattr(trading, "_client", forbidden)
    result = await trading.my_balance()
    assert result["balance_status"] == "unavailable"
    assert result["cash_balance_usd"] is None


async def test_balance_tool_returns_authenticated_cash_not_public_holdings(monkeypatch):
    from types import SimpleNamespace
    from oddsrail import trading, polymarket
    from polymarket import AsyncSecureClient

    wallet = "0x" + "a" * 40
    monkeypatch.setenv("POLYMARKET_WALLET_ADDRESS", wallet)
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "local-test-key")
    monkeypatch.setattr(trading.hosted, "enabled", lambda: False)

    async def read(**kwargs):
        assert kwargs == {"asset_type": "COLLATERAL"}
        return SimpleNamespace(balance=12345678, allowances={})

    async def close():
        pass

    async def client(**kwargs):
        assert kwargs["wallet"] == wallet
        assert kwargs["api_key"] is None
        assert kwargs["validate_credentials"] is True
        return SimpleNamespace(wallet=wallet, get_balance_allowance=read, close=close)

    async def forbidden(*args):
        raise AssertionError("Public holdings are not spendable cash")

    monkeypatch.setattr(AsyncSecureClient, "_create", client)
    monkeypatch.setattr(AsyncSecureClient, "create", forbidden)
    monkeypatch.setattr(polymarket, "portfolio_value", forbidden)
    result = await trading.my_balance()
    assert result["cash_balance_usd"] == "12.345678"
    assert result["available_to_trade_usd"] is None


async def test_configured_cash_read_rejects_different_wallet_and_closes(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from polymarket import AsyncSecureClient
    from oddsrail import local_account

    wallet = "0x" + "a" * 40
    client = SimpleNamespace(wallet="0x" + "b" * 40, close=AsyncMock(), get_balance_allowance=AsyncMock())
    factory = AsyncMock(return_value=client)
    monkeypatch.setattr(AsyncSecureClient, "_create", factory)
    result = await local_account.read_configured_cash("test-only", wallet)
    assert result["balance_status"] == "unavailable"
    client.get_balance_allowance.assert_not_awaited()
    client.close.assert_awaited_once()


async def test_configured_cash_read_sanitizes_auth_failure(monkeypatch):
    from unittest.mock import AsyncMock
    from polymarket import AsyncSecureClient
    from oddsrail import local_account

    monkeypatch.setattr(AsyncSecureClient, "_create", AsyncMock(side_effect=RuntimeError("secret-request-details")))
    result = await local_account.read_configured_cash("test-only", "0x" + "a" * 40)
    assert result["balance_status"] == "unavailable"
    assert "secret-request-details" not in str(result)
