"""Canonical account discovery and current-owner checks using offline RPC data."""

import asyncio
from copy import deepcopy
import json

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from starlette.applications import Starlette

from oddsrail.cloud import trading_accounts as ta
from oddsrail.cloud.wallet_auth import WalletAuth

OWNER = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
ORIGIN = "https://oddsrail.app"
NOW = 1800000000


def word(value):
    if isinstance(value, str):
        return "0x" + value[2:].rjust(64, "0")
    return "0x" + format(value, "064x")


def owners(values):
    return word(32) + word(len(values))[2:] + "".join(word(value)[2:] for value in values)


class Rpc:
    def __init__(self, owner=OWNER):
        self.owner = owner.lower()
        self.possible = ta.candidates(owner)
        self.records = {row["address"]: row for row in self.possible}
        self.calls = []
        self.absent = set()
        self.owner_overrides = {}
        self.threshold = 1
        self.safe_owners = [self.owner]
        self.proxy_owner = ta.DERIVATION["proxy_factory"]
        self.proxy_implementation = ta.DERIVATION["proxy_implementation"]
        self.chain = "0x89"
        self.block = {"number": "0x123456", "timestamp": hex(NOW)}
        self.change = None

    def __call__(self, request):
        assert str(request.url) == ta.RPC_URL
        assert request.method == "POST"
        assert not any(key in request.headers for key in ("authorization", "cookie", "poly_signature", "poly_api_key"))
        batch = json.loads(request.content)
        rows = []
        for call in batch:
            self.calls.append(call)
            method, params = call["method"], call["params"]
            assert method in {"eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_call", "eth_getStorageAt"}
            if method == "eth_chainId":
                result = self.chain
            elif method == "eth_getBlockByNumber":
                assert params == ["latest", False]
                result = self.block
            elif method == "eth_getCode":
                assert params[1] == self.block["number"]
                result = "0x" if params[0] in self.absent else "0x60006000"
                if self.records.get(params[0], {}).get("wallet_type") == "POLY_PROXY" and params[0] not in self.absent:
                    result = "0x363d3d373d3d3d363d73" + ta.DERIVATION["proxy_implementation"][2:] + "5af43d82803e903d91602b57fd5bf3"
            elif method == "eth_getStorageAt":
                assert params[1:] == [ta.PROXY_OWNER_SLOT, self.block["number"]]
                result = word(self.proxy_owner)
            else:
                assert params[1] == self.block["number"] and set(params[0]) == {"to", "data"}
                address, selector = params[0]["to"], params[0]["data"]
                if selector == ta.OWNER_SELECTOR:
                    assert self.records[address]["wallet_type"] == "DEPOSIT_WALLET"
                    result = word(self.owner_overrides.get(address, self.owner))
                elif selector == ta.SAFE_OWNERS_SELECTOR:
                    result = owners(self.safe_owners)
                elif selector == ta.SAFE_THRESHOLD_SELECTOR:
                    result = word(self.threshold)
                else:
                    assert address == ta.DERIVATION["proxy_factory"] and selector == ta.PROXY_IMPLEMENTATION_SELECTOR
                    result = word(self.proxy_implementation)
            rows.append({"jsonrpc": "2.0", "id": call["id"], "result": result})
        if self.change:
            return self.change(request, rows)
        # Batch responses may arrive in a different order.
        return httpx.Response(200, json=list(reversed(rows)))


async def load(rpc):
    return await ta.load_accounts(rpc.owner, transport=httpx.MockTransport(rpc), clock=lambda: NOW)


def test_known_canonical_addresses_match_current_sdk_vectors():
    assert ta.candidates(OWNER) == [
        {"address": "0x574548bc296a44a39a7828343fc262244f37a7e5", "wallet_type": "DEPOSIT_WALLET"},
        {"address": "0xfaea0f08159fcf2f573fe24e9e989b0d48f7651b", "wallet_type": "DEPOSIT_WALLET"},
        {"address": "0x6b503ad95d139be2a07cd0e8888d71c6403d9c9c", "wallet_type": "GNOSIS_SAFE"},
        {"address": "0xf537a2b3159593a425e2fa8f5ba3bd3080d4a18a", "wallet_type": "POLY_PROXY"},
    ]
    assert all(row["address"] != OWNER for row in ta.candidates(OWNER))
    assert {row["address"] for row in ta.candidates(OWNER)}.isdisjoint(row["address"] for row in ta.candidates(OTHER))


async def test_deployed_currently_controlled_accounts_are_returned_at_one_block():
    rpc = Rpc()
    result = await load(rpc)
    assert result == {"ok": True, "address": OWNER, "chain_id": 137,
                      "accounts": [{**row, "deployed": True} for row in rpc.possible], "complete": True}
    assert len(rpc.calls) == 15


