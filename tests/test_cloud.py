"""The hosted server, end to end: OAuth 2.1 with dynamic client registration,
PKCE and magic-link sign-in, then authenticated MCP calls that land in a
per-account paper ledger.

Runs a real uvicorn process the way Claude reaches it. No mail service is
configured, so the magic link is read off the dev-mode page. Nothing here
needs a venue: the tools exercised are the ones that answer offline."""

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse

import httpx
import httpx2
import pytest

REDIRECT = "http://127.0.0.1/callback"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def cloud(tmp_path_factory):
    data = tmp_path_factory.mktemp("cloud")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ODDSRAIL_", "POLYMARKET_", "KALSHI_"))}
    env.update({"ODDSRAIL_CLOUD_URL": base, "ODDSRAIL_CLOUD_PORT": str(port),
                "ODDSRAIL_CLOUD_DATA": str(data), "ODDSRAIL_CLOUD_DEV": "1",
                "PYTHONUNBUFFERED": "1"})
    log = open(data / "server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "oddsrail.cloud.app"], env=env,
                            stdout=log, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                break
        except Exception:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("cloud app did not start:\n" + (data / "server.log").read_text())
    if proc.poll() is not None:
        raise RuntimeError("cloud app exited:\n" + (data / "server.log").read_text())
    yield {"base": base, "data": data}
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


# --------------------------------------------------------------------------- #
# the flow Claude runs                                                        #
# --------------------------------------------------------------------------- #

def _pkce():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def register(base: str, name: str = "claude-test") -> dict:
    meta = httpx.get(base + "/.well-known/oauth-authorization-server").json()
    r = httpx.post(meta["registration_endpoint"], json={
        "client_name": name, "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    assert r.status_code == 201, r.text
    return {"meta": meta, "client": r.json()}


def sign_in(base: str, email: str, reg: dict | None = None, wrong_verifier: bool = False) -> dict:
    """Authorize -> magic link -> confirm -> code -> tokens, asserting each hop."""
    reg = reg or register(base)
    meta, client = reg["meta"], reg["client"]
    verifier, challenge = _pkce()
    state = secrets.token_hex(8)
    r = httpx.get(meta["authorization_endpoint"], params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "scope": "oddsrail"}, follow_redirects=False)
    assert r.status_code in (302, 303, 307), r.text
    login_url = r.headers["location"]
    rid = parse_qs(urlparse(login_url).query)["req"][0]

    page = httpx.get(login_url)
    assert page.status_code == 200 and 'name="email"' in page.text and "claude-test" in page.text

    sent = httpx.post(base + "/login", data={"req": rid, "email": email})
    assert sent.status_code == 200, sent.text
    m = re.search(r'href="([^"]*/login/verify\?t=[^"]+)"', sent.text)
    assert m, "dev mode shows the magic link on the page"
    link = m.group(1).replace("&amp;", "&")
    token = parse_qs(urlparse(link).query)["t"][0]

    # A link scanner's GET must not sign anyone in: it only renders Continue.
    for _ in range(2):
        peek = httpx.get(link)
        assert peek.status_code == 200 and "Continue" in peek.text and email in peek.text

    done = httpx.post(base + "/login/verify", data={"t": token}, follow_redirects=False)
    assert done.status_code == 303, done.text
    cb = urlparse(done.headers["location"])
    assert cb.scheme + "://" + cb.netloc + cb.path == REDIRECT
    q = parse_qs(cb.query)
    assert q["state"] == [state] and q["iss"] == [base]
    code = q["code"][0]

    again = httpx.post(base + "/login/verify", data={"t": token})
    assert again.status_code == 400, "a magic link is single use"

    tok = httpx.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": code,
        "code_verifier": verifier + ("x" if wrong_verifier else ""),
        "client_id": client["client_id"], "redirect_uri": REDIRECT})
    return {"reg": reg, "token_response": tok, "email": email}


