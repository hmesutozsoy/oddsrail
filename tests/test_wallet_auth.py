"""Wallet proof, browser binding, replay/revocation and admission boundaries.

All signatures use disposable test keys; no network, wallet extension, funds,
or venue credentials are involved.
"""

import asyncio
import hashlib
import sqlite3
import threading

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from starlette.applications import Starlette

from oddsrail.cloud.wallet_auth import (CHALLENGE_TTL, MAX_BODY_BYTES, SESSION_TTL,
                                       STATEMENT, WalletAuth)

ORIGIN = "https://oddsrail.app"
OTHER_ORIGIN = "https://www.oddsrail.app"
API = "https://mcp.oddsrail.app"


@pytest.fixture
def auth(tmp_path):
    return WalletAuth(tmp_path / "wallet.sqlite3", allowed_origins=[ORIGIN, OTHER_ORIGIN],
                      rate_allow=lambda _: True)


def client(auth, *, cookies=None, origin=ORIGIN):
    if isinstance(cookies, dict):
        scoped = httpx.Cookies()
        for key, value in cookies.items():
            scoped.set(key, value, domain="mcp.oddsrail.app", path="/")
        cookies = scoped
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=Starlette(routes=auth.routes())),
                             base_url=API, headers={"Origin": origin}, cookies=cookies)


async def challenge(web, account, chain_id=137):
    response = await web.post("/auth/wallet/challenge", json={"address": account.address,
                                                             "chain_id": chain_id})
    assert response.status_code == 200, response.text
    return response.json()


def sign(account, message):
    return "0x" + account.sign_message(encode_defunct(text=message)).signature.hex()


async def verify(web, account, payload):
    return await web.post("/auth/wallet/verify", json={
        "message": payload["message"], "signature": sign(account, payload["message"])})


async def login(web, account):
    payload = await challenge(web, account)
    response = await verify(web, account, payload)
    assert response.status_code == 200, response.text
    return response


async def test_real_signature_creates_only_hashed_non_sliding_wallet_session(auth):
    now = [1800000000]
    auth._clock = lambda: now[0]
    account = Account.create()
    async with client(auth) as web:
        payload = await challenge(web, account)
        lines = payload["message"].split("\n")
        assert len(lines) == 11
        assert lines[0] == ORIGIN + " wants you to sign in with your Ethereum account:"
        assert lines[1] == account.address
        assert lines[3] == STATEMENT
        assert lines[5:8] == ["URI: " + ORIGIN + "/", "Version: 1", "Chain ID: 137"]
        nonce = lines[8].removeprefix("Nonce: ")
        assert len(nonce) == 32 and nonce.isalnum()
        assert payload["expires_at"] == now[0] + CHALLENGE_TTL
        response = await verify(web, account, payload)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data == {"ok": True, "authenticated": True, "address": account.address,
                        "chain_id": 137, "expires_at": now[0] + SESSION_TTL}
        assert "token" not in data and "signature" not in data
        for cookie in response.headers.get_list("set-cookie"):
            assert "__Host-" in cookie and "HttpOnly" in cookie and "Secure" in cookie
            assert "SameSite=strict" in cookie and "Path=/" in cookie and "Domain=" not in cookie
        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert response.headers["access-control-allow-credentials"] == "true"
        assert response.headers["cache-control"] == "no-store"
        now[0] += 300
        assert (await web.get("/auth/wallet/session")).json() == data
        session_token = web.cookies.get(auth.session_cookie)
        browser_token = web.cookies.get(auth.preauth_cookie)
        with sqlite3.connect(auth.db_path) as db:
            row = db.execute("SELECT token_hash, browser_hash FROM wallet_sessions").fetchone()
            assert row == (hashlib.sha256(session_token.encode()).hexdigest(),
                           hashlib.sha256(browser_token.encode()).hexdigest())
            assert db.execute("SELECT COUNT(*) FROM wallet_challenges").fetchone()[0] == 0
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert names == {"wallet_sessions", "wallet_challenges"}
        now[0] = data["expires_at"]
        assert (await web.get("/auth/wallet/session")).json() == {"ok": True, "authenticated": False}