async def test_no_deployed_wallet_is_an_empty_complete_list_not_an_eoa_fallback():
    rpc = Rpc()
    rpc.absent = set(rpc.records)
    assert (await load(rpc))["accounts"] == []
    assert not any(call["method"] in ("eth_call", "eth_getStorageAt") for call in rpc.calls)


async def test_changed_deposit_owner_is_excluded():
    rpc = Rpc()
    address = rpc.possible[0]["address"]
    rpc.owner_overrides[address] = OTHER
    result = await load(rpc)
    assert result["complete"] is True
    assert address not in {row["address"] for row in result["accounts"]}


@pytest.mark.parametrize("owners_list,threshold", [([OWNER], 2), ([OWNER, OTHER], 1), ([OTHER], 1), ([], 0)])
async def test_safe_requires_one_current_owner_and_threshold_one(owners_list, threshold):
    rpc = Rpc()
    rpc.safe_owners, rpc.threshold = owners_list, threshold
    assert all(row["wallet_type"] != "GNOSIS_SAFE" for row in (await load(rpc))["accounts"])


@pytest.mark.parametrize("field", ["proxy_owner", "proxy_implementation"])
async def test_proxy_requires_current_factory_control_and_implementation(field):
    rpc = Rpc()
    setattr(rpc, field, OTHER)
    assert all(row["wallet_type"] != "POLY_PROXY" for row in (await load(rpc))["accounts"])


@pytest.mark.parametrize("chain", ["0x1", "0x089", 137, None, True])
async def test_rpc_must_identify_polygon(chain):
    rpc = Rpc()
    rpc.chain = chain
    with pytest.raises(ta.AccountLookupError):
        await load(rpc)
    assert len(rpc.calls) == 2


@pytest.mark.parametrize("block", [{"number": "0x0", "timestamp": hex(NOW)},
                                  {"number": "0x123", "timestamp": hex(NOW - ta.MAX_BLOCK_AGE - 1)},
                                  {"number": "0x123", "timestamp": hex(NOW + 31)}, None])
async def test_missing_stale_or_future_block_fails_closed(block):
    rpc = Rpc()
    rpc.block = block
    with pytest.raises(ta.AccountLookupError):
        await load(rpc)


async def test_missing_canonical_factory_fails_closed():
    rpc = Rpc()
    rpc.absent.add(ta.DERIVATION["deposit_wallet_factory"])
    with pytest.raises(ta.AccountLookupError):
        await load(rpc)


@pytest.mark.parametrize("change", ["missing", "duplicate", "error", "wrongid", "boolid", "version", "shape", "null"])
async def test_partial_or_malformed_rpc_batch_is_never_a_partial_success(change):
    rpc = Rpc()

    def mutate(request, rows):
        if change == "missing":
            rows.pop()
        elif change == "duplicate":
            rows[1] = rows[0]
        elif change == "error":
            rows[0] = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}}
        elif change == "wrongid":
            rows[0]["id"] = 9000
        elif change == "boolid":
            rows[0]["id"] = True
        elif change == "version":
            rows[0]["jsonrpc"] = "1.0"
        elif change == "shape":
            rows = {}
        else:
            rows = None
        return httpx.Response(200, json=rows)

    rpc.change = mutate
    with pytest.raises(ta.AccountLookupError):
        await load(rpc)


@pytest.mark.parametrize("bad", ["0xabc", "nonhex", "0x" + "00" * (ta.MAX_CODE_BYTES + 1), None])
async def test_malformed_deployment_code_is_not_an_absent_account(bad):
    rpc = Rpc()

    def mutate(request, rows):
        if json.loads(request.content)[0]["method"] == "eth_getCode":
            rows[0]["result"] = bad
        return httpx.Response(200, json=rows)

    rpc.change = mutate
    with pytest.raises(ta.AccountLookupError):
        await load(rpc)


@pytest.mark.parametrize("bad", ["0x", "0x" + "ff" * 32, word(OWNER) + "00", None])
async def test_malformed_current_owner_fails_entire_lookup(bad):
    rpc = Rpc()

    def mutate(request, rows):
        if json.loads(request.content)[0]["method"] == "eth_call":
            rows[0]["result"] = bad
        return httpx.Response(200, json=rows)

    rpc.change = mutate
    with pytest.raises(ta.AccountLookupError):
        await load(rpc)