@contextlib.asynccontextmanager
async def mcp(base: str, access_token: str):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {access_token}"},
                              timeout=httpx2.Timeout(90.0))
    async with http:
        async with streamable_http_client(base + "/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def call(session, name: str, **args) -> dict:
    res = await session.call_tool(name, args)
    assert not res.is_error, res
    return json.loads(res.content[0].text)


# --------------------------------------------------------------------------- #
# tests                                                                       #
# --------------------------------------------------------------------------- #

def test_discovery_and_unauthenticated_requests_are_refused(cloud):
    base = cloud["base"]
    meta = httpx.get(base + "/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == base, "no trailing slash: RFC 8414 compares issuers as strings"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["registration_endpoint"].endswith("/register")
    prm = httpx.get(base + "/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == base + "/mcp" and prm["authorization_servers"] == [base]
    r = httpx.post(base + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                   headers={"accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    assert httpx.get(base + "/healthz").json()["hosted"] is True


def test_wrong_pkce_verifier_is_rejected(cloud):
    out = sign_in(cloud["base"], "pkce@example.com", wrong_verifier=True)
    assert out["token_response"].status_code == 400
    assert out["token_response"].json()["error"] == "invalid_grant"


async def test_sign_in_then_each_account_gets_its_own_paper_ledger(cloud):
    base, data = cloud["base"], cloud["data"]
    a = sign_in(base, "alice@example.com")
    b = sign_in(base, "bob@example.com")
    ta, tb = a["token_response"].json(), b["token_response"].json()
    assert ta["token_type"] == "Bearer" and ta["refresh_token"] and ta["expires_in"] == 3600

    async with mcp(base, ta["access_token"]) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        for hidden in ("open_orders", "my_positions", "split_position", "kalshi_place_order",
                       "compare_venues", "cancel_all_orders"):
            assert hidden not in tools, hidden
        assert "check_order" in tools and "paper_positions" in tools
        assert tools["place_order"].description.startswith("Place a PAPER limit order")
        assert tools["place_order"].annotations.destructive_hint is True
        prompts = {p.name for p in (await s.list_prompts()).prompts}
        assert prompts == {"find_fade_setup"}

        reset_a = await call(s, "paper_reset")
        assert reset_a["reset"] is True and reset_a["cash"] == 1000
        assert reset_a["ledger"].startswith(str(data / "ledgers"))

        info = await call(s, "server_info")
        assert info["hosted"] is True and info["account"]["email"] == "alice@example.com"
        assert info["trading_key_configured"] is False and info["dry_run"] is True
        assert info["venues"]["kalshi"]["available"] is False

        # Kalshi is off, and says why, without touching the network.
        blocked = await call(s, "check_order", venue="kalshi", market_id="KXBTC-1", side="BUY",
                             price=0.5, size=10, intent="buy yes")
        assert blocked["verdict"] == "block" and "self-host" in blocked["error"].lower()
        narrowed = await call(s, "find_markets", query="anything", venues="kalshi")
        assert "Kalshi" in narrowed["error"]

    async with mcp(base, tb["access_token"]) as s:
        reset_b = await call(s, "paper_reset")
        assert reset_b["ledger"] != reset_a["ledger"], "ledgers are per account"
        assert reset_b["ledger"].startswith(str(data / "ledgers"))


def test_refresh_rotates_and_revokes_the_old_pair(cloud):
    base = cloud["base"]
    out = sign_in(base, "carol@example.com")
    reg, first = out["reg"], out["token_response"].json()
    r = httpx.post(reg["meta"]["token_endpoint"], data={
        "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
        "client_id": reg["client"]["client_id"]})
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]

    def probe(token):
        return httpx.post(base + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                          headers={"authorization": f"Bearer {token}",
                                   "accept": "application/json, text/event-stream"}).status_code

    assert probe(first["access_token"]) == 401, "rotation revokes the old pair"
    assert probe(second["access_token"]) == 200
    r = httpx.post(reg["meta"]["token_endpoint"], data={
        "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
        "client_id": reg["client"]["client_id"]})
    assert r.status_code == 400, "a rotated refresh token cannot be replayed"


def test_sign_in_page_rejects_junk(cloud):
    base = cloud["base"]
    assert httpx.get(base + "/login?req=nonsense").status_code == 400
    assert httpx.get(base + "/login/verify?t=nonsense").status_code == 400
    assert httpx.post(base + "/login/verify", data={"t": "nonsense"}).status_code == 400
    reg = register(base)
    verifier, challenge = _pkce()
    r = httpx.get(reg["meta"]["authorization_endpoint"], params={
        "response_type": "code", "client_id": reg["client"]["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256"}, follow_redirects=False)
    rid = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
    bad = httpx.post(base + "/login", data={"req": rid, "email": "not-an-email"})
    assert bad.status_code == 400 and "email address" in bad.text
