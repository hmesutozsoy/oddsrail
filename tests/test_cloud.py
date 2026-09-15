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


def sign_in(base: str, email: str, reg: dict | None = None, wrong_verifier: bool = False,
            shown: str | None = None) -> dict:
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
        # the page shows the inbox that will receive it, which is the
        # normalized address when the caller typed a plus-tagged one
        assert peek.status_code == 200 and "Continue" in peek.text and (shown or email) in peek.text

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


async def call(session, tool: str, **args) -> dict:
    res = await session.call_tool(tool, args)
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


async def test_arena_register_and_public_board(cloud):
    base = cloud["base"]
    d = sign_in(base, "dana@example.com")["token_response"].json()
    e = sign_in(base, "erin@example.com")["token_response"].json()
    async with mcp(base, d["access_token"]) as s:
        names = {t.name for t in (await s.list_tools()).tools}
        assert {"arena_register", "arena_unregister", "arena_status"} <= names
        assert (await call(s, "arena_status"))["registered"] is False
        await call(s, "paper_reset")
        bad = await call(s, "arena_register", name="x")
        assert bad["registered"] is False and "3 to 24" in bad["error"]
        ok = await call(s, "arena_register", name="Dana's fade bot", strategy="fades 8% overshoots")
        assert ok["registered"] is False, "apostrophes are not allowed"
        ok = await call(s, "arena_register", name="dana fade bot", strategy="fades 8% overshoots " + "x" * 200)
        assert ok["registered"] is True and ok["name"] == "dana fade bot" and len(ok["strategy"]) <= 140
        st = await call(s, "arena_status")
        assert st["registered"] is True and st["name"] == "dana fade bot"
    async with mcp(base, e["access_token"]) as s:
        taken = await call(s, "arena_register", name="DANA FADE BOT")
        assert taken["registered"] is False and "taken" in taken["error"]

    r = httpx.get(base + "/arena/paper.json?refresh=1")
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    board = r.json()
    assert board["division"] == "paper" and "computed_at_ts" not in board
    rows = [x for x in board["entries"] if not x["house"]]
    assert [x["name"] for x in rows] == ["dana fade bot"]
    # listed, but not ranked: a fresh entry has not traded yet
    assert rows[0].get("rank") is None and rows[0]["qualifies"] is False
    assert rows[0]["equity"] == 1000.0 and rows[0]["return_pct"] == 0.0
    assert rows[0]["fills"] == 0 and rows[0]["positions"] == 0

    async with mcp(base, d["access_token"]) as s:
        assert (await call(s, "arena_unregister"))["removed"] is True
    board = httpx.get(base + "/arena/paper.json?refresh=1").json()
    assert not [x for x in board["entries"] if not x["house"]]