async def test_replay_and_copied_proof_without_browser_cookie_fail(auth):
    account = Account.create()
    async with client(auth) as web, client(auth) as thief:
        payload = await challenge(web, account)
        assert (await verify(thief, account, payload)).status_code == 401
        assert (await verify(web, account, payload)).status_code == 200
        assert (await verify(web, account, payload)).status_code == 401


async def test_two_independent_connections_cannot_consume_same_challenge(auth, monkeypatch):
    account = Account.create()
    other = WalletAuth(auth.db_path, allowed_origins=[ORIGIN], rate_allow=lambda _: True)
    barrier = threading.Barrier(2)
    original = Account.recover_message

    def recover(*args, **kwargs):
        barrier.wait(timeout=5)
        return original(*args, **kwargs)

    async with client(auth) as web:
        payload = await challenge(web, account)
        async with client(other, cookies=web.cookies) as second:
            monkeypatch.setattr(Account, "recover_message", recover)
            results = await asyncio.gather(verify(web, account, payload), verify(second, account, payload))
    assert sorted(r.status_code for r in results) == [200, 401]
    with sqlite3.connect(auth.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallet_sessions").fetchone()[0] == 1


async def test_challenge_expiry_checked_again_after_signature_recovery(auth, monkeypatch):
    now = [1800000000]
    auth._clock = lambda: now[0]
    account = Account.create()
    original = Account.recover_message

    def recover(*args, **kwargs):
        now[0] += CHALLENGE_TTL
        return original(*args, **kwargs)

    async with client(auth) as web:
        payload = await challenge(web, account)
        monkeypatch.setattr(Account, "recover_message", recover)
        assert (await verify(web, account, payload)).status_code == 401


@pytest.mark.parametrize("replacement", ["origin", "uri", "chain", "address", "statement", "nonce"])
async def test_message_must_equal_server_challenge_even_with_valid_signature(auth, replacement):
    account = Account.create()
    async with client(auth) as web:
        payload = await challenge(web, account)
        before, after = {
            "origin": (ORIGIN + " wants", "https://evil.example wants"),
            "uri": ("URI: " + ORIGIN + "/", "URI: https://evil.example/"),
            "chain": ("Chain ID: 137", "Chain ID: 1"),
            "address": (account.address, Account.create().address),
            "statement": (STATEMENT, "Transfer funds."),
            "nonce": ("Nonce: ", "Nonce: abc"),
        }[replacement]
        payload["message"] = payload["message"].replace(before, after)
        assert (await verify(web, account, payload)).status_code == 401


async def test_wrong_signer_and_contract_format_signature_rejected(auth):
    account = Account.create()
    async with client(auth) as web:
        payload = await challenge(web, account)
        response = await verify(web, Account.create(), payload)
        assert response.status_code == 401 and response.json()["error"] == "invalid_signature"
        response = await web.post("/auth/wallet/verify", json={"message": payload["message"],
                                                              "signature": "0x" + "00" * 200})
        assert response.status_code == 400 and response.json()["error"] == "invalid_signature"
        # A malformed 65-byte ECDSA signature must be a controlled error too.
        response = await web.post("/auth/wallet/verify", json={"message": payload["message"],
                                                              "signature": "0x" + "00" * 65})
        assert response.status_code == 401 and response.json()["error"] == "invalid_signature"


@pytest.mark.parametrize("address,chain,error", [
    ("0x123", 137, "invalid_address"),
    (None, 137, "invalid_address"),
    ([], 137, "invalid_address"),
    ("0x" + "0" * 40, 137, "invalid_address"),
    ("0x" + "a" * 40, True, "unsupported_chain"),
    ("0x" + "a" * 40, "137", "unsupported_chain"),
    ("0x" + "a" * 40, 8453, "unsupported_chain"),
])
async def test_invalid_wallet_input(auth, address, chain, error):
    async with client(auth) as web:
        response = await web.post("/auth/wallet/challenge", json={"address": address, "chain_id": chain})
        assert response.status_code == 400 and response.json()["error"] == error


async def test_ethereum_mainnet_supported_and_lowercase_canonicalized(auth):
    account = Account.create()
    async with client(auth) as web:
        response = await web.post("/auth/wallet/challenge", json={"address": account.address.lower(), "chain_id": 1})
        assert account.address in response.json()["message"]
        response = await verify(web, account, response.json())
        assert response.status_code == 200 and response.json()["chain_id"] == 1


@pytest.mark.parametrize("origin", ["", "null", "https://evil.example", "https://oddsrail.app.evil.example",
                                   ORIGIN + "/", "http://oddsrail.app", "http://localhost:8898"])
async def test_all_routes_require_exact_origin(auth, origin):
    async with client(auth, origin=origin) as web:
        if origin == "":
            del web.headers["origin"]
        for path in ("challenge", "verify", "logout", "session"):
            response = (await web.get("/auth/wallet/session") if path == "session" else
                        await web.post("/auth/wallet/" + path, json={}))
            assert response.status_code == 403
            assert "access-control-allow-origin" not in response.headers
            assert "access-control-allow-credentials" not in response.headers


async def test_allowed_sibling_origin_cannot_use_another_origin_challenge_or_session(auth):
    account = Account.create()
    async with client(auth) as web:
        payload = await challenge(web, account)
        async with client(auth, cookies=web.cookies, origin=OTHER_ORIGIN) as sibling:
            assert (await verify(sibling, account, payload)).status_code == 401
        await verify(web, account, payload)
        async with client(auth, cookies=web.cookies, origin=OTHER_ORIGIN) as sibling:
            assert not (await sibling.get("/auth/wallet/session")).json()["authenticated"]


async def test_preflight_is_credentialed_and_strict(auth):
    async with client(auth) as web:
        response = await web.options("/auth/wallet/challenge", headers={
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
        assert response.status_code == 204
        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert response.headers["access-control-allow-credentials"] == "true"
        assert response.headers["access-control-allow-methods"] == "POST"
        for method, header in [("DELETE", "content-type"), ("POST", "authorization"), ("POST", "x-custom")]:
            response = await web.options("/auth/wallet/challenge", headers={
                "Access-Control-Request-Method": method, "Access-Control-Request-Headers": header})
            assert response.status_code == 403


@pytest.mark.parametrize("body,content_type,code", [
    (b"{}", "text/plain", 415),
    (b"{", "application/json", 400),
    (b"[]", "application/json", 400),
    (b'{"address":1,"address":2,"chain_id":137}', "application/json", 400),
    (b'{"address":NaN,"chain_id":137}', "application/json", 400),
    (b"{}" + b" " * MAX_BODY_BYTES, "application/json", 413),
])
async def test_malformed_body_is_bounded_and_rejected(auth, body, content_type, code):
    async with client(auth) as web:
        response = await web.post("/auth/wallet/challenge", content=body, headers={"Content-Type": content_type})
        assert response.status_code == code


async def test_stream_limit_without_content_length(auth):
    async def chunks():
        yield b"{" + b" " * 4000
        yield b" " * 4000
        yield b" " * 4000
        raise AssertionError("oversized stream must stop before asking for more chunks")

    async with client(auth) as web:
        response = await web.post("/auth/wallet/challenge", content=chunks(),
                                  headers={"Content-Type": "application/json"})
        assert response.status_code == 413


async def test_rate_limit_runs_before_body_or_crypto(tmp_path):
    auth = WalletAuth(tmp_path / "wallet.sqlite3", allowed_origins=[ORIGIN], rate_allow=lambda _: False)

    async def chunks():
        raise AssertionError("throttled request must not read body")
        yield b""

    async with client(auth) as web:
        response = await web.post("/auth/wallet/verify", content=chunks(),
                                  headers={"Content-Type": "application/json"})
        assert response.status_code == 429 and response.headers["retry-after"] == "60"


async def test_logout_revokes_session_and_pending_challenge(auth):
    account = Account.create()
    async with client(auth) as web:
        await login(web, account)
        old_cookies = dict(web.cookies)
        pending = await challenge(web, account)
        response = await web.post("/auth/wallet/logout", json={})
        assert response.status_code == 200 and not response.json()["authenticated"]
        async with client(auth, cookies=old_cookies) as stale:
            assert (await verify(stale, account, pending)).status_code == 401
            assert not (await stale.get("/auth/wallet/session")).json()["authenticated"]


async def test_logout_during_crypto_prevents_session_creation(auth, monkeypatch):
    account = Account.create()
    entered, proceed = threading.Event(), threading.Event()
    original = Account.recover_message

    def recover(*args, **kwargs):
        entered.set()
        assert proceed.wait(timeout=5)
        return original(*args, **kwargs)

    async with client(auth) as web:
        payload = await challenge(web, account)
        async with client(auth, cookies=web.cookies) as second:
            monkeypatch.setattr(Account, "recover_message", recover)
            verifying = asyncio.create_task(verify(web, account, payload))
            assert await asyncio.to_thread(entered.wait, 5)
            assert (await second.post("/auth/wallet/logout", json={})).status_code == 200
            proceed.set()
            assert (await verifying).status_code == 401


@pytest.mark.parametrize("second_origin", [ORIGIN, OTHER_ORIGIN])
async def test_delayed_verify_response_cannot_restore_session_after_other_tab_logout(auth, second_origin):
    account = Account.create()
    async with client(auth) as web:
        payload = await challenge(web, account)
        browser_cookie = dict(web.cookies)
        # Save the successful response as though its Set-Cookie headers are
        # still in flight, while another tab only has the browser context.
        delayed = await verify(web, account, payload)
        delayed_cookies = dict(web.cookies)
        async with client(auth, cookies=browser_cookie, origin=second_origin) as second:
            next_payload = await challenge(second, account)
            assert second.cookies.get(auth.preauth_cookie) == browser_cookie[auth.preauth_cookie]
            assert (await second.post("/auth/wallet/logout", json={})).status_code == 200
        async with client(auth, cookies=delayed_cookies) as delayed_browser:
            assert delayed.status_code == 200
            assert not (await delayed_browser.get("/auth/wallet/session")).json()["authenticated"]
            assert (await verify(delayed_browser, account, next_payload)).status_code == 401


async def test_new_challenge_supersedes_earlier_and_login_rotates_session(auth):
    account = Account.create()
    async with client(auth) as web:
        first = await challenge(web, account)
        second = await challenge(web, account)
        assert (await verify(web, account, first)).status_code == 401
        assert (await verify(web, account, second)).status_code == 200
        old_cookies = dict(web.cookies)
        await login(web, account)
        assert old_cookies[auth.session_cookie] != web.cookies.get(auth.session_cookie)
        assert old_cookies[auth.preauth_cookie] == web.cookies.get(auth.preauth_cookie)
        async with client(auth, cookies=old_cookies) as stale:
            assert not (await stale.get("/auth/wallet/session")).json()["authenticated"]


async def test_client_chosen_browser_cookie_cannot_fixate_session(auth):
    account = Account.create()
    chosen = "a" * 43
    async with client(auth, cookies={auth.preauth_cookie: chosen}) as web:
        await challenge(web, account)
        cookies = [cookie for cookie in web.cookies.jar if cookie.domain == "mcp.oddsrail.app"]
        assert next(cookie.value for cookie in cookies if cookie.name == auth.preauth_cookie) != chosen


async def test_capacity_prunes_expiry_and_fails_closed(tmp_path):
    now = [1800000000]
    auth = WalletAuth(tmp_path / "wallet.sqlite3", allowed_origins=[ORIGIN],
                      rate_allow=lambda _: True, clock=lambda: now[0], max_challenges=1, max_sessions=1)
    account = Account.create()
    async with client(auth) as first, client(auth) as second:
        await challenge(first, account)
        response = await second.post("/auth/wallet/challenge", json={"address": account.address, "chain_id": 137})
        assert response.status_code == 503 and response.json()["error"] == "auth_capacity"
        now[0] += CHALLENGE_TTL
        second_payload = await challenge(second, account)
        assert (await verify(second, account, second_payload)).status_code == 200
        first_payload = await challenge(first, account)
        response = await verify(first, account, first_payload)
        assert response.status_code == 503 and response.json()["error"] == "auth_capacity"
        # A failed capacity transaction must not silently evict the live login.
        assert (await second.get("/auth/wallet/session")).json()["authenticated"]
        now[0] += SESSION_TTL
        await login(first, account)
        with sqlite3.connect(auth.db_path) as db:
            assert db.execute("SELECT COUNT(*) FROM wallet_sessions").fetchone()[0] == 1


def test_secure_default_and_explicit_loopback_development_only(tmp_path):
    for origin in ["http://oddsrail.app", "http://127.0.0.1:8898", ORIGIN + "/", ORIGIN + "?x=1",
                   "https://name:pass@oddsrail.app", "null"]:
        with pytest.raises(ValueError):
            WalletAuth(tmp_path / "bad.sqlite3", allowed_origins=[origin])
    local = WalletAuth(tmp_path / "dev.sqlite3", allowed_origins=["http://127.0.0.1:8898"], dev=True)
    assert local.session_cookie == "oddsrail-wallet-session"
    with pytest.raises(ValueError):
        WalletAuth(tmp_path / "bad.sqlite3", allowed_origins=["http://evil.example"], dev=True)


@pytest.mark.parametrize("length,status", [("-1", 400), ("oops", 400), ("999999999999", 413)])
async def test_content_length_checked_before_stream(auth, length, status):
    async with client(auth) as web:
        response = await web.post("/auth/wallet/challenge", content=b"{}", headers={
            "Content-Type": "application/json", "Content-Length": length})
        assert response.status_code == status


async def test_slow_stream_has_bounded_read_timeout(auth, monkeypatch):
    original = asyncio.wait_for

    async def shorter(awaitable, timeout):
        assert timeout == 5
        return await original(awaitable, timeout=0.01)

    async def slow():
        yield b"{"
        await asyncio.sleep(10)
        yield b"}"

    monkeypatch.setattr(asyncio, "wait_for", shorter)
    async with client(auth) as web:
        response = await web.post("/auth/wallet/challenge", content=slow(),
                                  headers={"Content-Type": "application/json"})
        assert response.status_code == 408 and response.json()["error"] == "body_timeout"


async def test_session_survives_app_restart(auth):
    account = Account.create()
    async with client(auth) as web:
        expected = (await login(web, account)).json()
        restarted = WalletAuth(auth.db_path, allowed_origins=[ORIGIN])
        async with client(restarted, cookies=web.cookies) as after_restart:
            assert (await after_restart.get("/auth/wallet/session")).json() == expected


async def test_read_admission_cannot_exhaust_logout_or_signing_budget(tmp_path):
    auth = WalletAuth(tmp_path / "wallet.sqlite3", allowed_origins=[ORIGIN])
    async with client(auth) as web:
        for _ in range(120):
            assert (await web.get("/auth/wallet/session")).status_code == 200
        assert (await web.get("/auth/wallet/session")).status_code == 429
        assert (await web.post("/auth/wallet/logout", json={})).status_code == 200
        assert (await web.post("/auth/wallet/challenge", json={
            "address": Account.create().address, "chain_id": 137})).status_code == 200


async def test_concurrency_is_immediate_and_survives_cancel_until_work_finishes(tmp_path, monkeypatch):
    auth = WalletAuth(tmp_path / "wallet.sqlite3", allowed_origins=[ORIGIN],
                      rate_allow=lambda _: True, max_active_requests=1)
    account = Account.create()
    entered, proceed = threading.Event(), threading.Event()
    original_recover = Account.recover_message

    def recover(*args, **kwargs):
        entered.set()
        assert proceed.wait(timeout=5)
        return original_recover(*args, **kwargs)

    async with client(auth) as web:
        payload = await challenge(web, account)
        released = asyncio.Event()
        original_release = auth._active.release

        def release():
            original_release()
            released.set()

        monkeypatch.setattr(auth._active, "release", release)
        monkeypatch.setattr(Account, "recover_message", recover)
        verifying = asyncio.create_task(verify(web, account, payload))
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            # This response arrives while the only admitted job is blocked,
            # so admission cannot wait in the executor or a semaphore queue.
            response = await asyncio.wait_for(web.get("/auth/wallet/session"), timeout=1)
            assert response.status_code == 503 and response.json()["error"] == "auth_busy"
            assert response.headers["retry-after"] == "2"
            response = await web.options("/auth/wallet/session", headers={
                "Access-Control-Request-Method": "GET"})
            assert response.status_code == 204
            verifying.cancel()
            with pytest.raises(asyncio.CancelledError):
                await verifying
            assert not released.is_set()
            assert (await web.get("/auth/wallet/session")).status_code == 503
        finally:
            proceed.set()
        await asyncio.wait_for(released.wait(), timeout=5)
        assert (await web.get("/auth/wallet/session")).status_code == 200
        # Rejected handler bodies release their slot as well.
        assert (await web.post("/auth/wallet/verify", json={})).status_code == 400
        assert (await web.get("/auth/wallet/session")).status_code == 200
