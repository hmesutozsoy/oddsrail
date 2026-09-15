"""Readiness checks bind to signed-in owners and never become trading permission.

All wallet keys are disposable and all public-data responses are offline fakes.
"""

import asyncio
from copy import deepcopy
import json
import sqlite3

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from starlette.applications import Starlette

from oddsrail.cloud import activation
from oddsrail.cloud.read_admission import ReadAdmission, ReadBusy
from oddsrail.cloud.wallet_auth import SESSION_TTL, WalletAuth

ORIGIN = "https://oddsrail.app"
OTHER_ORIGIN = "https://www.oddsrail.app"
API = "https://mcp.oddsrail.app"
TRADING_ADDRESS = "0x" + "b" * 40


@pytest.fixture
def config():
    return {"schema_version": 2, "mode": "draft", "market_mode": "universe",
            "market_ids": [], "topics": ["crypto"], "keyword": "",
            "bankroll": 1000, "perorder": 25, "maxpos": 5, "closing": 2,
            "minvol": 20000, "on": {"mm": True, "daily": True, "expo": True},
            "vals": {"mm": {"edge": 2, "shares": 20}, "daily": {"usd": 30},
                     "expo": {"usd": 100}}}


@pytest.fixture
def auth(tmp_path):
    return WalletAuth(tmp_path / "wallet.sqlite3", allowed_origins=[ORIGIN, OTHER_ORIGIN],
                      rate_allow=lambda _: True)


def profile(owner, status="resolved"):
    return {"ok": True, "address": owner,
            "account": {"status": status, "connected_address": owner,
                        "trading_address": TRADING_ADDRESS if status == "resolved" else None,
                        "source": "polymarket_public_profile" if status == "resolved" else None},
            "summary": {"cash_usd": None}, "positions": {"items": []}}


def client(auth, loader, *, origin=ORIGIN, cookies=None):
    route = activation.ActivationCheck(loader).route(auth)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=Starlette(routes=[*auth.routes(), route])),
                             base_url=API, headers={"Origin": origin}, cookies=cookies)


async def login(web, account):
    response = await web.post("/auth/wallet/challenge", json={"address": account.address, "chain_id": 137})
    assert response.status_code == 200, response.text
    message = response.json()["message"]
    signed = "0x" + account.sign_message(encode_defunct(text=message)).signature.hex()
    response = await web.post("/auth/wallet/verify", json={"message": message, "signature": signed})
    assert response.status_code == 200, response.text


async def never_load(_):
    pytest.fail("an invalid or unauthenticated request reached public account discovery")


def checks(data):
    return {row["id"]: row for row in data["checks"]}


async def test_real_wallet_signature_binds_readiness_and_does_not_trade(auth, config, monkeypatch):
    from oddsrail import paper, trading
    from oddsrail.cloud import runner

    def never_execute(*args, **kwargs):
        pytest.fail("readiness attempted an execution, key lookup or paper write")

    for module, names in ((paper, ["save", "simulate_polymarket"]),
                          (trading, ["_client", "place_order", "cancel_order"]),
                          (runner, ["run_pass"])):
        for name in names:
            monkeypatch.setattr(module, name, never_execute)
    account = Account.create()
    loaded = []

    async def load(owner):
        loaded.append(owner)
        return profile(owner)

    before = deepcopy(config)
    async with client(auth, load) as web:
        await login(web, account)
        response = await web.post("/activation/check", json={"config": config})
    assert response.status_code == 200, response.text
    data = response.json()
    assert config == before
    assert loaded == [account.address.lower()]
    assert data["ok"] is True and data["address"] == account.address
    assert data["can_activate"] is False and data["checked_at"]
    rows = checks(data)
    assert set(rows) == {"wallet", "configuration", "trading_account", "funding", "permission", "execution"}
    assert [rows[k]["status"] for k in ("wallet", "configuration", "trading_account")] == ["ready"] * 3
    assert all(rows[k]["status"] == "unavailable" for k in ("funding", "permission", "execution"))
    assert "ownership and compatibility still need verification" in rows["trading_account"]["detail"]
    assert "eligibility has not been checked" in rows["configuration"]["detail"]
    assert data["blockers"] == ["funding", "permission", "execution"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["vary"] == "Origin"
    assert "set-cookie" not in response.headers
    with sqlite3.connect(auth.db_path) as db:
        assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} == {
            "wallet_sessions", "wallet_challenges"}


