"""OAuth 2.1 authorization server for the hosted oddsrail.

Claude (and any MCP client) registers itself dynamically, sends the user to
/authorize, and the user signs in with a magic link sent to their email.
The email is the account. Tokens are random, stored hashed, and rotate on
refresh; nothing here is a JWT because nothing needs to verify offline.

Environment:
  ODDSRAIL_CLOUD_URL    public https base URL, e.g. https://mcp.oddsrail.app
  ODDSRAIL_CLOUD_DATA   data directory (sqlite + per-account ledgers);
                        default ~/.oddsrail/cloud
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
                                      RefreshToken, TokenError, construct_redirect_uri)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from .db import DB

SCOPE = "oddsrail"
ACCESS_TTL = 3600            # 1 hour, refreshed silently by the client
REFRESH_TTL = 30 * 86400     # 30 days of inactivity signs you out
CODE_TTL = 600
REQUEST_TTL = 900            # time to complete the sign-in after Claude redirects
LINK_TTL = 900               # magic link validity
MAX_LINKS_PER_HOUR = 5


class LoginError(Exception):
    """Something the user should read; the message is shown as-is."""


def base_url() -> str:
    u = os.environ.get("ODDSRAIL_CLOUD_URL", "").strip().rstrip("/")
    if not u:
        raise RuntimeError("ODDSRAIL_CLOUD_URL is required in hosted mode "
                           "(the public base URL, e.g. https://mcp.oddsrail.app)")
    return u


def data_dir() -> Path:
    p = os.environ.get("ODDSRAIL_CLOUD_DATA")
    return Path(p).expanduser() if p else Path.home() / ".oddsrail" / "cloud"


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _tok(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


class Provider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider
    plus the magic-link half the web routes drive."""

    def __init__(self, db: DB, base: str):
        self.db = db
        self.base = base

    # ------------------ OAuthAuthorizationServerProvider ------------------ #

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.db.get_client(client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.db.put_client(client_info.client_id, client_info.model_dump_json())

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        rid = _tok(16)
        self.db.put_auth_request(rid, client.client_id, params.model_dump(mode="json"), REQUEST_TTL)
        return f"{self.base}/login?req={rid}"

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        data = self.db.get_code(_h(authorization_code))
        if not data or data.get("client_id") != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **data)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        self.db.delete_code(_h(authorization_code.code))   # single use
        if not authorization_code.subject:
            raise TokenError("invalid_grant", "authorization code has no account")
        return self._issue(client.client_id, authorization_code.subject, authorization_code.scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        row = self.db.get_token(_h(refresh_token), "refresh")
        if not row or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"],
                            scopes=row["scopes"].split(), subject=row["user_id"],
                            expires_at=int(row["expires"]) if row["expires"] else None)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        row = self.db.get_token(_h(refresh_token.token), "refresh")
        if not row:
            raise TokenError("invalid_grant", "refresh token is not valid")
        self.db.revoke_pair(row["pair"])                     # rotate: old pair dies
        return self._issue(client.client_id, row["user_id"], scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.db.get_token(_h(token), "access")
        if not row:
            return None
        user = self.db.user(row["user_id"])
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"].split(),
                           expires_at=int(row["expires"]) if row["expires"] else None,
                           subject=row["user_id"],
                           claims={"iss": self.base, "email": user["email"] if user else None})

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        row = (self.db.get_token(_h(token.token), "access")
               or self.db.get_token(_h(token.token), "refresh"))
        if row:
            self.db.revoke_pair(row["pair"])

    async def exchange_identity_assertion(self, client, params) -> OAuthToken:  # SEP-990, not offered
        raise TokenError("unsupported_grant_type", "identity assertion is not supported")

    def _issue(self, client_id: str, user_id: str, scopes: list[str]) -> OAuthToken:
        pair, access, refresh = _tok(8), _tok(32), _tok(32)
        self.db.put_token(_h(access), "access", client_id, user_id, scopes, pair, ACCESS_TTL)
        self.db.put_token(_h(refresh), "refresh", client_id, user_id, scopes, pair, REFRESH_TTL)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          scope=" ".join(scopes), refresh_token=refresh)

    # ------------------------- magic-link sign-in ------------------------- #

    def auth_request(self, rid: str) -> dict | None:
        """The pending authorization request behind /login?req=..., with the
        requesting client's display name."""
        r = self.db.get_auth_request(rid)
        if not r:
            return None
        name = None
        raw = self.db.get_client(r["client_id"])
        if raw:
            try:
                name = OAuthClientInformationFull.model_validate_json(raw).client_name
            except Exception:
                name = None
        r["client_name"] = name or r["client_id"]
        return r

    def start_login(self, email: str, rid: str) -> str:
        """Create a magic link for this email and pending request; returns
        the URL to deliver. Raises LoginError when the email is over its
        hourly allowance."""
        if self.db.recent_links(email, 3600) >= MAX_LINKS_PER_HOUR:
            raise LoginError("Too many sign-in emails were sent to that address in the last hour. "
                             "Open the latest one, or try again later.")
        token = _tok(24)
        self.db.put_magic_link(_h(token), email, rid, LINK_TTL)
        return f"{self.base}/login/verify?t={token}"

    def peek_login(self, token: str) -> dict | None:
        """Look at a link without consuming it (email scanners fetch links;
        the confirm button consumes). Returns {email, client_name} or None."""
        link = self.db.get_magic_link(_h(token))
        if not link:
            return None
        req = self.auth_request(link["req_id"])
        if not req:
            return None
        return {"email": link["email"], "client_name": req["client_name"]}

    def finish_login(self, token: str) -> str:
        """Consume the link, sign the account in, mint the authorization code
        and return the redirect URL back to the client."""
        h = _h(token)
        link = self.db.get_magic_link(h)
        if not link:
            raise LoginError("This sign-in link is invalid, already used, or expired. "
                             "Go back to Claude and connect again.")
        req = self.db.get_auth_request(link["req_id"])
        if not req:
            raise LoginError("The connection request expired before you signed in. "
                             "Go back to Claude and connect again.")
        if not self.db.use_magic_link(h):
            raise LoginError("This sign-in link was already used.")
        user = self.db.login_user(link["email"])
        p = req["params"]
        code = _tok(32)
        data = {"scopes": p.get("scopes") or [SCOPE],
                "expires_at": time.time() + CODE_TTL,
                "client_id": req["client_id"],
                "code_challenge": p["code_challenge"],
                "redirect_uri": p["redirect_uri"],
                "redirect_uri_provided_explicitly": bool(p.get("redirect_uri_provided_explicitly", True)),
                "resource": p.get("resource"),
                "subject": user["id"]}
        self.db.put_code(_h(code), data, CODE_TTL)
        self.db.delete_auth_request(link["req_id"])
        return construct_redirect_uri(str(p["redirect_uri"]), code=code, state=p.get("state"), iss=self.base)


_provider: Provider | None = None


def provider() -> Provider:
    """Process-wide provider (one sqlite file, one issuer)."""
    global _provider
    if _provider is None:
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        _provider = Provider(DB(d / "cloud.sqlite3"), base_url())
    return _provider


def server_auth_kwargs() -> dict:
    """What MCPServer needs to become an OAuth-protected resource server and
    the authorization server in one process."""
    base = base_url()
    return {
        "auth_server_provider": provider(),
        # Strings, not pre-built AnyHttpUrl: AuthSettings keeps an empty path
        # empty, so the issuer stays "https://host" (RFC 8414 compares issuers
        # as exact strings, and a stray trailing slash breaks that).
        "auth": AuthSettings(
            issuer_url=base,
            resource_server_url=base + "/mcp",
            service_documentation_url="https://oddsrail.app/",
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=None,
        ),
    }
