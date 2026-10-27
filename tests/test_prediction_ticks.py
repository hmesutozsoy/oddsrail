"""Pinned SDK price-grid contracts and the public MCP limit-order path, offline."""
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest
from polymarket import AsyncSecureClient
from polymarket._internal.actions.orders.context import resolve_rounding_config, validate_price_on_tick_grid
from polymarket._internal.actions.orders.market_data import _parse_tick_size_value
from polymarket._internal.actions.orders.orders import create_unsigned_order
from polymarket.errors import UnexpectedResponseError, UserInputError

from oddsrail import guard, trading
from oddsrail.prediction_ticks import TICK_SIZES

ASSET = str(2**200 + 2**60)
ADDRESS = "0x" + "11" * 20


def test_supported_price_grids_match_the_pinned_sdk():
    for tick in TICK_SIZES:
        assert _parse_tick_size_value(str(tick), "market mts") == tick
    for tick in ("0.002", "0.00001", "0", "NaN", True):
        with pytest.raises(UnexpectedResponseError):
            _parse_tick_size_value(tick, "market mts")


@pytest.mark.parametrize("tick,valid,invalid,amount_decimals", [
    ("0.005", "0.455", "0.451", 5),
    ("0.0025", "0.4475", "0.4501", 6),
])
def test_sdk_rejects_off_grid_prices_instead_of_rounding_them(tick, valid, invalid, amount_decimals):
    assert resolve_rounding_config(D(tick)).amount == amount_decimals
    assert validate_price_on_tick_grid(D(valid), D(tick), "price") == D(valid)
    for price in (invalid, "0", "1"):
        with pytest.raises(UserInputError):
            validate_price_on_tick_grid(D(price), D(tick), "price")


@pytest.mark.parametrize("tick,price,size,collateral,shares", [
    ("0.005", "0.455", "11.11", 5055050, 11110000),
    ("0.0025", "0.4475", "11.17", 4998575, 11170000),
])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
async def test_mcp_passes_fractional_cent_price_to_real_sdk_without_rounding(monkeypatch, tick, price, size, collateral, shares, side):
    """The real SDK builds the order; only metadata, signing and posting are fakes."""
    calls, drafts, orders = [], [], []

    async def metadata(_ctx, *, token_id):
        assert token_id == ASSET
        calls.append(token_id)
        return SimpleNamespace(tick_size=_parse_tick_size_value(tick, "market mts"), neg_risk=False)

    class OfflineClient:
        create_limit_order = AsyncSecureClient.create_limit_order

        async def _sign_order(self, draft, *, post_only):
            drafts.append(draft)
            assert post_only is True
            return create_unsigned_order(draft, wallet=ADDRESS, wallet_type="EOA")

        async def place_limit_order(self, **kwargs):
            assert kwargs["price"] == price and kwargs["size"] == size
            order = await self.create_limit_order(**kwargs)
            orders.append(order)
            return {"ok": True, "id": "offline-tick-order"}

    client = OfflineClient()
    client._ctx = SimpleNamespace(order_metadata=SimpleNamespace(resolve_market=metadata, fetch_current_market=metadata),
                                  wallet=ADDRESS, signer=SimpleNamespace(address=ADDRESS),
                                  environment_config=SimpleNamespace(chain_id=137, standard_exchange="0x" + "22" * 20,
                                                                     neg_risk_exchange="0x" + "33" * 20))
    async def connect():
        return client
    async def no_network(*args, **kwargs):
        pytest.fail("Price-grid regression must never access a live endpoint")
    monkeypatch.setattr(trading, "_client", connect)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    monkeypatch.setenv("ODDSRAIL_DRY_RUN", "0")
    guard.reset_session()
    try:
        result = await trading.place_order(ASSET, side, float(price), float(size), post_only=True)
    finally:
        guard.reset_session()
    assert result["accepted"] is True and result["execution_state"] == "accepted"
    assert calls == [ASSET] and len(drafts) == len(orders) == 1
    order = orders[0]
    assert (order.maker_amount, order.taker_amount) == ((collateral, shares) if side == "BUY" else (shares, collateral))
    assert D(collateral) / D(shares) == D(price)
    assert order.token_id == ASSET and order.builder == trading.builder_code()
    assert order.order_type == "GTC" and order.side == side and order.protocol_version == "2"
