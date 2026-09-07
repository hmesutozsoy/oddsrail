"""The hosted oddsrail: one process serving the OAuth endpoints, the sign-in
pages and the MCP endpoint over streamable HTTP.

    ODDSRAIL_CLOUD_URL=https://mcp.oddsrail.app python -m oddsrail.cloud.app

Environment (all optional except the URL):
  ODDSRAIL_CLOUD_URL       public base URL; also the OAuth issuer
  ODDSRAIL_CLOUD_DATA      sqlite + ledgers directory (default ~/.oddsrail/cloud)
  ODDSRAIL_CLOUD_BIND      listen address (default 127.0.0.1, behind Caddy)
  ODDSRAIL_CLOUD_PORT      listen port (default 8787)
  ODDSRAIL_RESEND_API_KEY  send magic links by email (else they go to the log)
  ODDSRAIL_MAIL_FROM       sender, default "oddsrail <login@oddsrail.app>"
  ODDSRAIL_CLOUD_DEV=1     show the magic link on the page when no mail
                           service is configured (never set this in production)

Requests run stateless: every MCP call carries its bearer token, the token
names the account, and the account names the paper ledger the call works on.
"""

from __future__ import annotations

import collections
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


class RateLimiter:
    """Small sliding window per key, in memory. Keeps a bored script from
    turning the sign-in form into a mail cannon; the per-email allowance in
    the provider is the durable one."""

    def __init__(self, limit: int = 20, window: float = 3600.0):
        self.limit, self.window = limit, window
        self._hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        q = self._hits[key]
        while q and q[0] < now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