def test_run_endpoint_serves_guests_with_cors(cloud):
    base = cloud["base"]
    gid = "ab" * 16
    pre = httpx.options(base + "/run", headers={"origin": "https://oddsrail.app",
                                                 "access-control-request-method": "POST"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "https://oddsrail.app"
    assert httpx.post(base + "/run", json={"guest": "nope", "config": {}}).status_code == 400
    fresh = httpx.get(base + "/run/ledger", params={"guest": gid}).json()
    assert fresh["ok"] and fresh["fresh"] and fresh["cash"] == 1000
    r = httpx.post(base + "/run", json={"guest": gid, "config": {"on": {"report": True}, "topic": "bitcoin"}},
                   headers={"origin": "https://oddsrail.app"}, timeout=120)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["engine"] == "rules" and out["strategies"] == [] and out["decisions"] == []
    assert out["ledger"]["cash"] == 1000 and isinstance(out["steps"], list)
    assert r.headers["access-control-allow-origin"] == "https://oddsrail.app"
    assert (cloud["data"] / "guests" / f"{gid}.json").exists()
    reset = httpx.post(base + "/run/reset", json={"guest": gid}).json()
    assert reset["ok"] and reset["reset"] is True


def test_keep_this_agent_claims_the_guest_ledger_and_schedules(cloud):
    base, data = cloud["base"], cloud["data"]
    gid = "cd" * 16
    # a guest runs once so there is a ledger to keep
    r = httpx.post(base + "/run", json={"guest": gid, "config": {"on": {"report": True}}}, timeout=120)
    assert r.status_code == 200 and (data / "guests" / f"{gid}.json").exists()

    assert httpx.post(base + "/claim/start", json={"guest": gid, "email": "not-an-email"}).status_code == 400
    r = httpx.post(base + "/claim/start", json={"guest": gid, "email": "keeper@example.com"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["mode"] == "console" and r.json().get("dev_link")
    link = r.json()["dev_link"]
    token = parse_qs(urlparse(link).query)["t"][0]
    assert httpx.get(link).status_code == 200 and "Keep your agent" in httpx.get(link).text
    done = httpx.post(base + "/claim/verify", data={"t": token}, follow_redirects=False)
    assert done.status_code == 303
    loc = done.headers["location"]
    assert loc.startswith("https://oddsrail.app/build#session=")
    session = loc.split("#session=")[1]
    assert httpx.post(base + "/claim/verify", data={"t": token}).status_code == 400, "single use"

    auth = {"authorization": f"Bearer {session}"}
    me = httpx.get(base + "/me", headers=auth).json()
    assert me["signed_in"] is True and me["email"] == "keeper@example.com" and me["agent"] is None
    assert not (data / "guests" / f"{gid}.json").exists(), "the guest ledger moved to the account"
    led = httpx.get(base + "/run/ledger", params={"guest": gid}, headers=auth).json()
    assert led["ok"] and led["account"] == "keeper@example.com"

    assert httpx.post(base + "/agents", json={"name": "keeper bot"}).status_code == 401
    r = httpx.post(base + "/agents", headers=auth, json={"name": "keeper bot", "strategy": "quotes football",
                                                          "config": {"on": {"mm": True}}, "schedule": "hourly",
                                                          "arena": True})
    assert r.status_code == 200, r.text
    a = r.json()["agent"]
    assert a["name"] == "keeper bot" and a["schedule"] == "hourly" and a["config"] == {"on": {"mm": True}}
    board = httpx.get(base + "/arena/paper.json?refresh=1").json()
    assert "keeper bot" in [e["name"] for e in board["entries"] if not e["house"]]
    me = httpx.get(base + "/me", headers=auth).json()
    assert me["agent"]["name"] == "keeper bot" and me["arena"] == "keeper bot"

    r = httpx.post(base + "/agents", headers=auth, json={"name": "keeper bot", "schedule": "off", "arena": False})
    assert r.json()["agent"]["schedule"] == "off"
    assert not [e for e in httpx.get(base + "/arena/paper.json?refresh=1").json()["entries"] if not e["house"]]

    assert httpx.post(base + "/logout", headers=auth).json()["ok"]
    assert httpx.get(base + "/me", headers=auth).json()["signed_in"] is False


def test_run_permalink_and_public_agent_page(cloud):
    base, data = cloud["base"], cloud["data"]
    gid = "ef" * 16
    r = httpx.post(base + "/run", json={"guest": gid, "config": {"on": {"report": True}, "topics": ["all"]}}, timeout=120)
    assert r.status_code == 200
    out = r.json()
    assert out["run_id"] and out["permalink"].endswith(out["run_id"])

    shared = httpx.get(base + f"/runs/{out['run_id']}.json")
    assert shared.status_code == 200
    j = shared.json()
    assert j["ok"] and j["id"] == out["run_id"] and j["agent"] is None
    assert j["config"]["topics"] == ["all"] and "decisions" in j["result"]
    assert "account" not in j["result"], "a shared run names nobody"
    assert httpx.get(base + "/runs/nope.json").status_code == 404
    assert httpx.get(base + "/runs/" + "z" * 40 + ".json").status_code == 404

    # a kept agent gets a public page with its run history. The OAuth access
    # token a connector holds proves the same account as a website session.
    session = sign_in(base, "curve@example.com")["token_response"].json()["access_token"]
    auth = {"authorization": f"Bearer {session}"}
    assert httpx.get(base + "/me", headers=auth).json()["email"] == "curve@example.com"
    saved = httpx.post(base + "/agents", headers=auth, json={"name": "curve bot", "strategy": "reads the board",
                                                             "config": {"on": {"report": True}}, "arena": True})
    assert saved.status_code == 200 and saved.json().get("arena") is True, saved.text
    httpx.post(base + "/run", headers=auth, json={"guest": gid, "config": {"on": {"report": True}}}, timeout=120)

    page = httpx.get(base + "/arena/agent/CURVE%20BOT.json")
    assert page.status_code == 200, page.text
    a = page.json()
    assert a["ok"] and a["name"] == "curve bot" and a["strategy"] == "reads the board"
    assert a["runs"] == 1 and len(a["history"]) == 1 and a["history"][0]["equity"] == 1000.0
    assert a["ledger"]["bankroll"] == 1000 and a["last_pass"]["ok"] is True
    assert httpx.get(base + f"/runs/{a['last_run_id']}.json").json()["agent"] == "curve bot"
    assert httpx.get(base + "/arena/agent/nobody.json").status_code == 404


def test_board_ranks_only_agents_that_have_traded(cloud):
    base = cloud["base"]
    session = sign_in(base, "idle@example.com")["token_response"].json()["access_token"]
    auth = {"authorization": f"Bearer {session}"}
    r = httpx.post(base + "/agents", headers=auth, json={"name": "idle bot", "arena": True,
                                                          "config": {"on": {"report": True}}})
    assert r.status_code == 200, r.text
    board = httpx.get(base + "/arena/paper.json?refresh=1").json()
    mine = [e for e in board["entries"] if e["name"] == "idle bot"][0]
    assert mine.get("rank") is None and mine["qualifies"] is False and mine["why_unranked"]
    assert board["qualification"]["fills"] >= 1 and board["qualification"]["passes"] >= 1

    # and it cannot wipe its record while it is listed
    gone = httpx.post(base + "/run/reset", headers=auth, json={"guest": "aa" * 16})
    assert gone.status_code == 409 and "public board" in gone.json()["error"]
    httpx.post(base + "/agents", headers=auth, json={"name": "idle bot", "arena": False})
    assert httpx.post(base + "/run/reset", headers=auth, json={"guest": "aa" * 16}).status_code == 200


def test_reserved_words_cannot_be_worn_as_a_name(cloud):
    base = cloud["base"]
    session = sign_in(base, "imposter@example.com")["token_response"].json()["access_token"]
    auth = {"authorization": f"Bearer {session}"}
    for name in ("house paper agent", "House Paper Agent", "oddsrail bot", "the-official-one", "ADMIN.bot"):
        r = httpx.post(base + "/agents", headers=auth, json={"name": name, "arena": True})
        assert r.status_code == 400 and "reserved" in r.json()["error"], name
    assert httpx.post(base + "/agents", headers=auth, json={"name": "greenhouse gases", "arena": False}).status_code == 400


def test_one_inbox_is_one_account(cloud):
    from oddsrail.cloud.db import DB
    n = DB.normalize_email
    assert n("A.B+arena@Gmail.com") == "ab@gmail.com"
    assert n("a.b@googlemail.com") == "ab@gmail.com"
    assert n("First+tag@fastmail.com") == "first@fastmail.com"
    assert n("dots.kept@outlook.com") == "dots.kept@outlook.com"     # only gmail ignores dots
    assert n("not-an-email") == "not-an-email"

    base = cloud["base"]
    first = sign_in(base, "farm@example.com")["token_response"].json()["access_token"]
    second = sign_in(base, "farm+second@example.com",
                     shown="farm@example.com")["token_response"].json()["access_token"]
    a = httpx.get(base + "/me", headers={"authorization": f"Bearer {first}"}).json()
    b = httpx.get(base + "/me", headers={"authorization": f"Bearer {second}"}).json()
    assert a["email"] == b["email"] == "farm@example.com", "plus-addressing is the same account"


# Guided builder routes deliberately exercised without a venue lookup. All
# execution requests below must fail validation before the runner is entered.
def _guided_config():
    return {"schema_version": 2, "mode": "paper", "market_mode": "universe",
            "market_ids": [], "topics": ["crypto"], "keyword": "", "bankroll": 1000,
            "perorder": 25, "maxpos": 5, "closing": 2, "minvol": 20000,
            "on": {"fade": True, "daily": True},
            "vals": {"fade": {"jump": 8, "hours": 6}, "daily": {"usd": 30}}}


def test_guided_capabilities_do_not_advertise_live_authorization(cloud):
    response = httpx.get(cloud["base"] + "/capabilities")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    capabilities = response.json()
    assert capabilities["ok"] is True
    assert capabilities["paper_runner"] is True
    assert capabilities["specific_markets"] is True
    assert capabilities["hosted_live"] is False
    assert capabilities["session_authorization"] is False
    assert capabilities["wallet_authentication"] is True
    assert capabilities["hosted_ai"] == "unavailable"
    assert capabilities["byo_api"] is False


def test_wallet_sign_in_is_mounted_but_does_not_authorize_legacy_accounts(cloud):
    from eth_account import Account
    from eth_account.messages import encode_defunct

    signer = Account.create()  # Disposable offline test identity, never funded.
    with httpx.Client(base_url=cloud["base"], headers={"Origin": "https://oddsrail.app"}) as client:
        challenge = client.post("/auth/wallet/challenge", json={"address": signer.address, "chain_id": 137})
        assert challenge.status_code == 200, challenge.text
        message = challenge.json()["message"]
        signature = "0x" + signer.sign_message(encode_defunct(text=message)).signature.hex()
        verified = client.post("/auth/wallet/verify", json={"message": message, "signature": signature})
        assert verified.status_code == 200, verified.text
        session = client.get("/auth/wallet/session")
        assert session.json()["authenticated"] is True
        assert session.json()["address"] == signer.address
        assert client.get("/me").json() == {"ok": True, "signed_in": False}
        assert client.post("/agents", json={}).status_code == 401
        mcp_response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                                   headers={"accept": "application/json, text/event-stream"})
        assert mcp_response.status_code == 401
        assert client.post("/auth/wallet/logout", json={}).status_code == 200
        assert client.get("/auth/wallet/session").json()["authenticated"] is False


def test_wallet_auth_does_not_inherit_permissive_public_cors(cloud):
    # Public lookup CORS has broader local-preview support; auth is stricter.
    for origin in ("http://127.0.0.1:9999", "null", "https://oddsrail.app.evil.invalid"):
        response = httpx.post(cloud["base"] + "/auth/wallet/challenge",
                              headers={"Origin": origin}, json={"address": "0x" + "a" * 40, "chain_id": 137})
        assert response.status_code == 403
        assert "set-cookie" not in response.headers


def test_activation_check_is_mounted_and_requires_wallet_cookie(cloud):
    capabilities = httpx.get(cloud["base"] + "/capabilities").json()
    assert capabilities["activation_check"] is True
    assert capabilities["hosted_live"] is False and capabilities["session_authorization"] is False
    response = httpx.post(cloud["base"] + "/activation/check", json={"config": {}},
                          headers={"Origin": "https://oddsrail.app", "Authorization": "Bearer not-a-wallet-session"})
    assert response.status_code == 401 and response.json()["error"] == "wallet_sign_in_required"
    assert response.headers["access-control-allow-credentials"] == "true"
    response = httpx.post(cloud["base"] + "/activation/check", json={"config": {}},
                          headers={"Origin": "http://127.0.0.1:9999"})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_trading_account_discovery_is_mounted_as_a_private_read(cloud):
    capabilities = httpx.get(cloud["base"] + "/capabilities").json()
    assert capabilities["trading_accounts"] is True
    assert capabilities["hosted_live"] is False
    response = httpx.post(cloud["base"] + "/trading/accounts", json={},
                          headers={"Origin": "https://oddsrail.app"})
    assert response.status_code == 401 and response.json()["error"] == "wallet_sign_in_required"
    assert response.headers["cache-control"] == "no-store"
    response = httpx.post(cloud["base"] + "/trading/accounts", json={},
                          headers={"Origin": "http://127.0.0.1:9999"})
    assert response.status_code == 403 and "accounts" not in response.json()


def test_activation_check_validates_authenticated_draft_without_execution(cloud):
    from eth_account import Account
    from eth_account.messages import encode_defunct

    signer = Account.create()
    with httpx.Client(base_url=cloud["base"], headers={"Origin": "https://oddsrail.app"}) as client:
        challenge = client.post("/auth/wallet/challenge", json={"address": signer.address, "chain_id": 137})
        assert challenge.status_code == 200, challenge.text
        message = challenge.json()["message"]
        signature = "0x" + signer.sign_message(encode_defunct(text=message)).signature.hex()
        assert client.post("/auth/wallet/verify", json={"message": message, "signature": signature}).status_code == 200
        response = client.post("/activation/check", json={"config": {"mode": "live"}})
        assert response.status_code == 400 and response.json()["error"] == "invalid_configuration"
        assert "draft" in response.json()["detail"]
        response = client.post("/activation/check", json={"config": {}, "address": signer.address})
        assert response.status_code == 400 and response.json()["error"] == "invalid_body"
        assert client.post("/auth/wallet/logout", json={}).status_code == 200
        assert client.post("/activation/check", json={"config": {}}).status_code == 401


def test_guided_validation_returns_executable_summary_not_replacement_config(cloud):
    config = _guided_config()
    response = httpx.post(cloud["base"] + "/config/validate", json={"config": config})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] is True and result["mode"] == "paper"
    summary = result["normalized"]
    assert summary["schema_version"] == 2
    assert summary["perorder"] == config["perorder"]
    assert summary["bankroll"] == config["bankroll"]
    assert summary["fade"]["jump"] == .08
    assert summary["daily"] == 30
    assert "config" not in result, "The normalized summary must not replace the original configuration."


@pytest.mark.parametrize("body", [None, [], "agent", 3, {"config": []}, {"config": None}])
def test_guided_validation_rejects_wrong_json_types(cloud, body):
    response = httpx.post(cloud["base"] + "/config/validate", content=json.dumps(body),
                          headers={"content-type": "application/json"})
    assert response.status_code == 400, response.text
    assert response.json()["ok"] is False
    assert isinstance(response.json()["error"], str)


def test_guided_validation_rejects_invalid_json_and_oversized_body(cloud):
    url = cloud["base"] + "/config/validate"
    invalid = httpx.post(url, content="{", headers={"content-type": "application/json"})
    assert invalid.status_code == 400
    assert invalid.json()["ok"] is False
    large = httpx.post(url, json={"config": _guided_config(), "padding": "x" * 20001})
    assert large.status_code == 413
    assert large.json()["ok"] is False


@pytest.mark.parametrize("forecast", ["Bitcoin: NaN", "Bitcoin: 1", "Bitcoin: .7\nEthereum: unknown"])
def test_guided_validation_rejects_malformed_forecasts(cloud, forecast):
    config = _guided_config()
    config["on"]["value"] = True
    config["vals"]["value"] = {"edge": 5, "kelly": .25, "views": forecast}
    response = httpx.post(cloud["base"] + "/config/validate", json={"config": config})
    assert response.status_code == 400, response.text
    assert "forecast" in response.json()["error"].lower()


def test_guided_validation_blocks_hosted_live_mode(cloud):
    config = _guided_config()
    config["mode"] = "live"
    response = httpx.post(cloud["base"] + "/config/validate", json={"config": config})
    assert response.status_code == 400
    assert "live" in response.json()["error"].lower()


def test_guided_validation_preserves_specific_outcome_scope(cloud):
    config = _guided_config()
    config.update(market_mode="specific", topics=[], market_ids=[str(10**50 + 1), str(10**50 + 2)])
    response = httpx.post(cloud["base"] + "/config/validate", json={"config": config})
    assert response.status_code == 200, response.text
    summary = response.json()["normalized"]
    assert summary["market_mode"] == "specific"
    assert summary["market_ids"] == config["market_ids"]
    assert all(isinstance(token, str) for token in summary["market_ids"])
    config["market_ids"] = []
    invalid = httpx.post(cloud["base"] + "/config/validate", json={"config": config})
    assert invalid.status_code == 400, "An empty selection must not become an unrestricted universe."


@pytest.mark.parametrize("url", ["https://polymarket.com.evil.example/event/a",
                                  "https://user@polymarket.com/event/a",
                                  "https://polymarket.com:443/event/a"])
def test_guided_market_resolution_rejects_untrusted_urls_offline(cloud, url):
    response = httpx.get(cloud["base"] + "/markets/resolve", params={"q": url})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_input"


@pytest.mark.parametrize("invalid_case", ["order_limit", "selection", "forecast", "live"])
def test_guided_run_rejects_invalid_v2_before_execution(cloud, invalid_case):
    config = _guided_config()
    if invalid_case == "order_limit":
        config["perorder"] = 501
    elif invalid_case == "selection":
        config.update(market_mode="specific", market_ids=[])
    elif invalid_case == "forecast":
        config["on"]["value"] = True
        config["vals"]["value"] = {"edge": 5, "kelly": .25, "views": "Bitcoin: maybe"}
    else:
        config["mode"] = "live"
    guest = secrets.token_hex(16)
    ledger = cloud["data"] / "guests" / (guest + ".json")
    assert not ledger.exists()
    response = httpx.post(cloud["base"] + "/run", json={"guest": guest, "config": config}, timeout=5)
    assert response.status_code == 400, response.text
    result = response.json()
    assert result["ok"] is False and isinstance(result["error"], str)
    assert "run_id" not in result and "decisions" not in result
    assert not ledger.exists(), "Rejected configuration must not enter paper execution."


@pytest.mark.parametrize("body", [None, [], "agent"])
def test_guided_run_rejects_nonobject_body(cloud, body):
    response = httpx.post(cloud["base"] + "/run", content=json.dumps(body),
                          headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_guided_agent_save_keeps_original_config_and_rejects_invalid_update(cloud):
    base = cloud["base"]
    session = sign_in(base, "guided-save@example.com")["token_response"].json()["access_token"]
    auth = {"authorization": "Bearer " + session}
    config = _guided_config()
    request = {"name": "guided save bot", "strategy": "rules", "config": config,
               "schedule": "off", "arena": False}
    saved = httpx.post(base + "/agents", headers=auth, json=request)
    assert saved.status_code == 200, saved.text
    assert saved.json()["agent"]["config"] == config
    invalid = _guided_config()
    invalid["perorder"] = 501
    rejected = httpx.post(base + "/agents", headers=auth, json={**request, "config": invalid})
    assert rejected.status_code == 400
    current = httpx.get(base + "/me", headers=auth).json()["agent"]
    assert current["config"] == config, "A rejected update must preserve the saved agent."


def _draft_config(version):
    config = _guided_config()
    config["mode"] = "draft"
    if version is None:
        config.pop("schema_version")
    else:
        config["schema_version"] = version
    return config


@pytest.mark.parametrize("version", [2, 1, None])
def test_draft_can_be_reviewed_without_execution_for_every_version(cloud, version):
    config = _draft_config(version)
    response = httpx.post(cloud["base"] + "/config/validate", json={"config": config})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] is True and result["mode"] == "draft"
    assert result["normalized"]["schema_version"] == (version or 1)
    assert "run_id" not in result and "decisions" not in result


@pytest.mark.parametrize("version", [2, 1, None])
def test_draft_cannot_enter_run_for_every_version(cloud, version):
    guest = secrets.token_hex(16)
    ledger = cloud["data"] / "guests" / (guest + ".json")
    response = httpx.post(cloud["base"] + "/run", json={"guest": guest, "config": _draft_config(version)},
                          timeout=5)
    assert response.status_code == 400, response.text
    result = response.json()
    assert result["ok"] is False and "draft" in result["error"].lower()
    assert "run_id" not in result and "decisions" not in result
    assert not ledger.exists(), "A draft must not enter the runner even without a v2 marker."


@pytest.mark.parametrize("version", [2, 1, None])
def test_draft_cannot_be_saved_as_scheduled_agent_for_every_version(cloud, version):
    base = cloud["base"]
    suffix = str(version) if version is not None else "omitted"
    session = sign_in(base, f"draft-guard-{suffix}@example.com")["token_response"].json()["access_token"]
    auth = {"authorization": "Bearer " + session}
    response = httpx.post(base + "/agents", headers=auth, json={
        "name": "draft guard " + suffix, "strategy": "review only", "config": _draft_config(version),
        "schedule": "hourly", "arena": False})
    assert response.status_code == 400, response.text
    assert "draft" in response.json()["error"].lower()
    assert httpx.get(base + "/me", headers=auth).json()["agent"] is None


@pytest.mark.parametrize("query", [[], [("address", "")], [("address", "0x123")],
                                     [("address", "0x" + "0" * 40)],
                                     [("address", "http://127.0.0.1/private")],
                                     [("address", "0x" + "ab" * 20), ("address", "0x" + "cd" * 20)]])
def test_public_portfolio_rejects_invalid_address_before_public_fetch(cloud, query):
    response = httpx.get(cloud["base"] + "/portfolio", params=query,
                         headers={"origin": "https://oddsrail.app", "authorization": "Bearer not-a-login"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_address"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "https://oddsrail.app"


def test_public_portfolio_preflight_needs_no_account_or_address(cloud):
    response = httpx.options(cloud["base"] + "/portfolio", headers={"origin": "https://oddsrail.app"})
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "https://oddsrail.app"
    assert "GET" in response.headers["access-control-allow-methods"]


def test_the_host_answers_what_crawlers_and_clients_ask_for(cloud):
    """Three requests the access log showed being met with 404."""
    import json
    from pathlib import Path
    base = cloud["base"]
    # Glama verifies ownership of a remote server by fetching this on the host
    g = httpx.get(base + "/.well-known/glama.json")
    assert g.status_code == 200
    assert g.json() == json.loads((Path(__file__).resolve().parent.parent / "glama.json").read_text())
    # some MCP clients try the path-appended metadata URL before the path-inserted one
    r = httpx.get(base + "/mcp/.well-known/oauth-protected-resource", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith("/.well-known/oauth-protected-resource/mcp")
    assert httpx.get(base + "/mcp/.well-known/oauth-protected-resource", follow_redirects=True).json()["resource"].endswith("/mcp")
    r = httpx.get(base + "/mcp/.well-known/oauth-authorization-server", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith("/.well-known/oauth-authorization-server")
    # search crawlers ask the API host for the site's sitemap
    r = httpx.get(base + "/sitemap.xml", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://oddsrail.app/sitemap.xml"