async def test_rpc_body_limit_and_redirects_are_enforced(monkeypatch):
    for response in [httpx.Response(200, content=b" " * (ta.MAX_RESPONSE_BYTES + 1)),
                     httpx.Response(302, headers={"Location": "https://evil.invalid"})]:
        requested = []

        def answer(request):
            requested.append(str(request.url))
            return response

        with pytest.raises((ta.AccountLookupError, httpx.HTTPStatusError)):
            await ta.load_accounts(OWNER, transport=httpx.MockTransport(answer))
        assert requested == [ta.RPC_URL]


async def test_total_lookup_deadline_is_bounded(monkeypatch):
    monkeypatch.setattr(ta, "LOOKUP_SECONDS", .01)

    async def slow(request):
        await asyncio.sleep(10)

    with pytest.raises(TimeoutError):
        await ta.load_accounts(OWNER, transport=httpx.MockTransport(slow))


@pytest.fixture
def auth(tmp_path):
    return WalletAuth(tmp_path / "wallet.sqlite3", allowed_origins=[ORIGIN], rate_allow=lambda _: True)


def client(auth, lookup):
    app = Starlette(routes=[*auth.routes(), ta.TradingAccounts(lookup).route(auth)])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.oddsrail.app",
                             headers={"Origin": ORIGIN})


async def login(web, account):
    challenge = await web.post("/auth/wallet/challenge", json={"address": account.address, "chain_id": 137})
    message = challenge.json()["message"]
    signature = "0x" + account.sign_message(encode_defunct(text=message)).signature.hex()
    response = await web.post("/auth/wallet/verify", json={"message": message, "signature": signature})
    assert response.status_code == 200, response.text


async def never_lookup(_):
    pytest.fail("untrusted caller reached account discovery")


async def test_route_requires_cookie_and_exact_allowed_origin(auth):
    async with client(auth, never_lookup) as web:
        assert (await web.post("/trading/accounts", json={})).status_code == 401
        response = await web.post("/trading/accounts", json={}, headers={"Origin": "http://localhost:9999"})
        assert response.status_code == 403 and "access-control-allow-origin" not in response.headers
        assert (await web.get("/trading/accounts")).status_code == 405


@pytest.mark.parametrize("body", [{"address": OTHER}, {"wallet": OTHER}, {"chain_id": 1}, [], None])
async def test_no_user_supplied_wallet_chain_or_other_fields(auth, body):
    async with client(auth, never_lookup) as web:
        await login(web, Account.create())
        response = await web.post("/trading/accounts", content=json.dumps(body), headers={"Content-Type": "application/json"})
        assert response.status_code == 400


async def test_complete_account_result_bound_to_session_and_not_cached(auth):
    account = Account.create()
    addresses = []

    async def lookup(owner):
        addresses.append(owner)
        return await load(Rpc(owner))

    async with client(auth, lookup) as web:
        await login(web, account)
        response = await web.post("/trading/accounts", json={})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["address"] == account.address and data["chain_id"] == 137 and data["complete"] is True
        assert addresses == [account.address.lower()]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["access-control-allow-credentials"] == "true"
        assert "set-cookie" not in response.headers
        assert (await web.post("/auth/wallet/logout", json={})).status_code == 200
        assert (await web.post("/trading/accounts", json={})).status_code == 401


@pytest.mark.parametrize("bad", ["otherowner", "partial", "otherchain", "claim", "duplicate", "missingaddress"])
async def test_corrupt_or_partial_loader_output_is_a_controlled_failure(auth, bad):
    async def lookup(owner):
        result = {"ok": True, "address": owner, "chain_id": 137, "complete": True,
                  "accounts": [{**row, "deployed": True} for row in ta.candidates(owner)]}
        if bad == "otherowner":
            result["address"] = OTHER
        elif bad == "partial":
            result["complete"] = False
        elif bad == "otherchain":
            result["chain_id"] = 1
        elif bad == "claim":
            result["accounts"][0]["address"] = OTHER
        elif bad == "duplicate":
            result["accounts"][1] = result["accounts"][0]
        else:
            result["accounts"] = [{"deployed": True}]
        return result

    async with client(auth, lookup) as web:
        await login(web, Account.create())
        response = await web.post("/trading/accounts", json={})
        assert response.status_code == 503 and response.json()["error"] == "trading_accounts_unavailable"
        assert "accounts" not in response.json()


async def test_logout_during_lookup_prevents_stale_success(auth):
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def lookup(owner):
        entered.set()
        await proceed.wait()
        return {"ok": True, "address": owner, "chain_id": 137, "complete": True, "accounts": []}

    async with client(auth, lookup) as web:
        await login(web, Account.create())
        pending = asyncio.create_task(web.post("/trading/accounts", json={}))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert (await web.post("/auth/wallet/logout", json={})).status_code == 200
        proceed.set()
        assert (await pending).status_code == 401