async def test_missing_cookie_and_bearer_email_or_claimed_address_cannot_authenticate(auth, config):
    async with client(auth, never_load) as web:
        response = await web.post("/activation/check", json={"config": config},
                                  headers={"Authorization": "Bearer arbitrary-email-session"})
        assert response.status_code == 401
        assert response.json()["error"] == "wallet_sign_in_required"
        response = await web.post("/activation/check", json={"config": config, "address": Account.create().address})
        assert response.status_code == 401


@pytest.mark.parametrize("origin", ["", "null", "https://oddsrail.app.evil.invalid", "http://127.0.0.1:9999"])
async def test_unknown_origins_never_inherit_public_route_cors(auth, config, origin):
    async with client(auth, never_load, origin=origin) as web:
        response = await web.post("/activation/check", json={"config": config})
        assert response.status_code == 403 and response.json()["error"] == "origin_not_allowed"
        assert "access-control-allow-origin" not in response.headers


async def test_approved_other_origin_cannot_reuse_cookie_and_duplicate_origin_is_rejected(auth, config):
    async with client(auth, never_load) as web:
        await login(web, Account.create())
        response = await web.post("/activation/check", json={"config": config}, headers={"Origin": OTHER_ORIGIN})
        assert response.status_code == 401
        response = await web.post("/activation/check", json={"config": config},
                                  headers=[("Origin", ORIGIN), ("Origin", ORIGIN)])
        assert response.status_code == 403


