"""Gasless position management — the paths where a bug moves collateral.

Offline: the SDK client is never constructed; the tests pin the dry-run
guarantee, the not-configured guard (no silent EOA fallback), unit
conversion, and input validation."""

import pytest

from oddsrail import trading


@pytest.fixture(autouse=True)
def _no_client(monkeypatch):
    async def boom():
        raise AssertionError("network client must not be constructed here")
    monkeypatch.setattr(trading, "_client", boom)
    monkeypatch.setattr(trading, "_secure", None)


# --------------------------------------------------------------------------- #
# unit conversion — USDC has 6 decimals, the SDK wants integers                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("usdc,base", [
    (1, 1_000_000), (1.5, 1_500_000), ("25", 25_000_000),
    (0.000001, 1), (0.1 + 0.2, 300_000),   # rounds, never truncates
])
def test_usdc_to_base_units(usdc, base):
    assert trading.usdc_to_base(usdc) == base


@pytest.mark.parametrize("bad", [0, -1, "abc", None, "max"])
def test_usdc_to_base_rejects_non_positive(bad):
    with pytest.raises(ValueError):
        trading.usdc_to_base(bad)


# --------------------------------------------------------------------------- #
# dry-run never touches the network, for all three                            #
# --------------------------------------------------------------------------- #

async def test_split_dry_run(monkeypatch):
    monkeypatch.delenv("ODDSRAIL_DRY_RUN", raising=False)
    out = await trading.split_position("0xcond", 12.5)
    assert out["dry_run"] is True
    assert out["would_submit"]["amount_base_units"] == 12_500_000


async def test_merge_dry_run_max_and_amount(monkeypatch):
    monkeypatch.delenv("ODDSRAIL_DRY_RUN", raising=False)
    out = await trading.merge_positions("0xcond", "max")
    assert out["dry_run"] is True and out["would_submit"]["amount"] == "max"
    out = await trading.merge_positions("0xcond", 3)
    assert out["would_submit"]["amount_base_units"] == 3_000_000


async def test_redeem_dry_run(monkeypatch):
    monkeypatch.delenv("ODDSRAIL_DRY_RUN", raising=False)
    out = await trading.redeem_positions(market_id="12345")
    assert out["dry_run"] is True and out["would_submit"]["market_id"] == "12345"


# --------------------------------------------------------------------------- #
# live mode without a relayer key: structured refusal, NO gas-paying fallback  #
# --------------------------------------------------------------------------- #

async def test_live_without_relayer_key_sends_nothing(monkeypatch):
    monkeypatch.setenv("ODDSRAIL_DRY_RUN", "0")
    for var in ("POLYMARKET_RELAYER_API_KEY", "POLYMARKET_RELAYER_API_KEY_ADDRESS"):
        monkeypatch.delenv(var, raising=False)
    out = await trading.split_position("0xcond", 1)
    assert out["accepted"] is False
    assert "Relayer API keys" in out["error"]
    # the autouse fixture would have raised had _client() been called


def test_relayer_configured_needs_both_halves(monkeypatch):
    monkeypatch.setenv("POLYMARKET_RELAYER_API_KEY", "k")
    monkeypatch.delenv("POLYMARKET_RELAYER_API_KEY_ADDRESS", raising=False)
    assert trading.relayer_configured() is False
    monkeypatch.setenv("POLYMARKET_RELAYER_API_KEY_ADDRESS", "0x" + "1" * 40)
    assert trading.relayer_configured() is True


# --------------------------------------------------------------------------- #
# input validation happens before anything else                               #
# --------------------------------------------------------------------------- #

async def test_redeem_requires_exactly_one_identifier():
    with pytest.raises(ValueError):
        await trading.redeem_positions()
    with pytest.raises(ValueError):
        await trading.redeem_positions(condition_id="0xc", market_id="1")


async def test_split_requires_condition_id_and_positive_amount():
    with pytest.raises(ValueError):
        await trading.split_position("", 1)
    with pytest.raises(ValueError):
        await trading.split_position("0xc", 0)
