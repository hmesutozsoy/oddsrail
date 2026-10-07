"""Local SDK migration contracts. Disposable data only; no real orders."""
from types import SimpleNamespace

import pytest

from oddsrail import polymarket as pm, trading

CTF = str(2**200 + 2**60)
V2 = str(1 << 248)


def market(version="v2"):
    return {"id": "42", "version": version, "condition_id": "0x" + "01" * 31,
            "outcomes": {"yes": {"token_id": CTF, "position_id": V2, "label": "Yes"},
                         "no": {"token_id": str(int(CTF)+1), "position_id": str(int(V2)+1), "label": "No"}}}


@pytest.mark.parametrize("version,expected", [("v1", CTF), ("v2", V2), (None, None), ("v3", None)])
def test_slim_selects_exact_protocol_ids(version, expected):
    assert pm.slim_market(market(version))["outcomes"]["yes"]["token_id"] == expected


async def test_lookup_uses_position_filter_and_verifies_returned_identity(monkeypatch):
    calls = []
    class Page:
        def __init__(self, items): self.items = items
        async def first_page(self): return self
    class Client:
        def list_markets(self, **kwargs):
            calls.append(kwargs)
            return Page([market("v1")] if "clob_token_ids" in kwargs else [market()])
    async def client(): return Client()
    monkeypatch.setattr(pm, "public", client)
    result = await pm.get_market_by_token(V2)
    assert result["outcomes"]["yes"]["token_id"] == V2
    assert calls == [{"clob_token_ids": V2, "page_size": 2}, {"position_ids": V2, "page_size": 2}]
    assert await pm.get_market_by_token(str(int(V2)+99)) is None


async def test_v2_live_order_refused_before_account_or_signer(monkeypatch):
    monkeypatch.setenv("ODDSRAIL_DRY_RUN", "0")
    async def forbidden(): pytest.fail("Must not authenticate or sign")
    monkeypatch.setattr(trading, "_client", forbidden)
    result = await trading.place_order(V2, "BUY", .5, 5)
    assert result["execution_state"] == "not_submitted"
    assert result["error_type"] == "ProtocolV2NotEnabled"
    assert not result["accepted"]


@pytest.mark.parametrize("action", ["split", "merge", "redeem"])
async def test_v2_relayer_refuses_condition_id_before_client(monkeypatch, action):
    monkeypatch.setenv("ODDSRAIL_DRY_RUN", "0")
    monkeypatch.setenv("POLYMARKET_RELAYER_API_KEY", "test")
    monkeypatch.setenv("POLYMARKET_RELAYER_API_KEY_ADDRESS", "0x"+"11"*20)
    async def forbidden(): pytest.fail("Must not submit a transaction")
    monkeypatch.setattr(trading, "_client", forbidden)
    cid=market()["condition_id"]
    result = await ({"split": lambda: trading.split_position(cid, 1),
                     "merge": lambda: trading.merge_positions(cid, 1),
                     "redeem": lambda: trading.redeem_positions(condition_id=cid)}[action])()
    assert result["error_type"] == "ProtocolV2NotEnabled"


async def test_v2_market_id_redemption_is_also_gated(monkeypatch):
    monkeypatch.setenv("ODDSRAIL_DRY_RUN", "0")
    monkeypatch.setenv("POLYMARKET_RELAYER_API_KEY", "test")
    monkeypatch.setenv("POLYMARKET_RELAYER_API_KEY_ADDRESS", "0x"+"11"*20)
    async def lookup(*a, **k): return market()
    async def forbidden(): pytest.fail("Must not submit a transaction")
    monkeypatch.setattr(pm, "get_market", lookup)
    monkeypatch.setattr(trading, "_client", forbidden)
    result=await trading.redeem_positions(market_id="42")
    assert result["error_type"] == "ProtocolV2NotEnabled"


@pytest.mark.parametrize("asset,expected_domain", [(CTF, "2"), (V2, "3")])
def test_reviewed_sdk_signing_domains_are_protocol_specific(asset, expected_domain):
    from polymarket._internal.actions.orders.orders import create_unsigned_order
    from polymarket._internal.actions.orders.types import OrderDraft
    from polymarket._internal.actions.orders.typed_data import build_order_typed_data
    from polymarket._internal.actions.orders.context import resolve_order_exchange_address
    address="0x"+"11"*20
    config=SimpleNamespace(exchange_v3="0x"+"33"*20,standard_exchange="0x"+"22"*20,neg_risk_exchange="0x"+"44"*20)
    exchange=resolve_order_exchange_address(config,asset_id=asset,neg_risk=False)
    draft=OrderDraft(chain_id=137,exchange_address=exchange,expiration=0,
                     funder_address=address,offered_amount=1000000,order_type="GTC",side="BUY",
                     signer=address,requested_amount=2000000,asset_id=asset)
    order=create_unsigned_order(draft,wallet=address,wallet_type="EOA")
    domain=build_order_typed_data(order)["domain"]
    assert domain["version"] == expected_domain
    assert domain["verifyingContract"] == exchange
