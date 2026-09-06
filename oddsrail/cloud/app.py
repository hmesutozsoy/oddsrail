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

    from .. import paper
    from .. import server as S
    from . import auth as A
    from . import mail, pages

    prov = A.provider()
    base = A.base_url()
    ledgers = A.data_dir() / "ledgers"
    ledgers.mkdir(parents=True, exist_ok=True)

    def account_ledger():
        tok = get_access_token()
        if tok is None or not tok.subject:
            raise RuntimeError("no signed-in account on this request")
        return ledgers / f"{tok.subject}.json"

    paper.ledger_resolver = account_ledger

    srv = S.srv
    dev = os.environ.get("ODDSRAIL_CLOUD_DEV") == "1"
    limiter = RateLimiter()

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    @srv.custom_route("/", methods=["GET"])
    async def home(request: Request):
        return HTMLResponse(pages.home(base, S.VERSION))

    @srv.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request):
        return JSONResponse({"ok": True, "version": S.VERSION, "hosted": True,
                             "mail": "resend" if mail.configured() else "console"})

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

    public_host = urlparse(base).netloc
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public_host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[base, "http://127.0.0.1:*", "http://localhost:*"],
    )
    return srv.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                   json_response=True, host=public_host,
                                   transport_security=security)


def main() -> None:
    import uvicorn
    app = build_app()
    uvicorn.run(app, host=os.environ.get("ODDSRAIL_CLOUD_BIND", "127.0.0.1"),
                port=int(os.environ.get("ODDSRAIL_CLOUD_PORT", "8787")),
                proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_level="info")


if __name__ == "__main__":
    main()