async def test_strict_preflight_requires_post_and_only_content_type(auth):
    async with client(auth, never_load) as web:
        response = await web.options("/activation/check", headers={
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
        assert response.status_code == 204
        assert response.headers["access-control-allow-methods"] == "POST"
        assert response.headers["access-control-allow-credentials"] == "true"
        for headers in ({"Access-Control-Request-Method": "GET"},
                        {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"}):
            assert (await web.options("/activation/check", headers=headers)).status_code == 403
        assert (await web.get("/activation/check")).status_code == 405


@pytest.mark.parametrize("body", [[], None, "config", 1, {"config": {}, "address": "0x" + "a" * 40},
                                 {"config": {}, "can_activate": True}, {"config": {}, "checks": []}])
async def test_only_one_config_body_field_is_accepted(auth, body):
    async with client(auth, never_load) as web:
        await login(web, Account.create())
        response = await web.post("/activation/check", content=json.dumps(body),
                                  headers={"Content-Type": "application/json"})
        assert response.status_code == 400 and response.json()["error"] == "invalid_body"


@pytest.mark.parametrize("key,value", [("mode", "live"), ("mode", "paper"), ("mode", None),
                                     ("perorder", 5000), ("bankroll", 1), ("on", {}),
                                     ("topics", []), ("market_ids", ["not-a-token"])])
async def test_invalid_draft_does_not_start_account_reads(auth, config, key, value):
    config[key] = value
    if key == "market_ids":
        config["market_mode"] = "specific"
    async with client(auth, never_load) as web:
        await login(web, Account.create())
        response = await web.post("/activation/check", json={"config": config})
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_configuration" and response.json()["detail"]


@pytest.mark.parametrize("value", [None, [], 1, "configuration"])
async def test_config_itself_must_be_an_object(auth, value):
    async with client(auth, never_load) as web:
        await login(web, Account.create())
        response = await web.post("/activation/check", json={"config": value})
        assert response.status_code == 400 and response.json()["error"] == "invalid_configuration"


@pytest.mark.parametrize("raw,status", [
    (b'{"config":{},"config":{}}', 400), (b'{"config":{"bankroll":NaN}}', 400),
    (b'{"config":' + b'[' * 2000 + b']' * 2000 + b'}', 400),
    (b'{}' + b' ' * activation.MAX_BODY_BYTES, 413),
])
async def test_stream_and_json_encoding_limits(auth, raw, status):
    async def stream():
        for offset in range(0, len(raw), 500):
            yield raw[offset:offset + 500]

    async with client(auth, never_load) as web:
        await login(web, Account.create())
        response = await web.post("/activation/check", content=stream(), headers={"Content-Type": "application/json"})
        assert response.status_code == status, response.text


async def test_declared_body_limit_and_content_type_fail_before_read(auth, config):
    async with client(auth, never_load) as web:
        await login(web, Account.create())
        response = await web.post("/activation/check", json={"config": config},
                                  headers={"Content-Length": str(activation.MAX_BODY_BYTES + 1)})
        assert response.status_code == 413
        response = await web.post("/activation/check", content=b"{}", headers={"Content-Type": "text/plain"})
        assert response.status_code == 415


async def test_body_between_auth_default_and_activation_limit_is_accepted(auth, config):
    async def load(owner):
        return profile(owner)

    async with client(auth, load) as web:
        await login(web, Account.create())
        body = json.dumps({"config": config}).encode() + b" " * 9000
        assert len(body) < activation.MAX_BODY_BYTES
        response = await web.post("/activation/check", content=body, headers={"Content-Type": "application/json"})
        assert response.status_code == 200, response.text


async def test_slow_request_body_has_deadline(auth, monkeypatch):
    original = asyncio.wait_for

    async def shorter(awaitable, timeout):
        assert timeout == 5
        return await original(awaitable, timeout=0.01)

    async def slow():
        yield b"{"
        await asyncio.sleep(10)

    async with client(auth, never_load) as web:
        await login(web, Account.create())
        monkeypatch.setattr(asyncio, "wait_for", shorter)
        response = await web.post("/activation/check", content=slow(), headers={"Content-Type": "application/json"})
        assert response.status_code == 408


async def test_expired_and_revoked_session_fail_before_public_lookup(auth, config):
    now = [1800000000]
    auth._clock = lambda: now[0]
    async with client(auth, never_load) as web:
        await login(web, Account.create())
        now[0] += SESSION_TTL
        assert (await web.post("/activation/check", json={"config": config})).status_code == 401
        await login(web, Account.create())
        stale_cookies = httpx.Cookies(web.cookies)
        assert (await web.post("/auth/wallet/logout", json={})).status_code == 200
        async with client(auth, never_load, cookies=stale_cookies) as stale:
            assert (await stale.post("/activation/check", json={"config": config})).status_code == 401


@pytest.mark.parametrize("change", ["expire", "logout"])
async def test_session_is_rechecked_after_account_discovery(auth, config, change):
    entered, proceed = asyncio.Event(), asyncio.Event()
    now = [1800000000]
    auth._clock = lambda: now[0]

    async def load(owner):
        entered.set()
        await proceed.wait()
        return profile(owner)

    async with client(auth, load) as web:
        await login(web, Account.create())
        pending = asyncio.create_task(web.post("/activation/check", json={"config": config}))
        await asyncio.wait_for(entered.wait(), timeout=1)
        if change == "expire":
            now[0] += SESSION_TTL
        else:
            assert (await web.post("/auth/wallet/logout", json={})).status_code == 200
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=1)
        assert response.status_code == 401 and response.json()["error"] == "wallet_sign_in_required"


@pytest.mark.parametrize("status,expected,blocker", [
    ("unresolved", "action_required", "trading_account"),
    ("error", "unavailable", "trading_account"),
    ("anything_else", "unavailable", "trading_account"),
])
async def test_missing_profile_offers_setup_but_failed_lookup_offers_retry(auth, config, status, expected, blocker):
    async def load(owner):
        return profile(owner, status)

    async with client(auth, load) as web:
        await login(web, Account.create())
        response = await web.post("/activation/check", json={"config": config})
    assert response.status_code == 200
    data = response.json()
    row = checks(data)["trading_account"]
    assert row["status"] == expected and data["blockers"][0] == blocker
    assert data["can_activate"] is False and len(data["checks"]) == 6
    if status == "unresolved":
        assert row["href"] == "https://polymarket.com/" and "same wallet" in row["detail"]
    else:
        assert "href" not in row and "Refresh and try again" in row["detail"]


@pytest.mark.parametrize("part", ["address", "connected_address", "trading_address", "source", "shape"])
async def test_public_account_result_must_belong_to_signed_in_owner(auth, config, part):
    async def load(owner):
        result = profile(owner)
        if part == "address":
            result["address"] = Account.create().address
        elif part == "connected_address":
            result["account"]["connected_address"] = Account.create().address
        elif part == "trading_address":
            result["account"]["trading_address"] = "0x0"
        elif part == "source":
            result["account"]["source"] = "browser_claim"
        else:
            result = []
        return result

    async with client(auth, load) as web:
        account = Account.create()
        await login(web, account)
        data = (await web.post("/activation/check", json={"config": config})).json()
        assert data["address"] == account.address
        assert checks(data)["trading_account"]["status"] == "unavailable"
        assert "trading_account" in data["blockers"]


@pytest.mark.parametrize("failure", ["busy", "exception", "timeout"])
async def test_public_read_failure_is_bounded_and_keeps_all_live_blockers(auth, config, monkeypatch, failure):
    async def load(owner):
        if failure == "busy":
            raise ReadBusy()
        if failure == "exception":
            raise RuntimeError("untrusted upstream error with sensitive implementation details")
        await asyncio.sleep(10)

    monkeypatch.setattr(activation, "ACCOUNT_TIMEOUT_SECONDS", .01)
    async with client(auth, load) as web:
        await login(web, Account.create())
        data = (await web.post("/activation/check", json={"config": config})).json()
        assert checks(data)["trading_account"]["status"] == "unavailable"
        assert data["can_activate"] is False and len(data["checks"]) == 6
        assert "sensitive" not in json.dumps(data)


async def test_client_claims_and_environment_cannot_enable_execution(auth, config, monkeypatch):
    for key in ("ODDSRAIL_DRY_RUN", "ODDSRAIL_HOSTED"):
        monkeypatch.setenv(key, "0")
    for key in ("ODDSRAIL_HOSTED_LIVE", "ODDSRAIL_SESSION_AUTHORIZATION"):
        monkeypatch.setenv(key, "1")
    config.update(can_activate=True, authenticated=True, address=Account.create().address,
                  permission={"authorized": True}, cash=10000)

    async def load(owner):
        result = profile(owner)
        result.update(can_activate=True, permission={"authorized": True}, execution="ready")
        result["summary"] = {"cash_usd": 10000, "cash_status": "verified"}
        return result

    async with client(auth, load) as web:
        account = Account.create()
        await login(web, account)
        data = (await web.post("/activation/check", json={"config": config})).json()
        assert data["address"] == account.address and data["can_activate"] is False
        assert all(checks(data)[key]["status"] == "unavailable" for key in ("funding", "permission", "execution"))
        assert (await web.post("/activation/activate", json={"config": config})).status_code == 404


async def test_activation_rate_limit_does_not_consume_logout_budget(tmp_path, config):
    auth = WalletAuth(tmp_path / "wallet.sqlite3", allowed_origins=[ORIGIN])
    async with client(auth, never_load) as web:
        for _ in range(30):
            assert (await web.post("/activation/check", json={"config": config})).status_code == 401
        response = await web.post("/activation/check", json={"config": config})
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        assert (await web.post("/auth/wallet/logout", json={})).status_code == 200
        assert (await web.post("/auth/wallet/challenge", json={"address": Account.create().address, "chain_id": 137})).status_code == 200


async def test_shared_admission_survives_disconnect_until_account_work_finishes(tmp_path, config):
    auth = WalletAuth(tmp_path / "wallet.sqlite3", allowed_origins=[ORIGIN],
                      rate_allow=lambda _: True, max_active_requests=1)
    entered, proceed, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def load(owner):
        entered.set()
        await proceed.wait()
        return profile(owner)

    async def handler(identity, body):
        try:
            return await activation.ActivationCheck(load).check(identity, body)
        finally:
            finished.set()

    service = activation.ActivationCheck(load)
    service.check = handler
    app = Starlette(routes=[*auth.routes(), service.route(auth)])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API,
                                 headers={"Origin": ORIGIN}) as web:
        await login(web, Account.create())
        pending = asyncio.create_task(web.post("/activation/check", json={"config": config}))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert (await web.get("/auth/wallet/session")).status_code == 503
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert (await web.get("/auth/wallet/session")).status_code == 503
        proceed.set()
        await asyncio.wait_for(finished.wait(), timeout=1)
        # The wrapper still rechecks its database before releasing the slot.
        for _ in range(100):
            response = await web.get("/auth/wallet/session")
            if response.status_code != 503:
                break
            await asyncio.sleep(.001)
        assert response.status_code == 200


async def test_account_read_uses_shared_public_capacity(auth, config):
    admission = ReadAdmission(max_active=1)
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def public_work():
        entered.set()
        await proceed.wait()
        return {"ok": True}

    async def load(owner):
        return await admission.get("portfolio:" + owner, lambda: never_load(owner), ttl=0, cache_if=lambda _: False)

    other = asyncio.create_task(admission.get("markets:existing", public_work, ttl=0, cache_if=lambda _: False))
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        async with client(auth, load) as web:
            await login(web, Account.create())
            data = (await web.post("/activation/check", json={"config": config})).json()
            assert checks(data)["trading_account"]["status"] == "unavailable"
            assert data["can_activate"] is False
    finally:
        proceed.set()
        await other