def build_app():
    # The profile must be on before oddsrail.server registers its tools.
    os.environ["ODDSRAIL_HOSTED"] = "1"
    os.environ["ODDSRAIL_DRY_RUN"] = "1"
    os.environ.setdefault("ODDSRAIL_PAPER", "1")

    from mcp.server.auth.middleware.auth_context import get_access_token
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

    import asyncio

    from .. import paper
    from .. import server as S
    from . import arena
    from . import auth as A
    from . import mail, pages, runner, scheduler
    from .accounts import Accounts

    prov = A.provider()
    base = A.base_url()
    ledgers = A.data_dir() / "ledgers"
    ledgers.mkdir(parents=True, exist_ok=True)

    def account_ledger():
        forced = arena.FORCED_LEDGER.get()
        if forced is not None:              # the arena board marking one ledger
            return forced
        tok = get_access_token()
        if tok is None or not tok.subject:
            raise RuntimeError("no signed-in account on this request")
        return ledgers / f"{tok.subject}.json"

    paper.ledger_resolver = account_ledger

    def account_id() -> str:
        tok = get_access_token()
        if tok is None or not tok.subject:
            raise RuntimeError("no signed-in account on this request")
        return tok.subject

    house = os.environ.get("ODDSRAIL_ARENA_HOUSE_LEDGER")
    board = arena.Board(prov.db, ledgers, Path(house) if house else None)

    srv = S.srv
    dev = os.environ.get("ODDSRAIL_CLOUD_DEV") == "1"
    limiter = RateLimiter()

    # ---- arena tools: an agent enters itself from its own chat ---- #

    def arena_register(name: str, strategy: str = "") -> str:
        try:
            out = arena.register(prov.db, account_id(), name, strategy)
        except arena.ArenaError as e:
            return S._j({"registered": False, "error": str(e)})
        board.invalidate()
        return S._j(out)

    def arena_unregister() -> str:
        out = arena.unregister(prov.db, account_id())
        board.invalidate()
        return S._j(out)

    def arena_status() -> str:
        e = prov.db.arena_get(account_id())
        return S._j({"registered": bool(e), "name": e["name"] if e else None,
                     "strategy": e["strategy"] if e else None, "board": "https://oddsrail.app/arena"})

    srv.add_tool(arena_register, name="arena_register",
                 description=("Enter this account's PAPER ledger on the public oddsrail arena board "
                              "(oddsrail.app/arena) under a display name (3 to 24 characters) with a "
                              "one-line strategy description. Ranked by return on the paper bankroll. "
                              "Makes the ledger's equity and P&L public under that name; nothing else "
                              "about the account is shown. arena_unregister removes it."),
                 annotations=S.TRADE, structured_output=False)
    srv.add_tool(arena_unregister, name="arena_unregister",
                 description="Remove this account from the public arena board.",
                 annotations=S.TRADE, structured_output=False)
    srv.add_tool(arena_status, name="arena_status",
                 description="Whether this account is on the arena board, and under which name.",
                 annotations=S.READ, structured_output=False)

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    @srv.custom_route("/", methods=["GET"])
    async def home(request: Request):
        return HTMLResponse(pages.home(base, S.VERSION))

    @srv.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request):
        # CORS so the site can probe which hostname is live.
        return JSONResponse({"ok": True, "version": S.VERSION, "hosted": True, "mail": mail.mode()},
                            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})

    # ---- the runner: a paper pass for a builder config, no account needed ---- #

    guests = A.data_dir() / "guests"
    guests.mkdir(parents=True, exist_ok=True)
    run_limiter = RateLimiter(limit=40, window=3600.0)
    guest_limiter = RateLimiter(limit=30, window=3600.0)
    ALLOWED_ORIGINS = ("https://oddsrail.app", "https://www.oddsrail.app", base)

    def cors(request: Request) -> dict:
        origin = request.headers.get("origin", "")
        ok = origin in ALLOWED_ORIGINS or origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:")
        return {"Access-Control-Allow-Origin": origin if ok else ALLOWED_ORIGINS[0],
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "content-type, authorization",
                "Access-Control-Max-Age": "600", "Vary": "Origin", "Cache-Control": "no-store"}

    def guest_ledger(gid: str):
        if not re.fullmatch(r"[a-f0-9]{16,64}", gid or ""):
            return None
        return guests / f"{gid}.json"

    accounts = Accounts(prov.db, base, ledgers, guests)
    site_url = os.environ.get("ODDSRAIL_SITE_URL", "https://oddsrail.app").rstrip("/")

    def bearer(request: Request) -> str | None:
        h = request.headers.get("authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else None

    def who(request: Request) -> dict | None:
        return accounts.user_for(bearer(request))

    def ledger_for(request: Request, gid: str):
        """A signed-in account's ledger wins over the browser's guest id."""
        user = who(request)
        if user:
            return ledgers / f"{user['id']}.json", user
        return guest_ledger(gid), None

    async def run_json(request: Request) -> tuple[dict | None, str | None]:
        try:
            body = await request.json()
        except Exception:
            return None, "body must be JSON"
        if not isinstance(body, dict):
            return None, "body must be an object"
        return body, None

    @srv.custom_route("/run", methods=["POST", "OPTIONS"])
    async def run_pass(request: Request):
        if request.method == "OPTIONS":
            return PlainTextResponse("", status_code=204, headers=cors(request))
        body, err = await run_json(request)
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400, headers=cors(request))
        ledger, user = ledger_for(request, str(body.get("guest", "")))
        if ledger is None:
            return JSONResponse({"ok": False, "error": "guest must be 16 to 64 hex characters"},
                                status_code=400, headers=cors(request))
        if not run_limiter.allow(client_ip(request)) or not guest_limiter.allow(ledger.name):
            return JSONResponse({"ok": False, "error": "too many runs; try again in a while"},
                                status_code=429, headers=cors(request))
        cfg = body.get("config") or {}
        if not isinstance(cfg, dict):
            return JSONResponse({"ok": False, "error": "config must be an object"}, status_code=400,
                                headers=cors(request))
        async with runner.lock_for(ledger):
            try:
                out = await runner.run_pass(cfg, ledger)
            except Exception as e:
                print(f"[oddsrail-cloud] run failed: {type(e).__name__}: {e}", flush=True)
                return JSONResponse({"ok": False, "error": f"the pass failed: {type(e).__name__}"},
                                    status_code=500, headers=cors(request))
        if user and prov.db.agent_get(user["id"]):
            prov.db.agent_ran(user["id"], scheduler.summary(out))
        out["account"] = user["email"] if user else None
        return JSONResponse(out, headers=cors(request))

    @srv.custom_route("/run/ledger", methods=["GET", "OPTIONS"])
    async def run_ledger(request: Request):
        if request.method == "OPTIONS":
            return PlainTextResponse("", status_code=204, headers=cors(request))
        ledger, user = ledger_for(request, request.query_params.get("guest", ""))
        if ledger is None:
            return JSONResponse({"ok": False, "error": "guest must be 16 to 64 hex characters"},
                                status_code=400, headers=cors(request))
        token = arena.FORCED_LEDGER.set(ledger)
        try:
            if not ledger.exists():
                return JSONResponse({"ok": True, "fresh": True, "cash": paper.bankroll(),
                                     "equity": paper.bankroll(), "positions": [], "open_orders": [],
                                     "fills": 0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                                     "bankroll": paper.bankroll()}, headers=cors(request))
            async with runner.lock_for(ledger):
                pos = await paper.positions()
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=502,
                                headers=cors(request))
        finally:
            arena.FORCED_LEDGER.reset(token)
        pos["ok"] = True
        pos["account"] = user["email"] if user else None
        return JSONResponse(pos, headers=cors(request))

    @srv.custom_route("/run/reset", methods=["POST", "OPTIONS"])
    async def run_reset(request: Request):
        if request.method == "OPTIONS":
            return PlainTextResponse("", status_code=204, headers=cors(request))
        body, err = await run_json(request)
        ledger, _user = ledger_for(request, str((body or {}).get("guest", "")))
        if err or ledger is None:
            return JSONResponse({"ok": False, "error": err or "guest must be 16 to 64 hex characters"},
                                status_code=400, headers=cors(request))
        token = arena.FORCED_LEDGER.set(ledger)
        try:
            out = paper.reset()
        finally:
            arena.FORCED_LEDGER.reset(token)
        out["ok"] = True
        return JSONResponse(out, headers=cors(request))

    # ---- keep this agent: email sign-in for the site, sessions, the agent record ---- #

    @srv.custom_route("/claim/start", methods=["POST", "OPTIONS"])
    async def claim_start(request: Request):
        if request.method == "OPTIONS":
            return PlainTextResponse("", status_code=204, headers=cors(request))
        body, err = await run_json(request)
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400, headers=cors(request))
        gid = str(body.get("guest", ""))
        email = str(body.get("email", "")).strip().lower()
        if guest_ledger(gid) is None:
            return JSONResponse({"ok": False, "error": "guest must be 16 to 64 hex characters"},
                                status_code=400, headers=cors(request))
        if len(email) > 254 or not EMAIL_RE.match(email):
            return JSONResponse({"ok": False, "error": "that does not look like an email address"},
                                status_code=400, headers=cors(request))
        if not limiter.allow(client_ip(request)):
            return JSONResponse({"ok": False, "error": "too many sign-in attempts from this network"},
                                status_code=429, headers=cors(request))
        try:
            url = accounts.start_claim(email, gid)
        except A.LoginError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=429, headers=cors(request))
        try:
            mode = await mail.send_magic_link(email, url)
        except Exception as e:
            print(f"[oddsrail-cloud] mail failure for {email}: {type(e).__name__}: {e}", flush=True)
            return JSONResponse({"ok": False, "error": "we could not send the email right now; try again in a minute"},
                                status_code=502, headers=cors(request))
        out = {"ok": True, "sent": mode != "console", "mode": mode}
        if dev and mode == "console":
            out["dev_link"] = url
        return JSONResponse(out, headers=cors(request))

    @srv.custom_route("/claim/verify", methods=["GET"])
    async def claim_verify_get(request: Request):
        t = request.query_params.get("t", "")
        info = accounts.peek_claim(t) if t else None
        if not info:
            return HTMLResponse(pages.error("This sign-in link is invalid, already used, or expired. "
                                            "Ask for a new one from the builder page."), status_code=400)
        return HTMLResponse(pages.confirm_claim(t, info["email"]))

    @srv.custom_route("/claim/verify", methods=["POST"])
    async def claim_verify_post(request: Request):
        form = await request.form()
        t = str(form.get("t", ""))
        try:
            done = accounts.finish_claim(t)
        except A.LoginError as e:
            return HTMLResponse(pages.error(str(e)), status_code=400)
        return RedirectResponse(f"{site_url}/build#session={done['session']}", status_code=303)

    @srv.custom_route("/me", methods=["GET", "OPTIONS"])
    async def me(request: Request):
        if request.method == "OPTIONS":
            return PlainTextResponse("", status_code=204, headers=cors(request))
        user = who(request)
        if not user:
            return JSONResponse({"ok": True, "signed_in": False}, headers=cors(request))
        agent = prov.db.agent_get(user["id"])
        on_board = prov.db.arena_get(user["id"])
        return JSONResponse({"ok": True, "signed_in": True, "email": user["email"], "agent": agent,
                             "arena": on_board["name"] if on_board else None}, headers=cors(request))

    @srv.custom_route("/agents", methods=["POST", "DELETE", "OPTIONS"])
    async def agents_route(request: Request):
        if request.method == "OPTIONS":
            return PlainTextResponse("", status_code=204, headers=cors(request))
        user = who(request)
        if not user:
            return JSONResponse({"ok": False, "error": "sign in first"}, status_code=401, headers=cors(request))
        if request.method == "DELETE":
            prov.db.agent_delete(user["id"])
            arena.unregister(prov.db, user["id"])
            board.invalidate()
            return JSONResponse({"ok": True, "deleted": True}, headers=cors(request))
        body, err = await run_json(request)
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400, headers=cors(request))
        try:
            name = arena.validate_name(str(body.get("name", "")))
        except arena.ArenaError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400, headers=cors(request))
        strategy = " ".join(str(body.get("strategy") or "").split())[:arena.STRATEGY_MAX]
        cfg = body.get("config") if isinstance(body.get("config"), dict) else {}
        schedule = "hourly" if body.get("schedule") in ("hourly", True) else "off"
        prov.db.agent_put(user["id"], name, strategy, cfg, schedule)
        if body.get("arena"):
            try:
                arena.register(prov.db, user["id"], name, strategy)
            except arena.ArenaError as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=400, headers=cors(request))
        else:
            arena.unregister(prov.db, user["id"])
        board.invalidate()
        return JSONResponse({"ok": True, "agent": prov.db.agent_get(user["id"]),
                             "arena": bool(body.get("arena"))}, headers=cors(request))

    @srv.custom_route("/logout", methods=["POST", "OPTIONS"])
    async def logout(request: Request):
        if request.method == "OPTIONS":
            return PlainTextResponse("", status_code=204, headers=cors(request))
        accounts.sign_out(bearer(request))
        return JSONResponse({"ok": True}, headers=cors(request))

    @srv.custom_route("/arena/paper.json", methods=["GET"])
    async def arena_board(request: Request):
        data = await board.get(force=request.query_params.get("refresh") == "1" and dev)
        body = {k: v for k, v in data.items() if k != "computed_at_ts"}
        return JSONResponse(body, headers={"Access-Control-Allow-Origin": "*",
                                           "Cache-Control": "public, max-age=60"})

    @srv.custom_route("/robots.txt", methods=["GET"])
    async def robots(request: Request):
        return PlainTextResponse("User-agent: *\nDisallow: /login\nDisallow: /authorize\n")

    @srv.custom_route("/privacy", methods=["GET"])
    async def privacy(request: Request):
        return RedirectResponse("https://oddsrail.app/privacy", status_code=302)

    @srv.custom_route("/login", methods=["GET"])
    async def login_get(request: Request):
        rid = request.query_params.get("req", "")
        req = prov.auth_request(rid)
        if not req:
            return HTMLResponse(pages.error("This sign-in request has expired. Go back to Claude and "
                                            "connect again."), status_code=400)
        return HTMLResponse(pages.login(rid, req["client_name"]))

    @srv.custom_route("/login", methods=["POST"])
    async def login_post(request: Request):
        form = await request.form()
        rid = str(form.get("req", ""))
        email = str(form.get("email", "")).strip().lower()
        req = prov.auth_request(rid)
        if not req:
            return HTMLResponse(pages.error("This sign-in request has expired. Go back to Claude and "
                                            "connect again."), status_code=400)
        if len(email) > 254 or not EMAIL_RE.match(email):
            return HTMLResponse(pages.login(rid, req["client_name"],
                                            "That does not look like an email address."), status_code=400)
        if not limiter.allow(client_ip(request)):
            return HTMLResponse(pages.error("Too many sign-in attempts from this network. "
                                            "Try again in an hour."), status_code=429)
        try:
            url = prov.start_login(email, rid)
        except A.LoginError as e:
            return HTMLResponse(pages.error(str(e)), status_code=429)
        try:
            mode = await mail.send_magic_link(email, url)
        except Exception as e:  # the mail service, not the user
            print(f"[oddsrail-cloud] mail failure for {email}: {type(e).__name__}: {e}", flush=True)
            return HTMLResponse(pages.error("We could not send the email right now. Try again in a "
                                            "minute."), status_code=502)
        return HTMLResponse(pages.sent(email, url if (dev and mode == "console") else None))

    @srv.custom_route("/login/verify", methods=["GET"])
    async def verify_get(request: Request):
        # A GET only shows the confirm page: email link scanners fetch URLs,
        # and a fetch must not sign anyone in. The button below consumes.
        t = request.query_params.get("t", "")
        info = prov.peek_login(t) if t else None
        if not info:
            return HTMLResponse(pages.error("This sign-in link is invalid, already used, or expired. "
                                            "Go back to Claude and connect again."), status_code=400)
        return HTMLResponse(pages.confirm(t, info["email"], info["client_name"]))

    @srv.custom_route("/login/verify", methods=["POST"])
    async def verify_post(request: Request):
        form = await request.form()
        t = str(form.get("t", ""))
        try:
            url = prov.finish_login(t)
        except A.LoginError as e:
            return HTMLResponse(pages.error(str(e)), status_code=400)
        return RedirectResponse(url, status_code=303)

    # The scheduler lives inside the app's lifespan, wrapped around the one
    # the MCP transport installs, so it shares the event loop and its locks.
    import contextlib

    def with_scheduler(app):
        inner = app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def lifespan(a):
            async with inner(a):
                stop = asyncio.Event()
                task = None
                if os.environ.get("ODDSRAIL_SCHEDULER", "1") not in ("0", "false", "no"):
                    task = asyncio.create_task(scheduler.loop(prov.db, ledgers, stop))
                try:
                    yield
                finally:
                    stop.set()
                    if task:
                        task.cancel()
                        with contextlib.suppress(BaseException):
                            await task

        app.router.lifespan_context = lifespan
        return app

    public_host = urlparse(base).netloc
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public_host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[base, "http://127.0.0.1:*", "http://localhost:*"],
    )
    app = srv.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                  json_response=True, host=public_host,
                                  transport_security=security)
    return with_scheduler(app)


def main() -> None:
    import uvicorn
    app = build_app()
    uvicorn.run(app, host=os.environ.get("ODDSRAIL_CLOUD_BIND", "127.0.0.1"),
                port=int(os.environ.get("ODDSRAIL_CLOUD_PORT", "8787")),
                proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_level="info")


if __name__ == "__main__":
    main()
