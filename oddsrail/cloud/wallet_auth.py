"""Browser-bound Sign-In with Ethereum, separate from the MCP email accounts.

Only EIP-191 signatures from externally owned accounts are supported. A wallet
session proves control of an address; it never authorizes orders, transfers,
allowances, a signer, or a hosted runner. See https://eips.ethereum.org/EIPS/eip-4361.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_checksum_address, to_checksum_address
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

CHALLENGE_TTL = 300
SESSION_TTL = 8 * 3600
MAX_BODY_BYTES = 8192
STATEMENT = "Sign in to OddsRail. This does not authorize trades or access to your funds."
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_COOKIE = re.compile(r"[a-zA-Z0-9_-]{43}\Z")
_SIGNATURE = re.compile(r"0x[0-9a-fA-F]{130}\Z")


class WalletRequestError(Exception):
    """A controlled response from a wallet-authenticated route."""

    def __init__(self, code: str, status: int = 400, *, detail: str | None = None):
        self.code, self.status, self.detail = code, status, detail


_AuthError = WalletRequestError


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _cookie_hash(request: Request, name: str) -> str | None:
    token = request.cookies.get(name, "")
    return _hash(token) if _COOKIE.fullmatch(token) else None


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("non-finite JSON number")


class _Limiter:
    """Bounded per-process admission before body parsing or signature recovery."""

    def __init__(self):
        self._hits: dict[str, collections.deque] = {}
        self._lock = threading.Lock()
        self._next_cleanup = 0.0

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if now >= self._next_cleanup:
                for old, hits in list(self._hits.items()):
                    if not hits or hits[-1] <= now - 60:
                        del self._hits[old]
                self._next_cleanup = now + 30
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= 4096 or len(key) > 512:
                    return False
                hits = self._hits[key] = collections.deque()
            while hits and hits[0] <= now - 60:
                hits.popleft()
            bucket = key.partition(":")[0]
            limit = {"sign": 30, "session": 120, "logout": 60, "preflight": 120,
                     "activation": 30, "trading_accounts": 30}[bucket]
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True


def _bounded(handler):
    """Admit without queueing; retain slots for work surviving a disconnect."""
    @wraps(handler)
    async def admitted(self, request: Request) -> Response:
        try:
            preflight = self._admit(request, "GET" if handler.__name__ == "session" else "POST")
            if preflight is not None:
                return preflight
        except _AuthError as error:
            return self._error(request, error)
        if not self._active.acquire(blocking=False):
            return self._error(request, _AuthError("auth_busy", 503))

        def finished(task):
            self._active.release()
            # A disconnected caller may no longer be awaiting this result.
            if not task.cancelled():
                task.exception()

        try:
            task = asyncio.create_task(handler(self, request))
        except BaseException:
            self._active.release()
            raise
        task.add_done_callback(finished)
        # Cancelling a request cannot stop to_thread once it is running. Keep
        # its entire handler alive and counted until body/crypto/DB work ends.
        return await asyncio.shield(task)

    return admitted


class WalletAuth:
    """Construct once per app, then register every Route returned by routes().

    ``allowed_origins`` contains exact browser origins, without paths or a
    trailing slash. Production requires HTTPS; HTTP is allowed only for an
    explicitly configured localhost origin with ``dev=True``. All requests,
    including session reads, require a matching Origin. A reverse proxy must
    preserve Origin and cookies and supply a trusted request.client address.
    """

    def __init__(self, db_path: Path, *, allowed_origins: Iterable[str],
                 dev: bool = False, rate_allow: Callable[[str], bool] | None = None,
                 clock: Callable[[], float] = time.time,
                 max_challenges: int = 2048, max_sessions: int = 10000,
                 max_active_requests: int = 16):
        origins = frozenset(allowed_origins)
        if (not origins or max_challenges < 1 or max_sessions < 1
                or type(max_active_requests) is not int or max_active_requests < 1):
            raise ValueError("Wallet auth requires origins and positive storage limits")
        for origin in origins:
            parsed = urlsplit(origin)
            local_http = (dev and parsed.scheme == "http" and
                          parsed.hostname in {"localhost", "127.0.0.1", "::1"})
            if (not parsed.netloc or parsed.path or parsed.query or parsed.fragment or
                    parsed.username or parsed.password or
                    (parsed.scheme != "https" and not local_http) or
                    origin != f"{parsed.scheme}://{parsed.netloc}" or
                    any(ord(char) < 33 or ord(char) > 126 for char in origin)):
                raise ValueError("Wallet auth origins must be exact HTTPS origins")
        self.allowed_origins = origins
        self.dev, self._clock = dev, clock
        self.max_challenges, self.max_sessions = max_challenges, max_sessions
        self._active = threading.BoundedSemaphore(max_active_requests)
        self._limiter = _Limiter()
        self._rate_allow = rate_allow or self._limiter.allow
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        prefix = "" if dev else "__Host-"
        self.session_cookie = prefix + "oddsrail-wallet-session"
        self.preauth_cookie = prefix + "oddsrail-wallet-preauth"
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS wallet_challenges (
                    browser_hash TEXT PRIMARY KEY, message TEXT NOT NULL,
                    address TEXT NOT NULL, chain_id INTEGER NOT NULL,
                    origin TEXT NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS wallet_challenges_expiry
                    ON wallet_challenges(expires_at);
                CREATE TABLE IF NOT EXISTS wallet_sessions (
                    token_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL UNIQUE,
                    address TEXT NOT NULL, chain_id INTEGER NOT NULL,
                    origin TEXT NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS wallet_sessions_expiry
                    ON wallet_sessions(expires_at);
            """)
        self.db_path.chmod(0o600)

    @contextmanager
    def _connection(self, *, write: bool = False):
        db = sqlite3.connect(self.db_path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _prune(self, db, now: int):
        db.execute("DELETE FROM wallet_challenges WHERE expires_at <= ?", (now,))
        db.execute("DELETE FROM wallet_sessions WHERE expires_at <= ?", (now,))

    def routes(self) -> list[Route]:
        return [
            Route("/auth/wallet/challenge", self.challenge, methods=["POST", "OPTIONS"]),
            Route("/auth/wallet/verify", self.verify, methods=["POST", "OPTIONS"]),
            Route("/auth/wallet/session", self.session, methods=["GET", "OPTIONS"]),
            Route("/auth/wallet/logout", self.logout, methods=["POST", "OPTIONS"]),
        ]

    async def _session_identity(self, request: Request) -> dict | None:
        """Resolve only this request's opaque cookie and exact browser origin."""
        origin = request.headers.get("origin", "")
        if len(request.headers.getlist("origin")) != 1 or origin not in self.allowed_origins:
            raise _AuthError("origin_not_allowed", 403)
        token_hash = _cookie_hash(request, self.session_cookie)

        def lookup():
            if token_hash is None:
                return None
            with self._connection() as db:
                row = db.execute("SELECT address, chain_id, expires_at FROM wallet_sessions "
                                 "WHERE token_hash = ? AND origin = ? AND expires_at > ?",
                                 (token_hash, origin, int(self._clock()))).fetchone()
                return dict(row) if row else None

        return await asyncio.to_thread(lookup)

    async def require_session(self, request: Request) -> dict:
        """Verify the owner in a bounded handler; never take identity from JSON.

        This lookup is not a trading permission. Route handlers should normally
        use ``authenticated_post`` so admission precedes its database work.
        """
        row = await self._session_identity(request)
        if row is None:
            raise _AuthError("wallet_sign_in_required", 401)
        return row

    def authenticated_post(self, handler, *, keys: set[str], max_body_bytes: int = MAX_BODY_BYTES):
        """Wrap a read-only check in the same origin, session and work limits.

        ``handler(identity, body)`` returns a JSON object or raises
        ``WalletRequestError``. It must bound upstream work independently.
        No waiting queue is added: cancellation keeps the shared admission slot
        until any body, database and handler work actually finishes.
        """
        if type(max_body_bytes) is not int or not 1 <= max_body_bytes <= 20000:
            raise ValueError("Wallet request body limit must be between 1 and 20000 bytes")

        @_bounded
        async def dispatch(auth, request: Request) -> Response:
            try:
                identity = await auth.require_session(request)
                body = await auth._json(request, keys, max_body_bytes=max_body_bytes)
                # A slow body must not outlive the sign-in used for its lookup.
                if await auth.require_session(request) != identity:
                    raise _AuthError("wallet_sign_in_required", 401)
                result = await handler(identity, body)
                # The check is read-only, but do not label an expired or revoked
                # wallet as ready after a slow public-account response.
                if await auth.require_session(request) != identity:
                    raise _AuthError("wallet_sign_in_required", 401)
                return auth._response(request, result)
            except (_AuthError, sqlite3.Error) as error:
                return auth._error(request, error)

        async def endpoint(request: Request) -> Response:
            return await dispatch(self, request)

        return endpoint

    def _headers(self, origin: str) -> dict[str, str]:
        headers = {"Cache-Control": "no-store", "Vary": "Origin",
                   "X-Content-Type-Options": "nosniff"}
        if origin in self.allowed_origins:
            headers.update({"Access-Control-Allow-Origin": origin,
                            "Access-Control-Allow-Credentials": "true"})
        return headers

    def _admit(self, request: Request, method: str) -> Response | None:
        origin = request.headers.get("origin", "")
        if len(request.headers.getlist("origin")) != 1 or origin not in self.allowed_origins:
            raise _AuthError("origin_not_allowed", 403)
        bucket = ("preflight" if request.method == "OPTIONS" else
                  "session" if request.url.path.endswith("/session") else
                  "logout" if request.url.path.endswith("/logout") else
                  "activation" if request.url.path == "/activation/check" else
                  "trading_accounts" if request.url.path == "/trading/accounts" else "sign")
        key = bucket + ":" + (request.client.host if request.client else "unknown")
        if not self._rate_allow(key):
            raise _AuthError("rate_limited", 429)
        if request.method == "OPTIONS":
            requested = request.headers.get("access-control-request-method", "")
            headers = {x.strip().lower() for x in request.headers.get(
                "access-control-request-headers", "").split(",") if x.strip()}
            if requested != method or not headers.issubset({"content-type"}):
                raise _AuthError("preflight_not_allowed", 403)
            return Response(status_code=204, headers={
                **self._headers(origin), "Access-Control-Allow-Methods": method,
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "600"})
        return None

    async def _json(self, request: Request, keys: set[str], *, max_body_bytes: int = MAX_BODY_BYTES) -> dict:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise _AuthError("json_required", 415)
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal():
                raise _AuthError("invalid_body")
            if len(length) > 8 or int(length) > max_body_bytes:
                raise _AuthError("body_too_large", 413)

        async def read():
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > max_body_bytes:
                    raise _AuthError("body_too_large", 413)
                body.extend(chunk)
            return body

        try:
            body = await asyncio.wait_for(read(), timeout=5)
            result = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                                parse_constant=_invalid_constant)
        except asyncio.TimeoutError:
            raise _AuthError("body_timeout", 408) from None
        except (ValueError, UnicodeError, RecursionError, ClientDisconnect):
            raise _AuthError("invalid_body") from None
        if not isinstance(result, dict) or set(result) != keys:
            raise _AuthError("invalid_body")
        return result

    def _response(self, request: Request, data: dict, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status,
                            headers=self._headers(request.headers.get("origin", "")))

    def _error(self, request: Request, error: Exception) -> JSONResponse:
        code, status = ((error.code, error.status) if isinstance(error, _AuthError)
                        else ("auth_unavailable", 503))
        data = {"ok": False, "error": code}
        if isinstance(error, _AuthError) and error.detail:
            data["detail"] = error.detail
        response = self._response(request, data, status)
        if status in {429, 503}:
            response.headers["Retry-After"] = "60" if status == 429 else "2"
        return response

    def _set_cookie(self, response: Response, name: str, token: str, ttl: int):
        response.set_cookie(name, token, max_age=ttl, path="/", httponly=True,
                            secure=not self.dev, samesite="strict")

    def _clear_cookie(self, response: Response, name: str):
        response.delete_cookie(name, path="/", httponly=True,
                               secure=not self.dev, samesite="strict")

    @_bounded
    async def challenge(self, request: Request) -> Response:
        try:
            body = await self._json(request, {"address", "chain_id"})
            address, chain_id = body["address"], body["chain_id"]
            if (not isinstance(address, str) or not _ADDRESS.fullmatch(address)
                    or address == "0x" + "0" * 40):
                raise _AuthError("invalid_address")
            if (address[2:] != address[2:].lower() and address[2:] != address[2:].upper()
                    and not is_checksum_address(address)):
                raise _AuthError("invalid_address")
            if type(chain_id) is not int or chain_id not in {1, 137}:
                raise _AuthError("unsupported_chain")
            address = to_checksum_address(address)
            origin = request.headers["origin"]
            now = int(self._clock())
            expires_at = now + CHALLENGE_TTL
            message = (f"{origin} wants you to sign in with your Ethereum account:\n"
                       f"{address}\n\n{STATEMENT}\n\nURI: {origin}/\nVersion: 1\n"
                       f"Chain ID: {chain_id}\nNonce: {secrets.token_hex(16)}\n"
                       f"Issued At: {_iso(now)}\nExpiration Time: {_iso(expires_at)}")
            previous = _cookie_hash(request, self.preauth_cookie)

            def store():
                with self._connection(write=True) as db:
                    self._prune(db, now)
                    known_browser = previous and db.execute(
                        "SELECT 1 FROM wallet_challenges WHERE browser_hash = ? "
                        "UNION ALL SELECT 1 FROM wallet_sessions WHERE browser_hash = ?",
                        (previous, previous)).fetchone()
                    # Keep a live browser family stable across tabs and sign-in
                    # attempts. Never accept an arbitrary client-chosen token.
                    token = (request.cookies[self.preauth_cookie] if known_browser
                             else secrets.token_urlsafe(32))
                    db.execute("DELETE FROM wallet_challenges WHERE browser_hash = ?", (_hash(token),))
                    if db.execute("SELECT COUNT(*) FROM wallet_challenges").fetchone()[0] >= self.max_challenges:
                        raise _AuthError("auth_capacity", 503)
                    db.execute("INSERT INTO wallet_challenges VALUES (?, ?, ?, ?, ?, ?)",
                               (_hash(token), message, address, chain_id, origin, expires_at))
                return token

            token = await asyncio.to_thread(store)
            response = self._response(request, {"ok": True, "message": message, "expires_at": expires_at})
            self._set_cookie(response, self.preauth_cookie, token, SESSION_TTL)
            return response
        except (_AuthError, sqlite3.Error) as error:
            return self._error(request, error)

    @_bounded
    async def verify(self, request: Request) -> Response:
        try:
            body = await self._json(request, {"message", "signature"})
            message, signature = body["message"], body["signature"]
            if (not isinstance(message, str) or not 1 <= len(message) <= 2048 or
                    not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature)):
                raise _AuthError("invalid_signature")
            browser_hash = _cookie_hash(request, self.preauth_cookie)
            if browser_hash is None:
                raise _AuthError("challenge_expired", 401)
            origin = request.headers["origin"]
            previous = _cookie_hash(request, self.session_cookie)

            def authenticate():
                with self._connection() as db:
                    row = db.execute("SELECT * FROM wallet_challenges WHERE browser_hash = ?",
                                     (browser_hash,)).fetchone()
                if (not row or row["expires_at"] <= int(self._clock()) or
                        row["origin"] != origin or row["message"] != message):
                    raise _AuthError("challenge_expired", 401)
                try:
                    recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
                except Exception:
                    raise _AuthError("invalid_signature", 401) from None
                if recovered != row["address"]:
                    raise _AuthError("invalid_signature", 401)
                token = secrets.token_urlsafe(32)
                now = int(self._clock())
                expires_at = now + SESSION_TTL
                with self._connection(write=True) as db:
                    self._prune(db, now)
                    deleted = db.execute("DELETE FROM wallet_challenges WHERE browser_hash = ? "
                                         "AND origin = ? AND message = ? AND expires_at > ?",
                                         (browser_hash, origin, message, now)).rowcount
                    if deleted != 1:
                        raise _AuthError("challenge_expired", 401)
                    # Rotate this browser's old login and prevent a racing
                    # logout carrying the preauth cookie from missing it.
                    db.execute("DELETE FROM wallet_sessions WHERE token_hash = ? OR browser_hash = ?",
                               (previous, browser_hash))
                    if db.execute("SELECT COUNT(*) FROM wallet_sessions").fetchone()[0] >= self.max_sessions:
                        raise _AuthError("auth_capacity", 503)
                    db.execute("INSERT INTO wallet_sessions VALUES (?, ?, ?, ?, ?, ?)",
                               (_hash(token), browser_hash, row["address"], row["chain_id"], origin, expires_at))
                return token, {"ok": True, "authenticated": True, "address": row["address"],
                               "chain_id": row["chain_id"], "expires_at": expires_at}

            token, data = await asyncio.to_thread(authenticate)
            response = self._response(request, data)
            self._set_cookie(response, self.session_cookie, token, SESSION_TTL)
            # Retain the stable browser context so logout from another tab
            # revokes a newly minted session even before its response arrives.
            self._set_cookie(response, self.preauth_cookie,
                             request.cookies[self.preauth_cookie], SESSION_TTL)
            return response
        except (_AuthError, sqlite3.Error) as error:
            return self._error(request, error)

    @_bounded
    async def session(self, request: Request) -> Response:
        try:
            token_hash = _cookie_hash(request, self.session_cookie)
            row = await self._session_identity(request)
            data = {"ok": True, "authenticated": bool(row)}
            if row:
                data.update(dict(row))
            response = self._response(request, data)
            if token_hash is not None and not row:
                self._clear_cookie(response, self.session_cookie)
            return response
        except (_AuthError, sqlite3.Error) as error:
            return self._error(request, error)

    @_bounded
    async def logout(self, request: Request) -> Response:
        try:
            await self._json(request, set())
            token_hash = _cookie_hash(request, self.session_cookie)
            browser_hash = _cookie_hash(request, self.preauth_cookie)

            def revoke():
                with self._connection(write=True) as db:
                    self._prune(db, int(self._clock()))
                    db.execute("DELETE FROM wallet_challenges WHERE browser_hash = ?", (browser_hash,))
                    db.execute("DELETE FROM wallet_sessions WHERE token_hash = ? OR browser_hash = ?",
                               (token_hash, browser_hash))

            await asyncio.to_thread(revoke)
            response = self._response(request, {"ok": True, "authenticated": False})
            self._clear_cookie(response, self.session_cookie)
            self._clear_cookie(response, self.preauth_cookie)
            return response
        except (_AuthError, sqlite3.Error) as error:
            return self._error(request, error)
