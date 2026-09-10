"""SQLite store for the hosted server: accounts, OAuth clients and grants,
magic-link sign-ins.

One file in WAL mode behind a process-wide lock. The hosted server is a
single process and this traffic is orders of magnitude below sqlite's
ceiling; a bigger store is a later problem, and the schema is small enough
to move when it is one.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
    created REAL NOT NULL, last_login REAL);
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS auth_requests (
    id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL,
    created REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS magic_links (
    token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, req_id TEXT NOT NULL,
    created REAL NOT NULL, expires REAL NOT NULL, used REAL);
CREATE TABLE IF NOT EXISTS auth_codes (
    code_hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
    user_id TEXT NOT NULL, scopes TEXT NOT NULL, pair TEXT NOT NULL,
    created REAL NOT NULL, expires REAL, revoked REAL);
CREATE INDEX IF NOT EXISTS tokens_pair ON tokens(pair);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created REAL NOT NULL,
    expires REAL NOT NULL, revoked REAL);
CREATE TABLE IF NOT EXISTS agents (
    user_id TEXT PRIMARY KEY, name TEXT NOT NULL, strategy TEXT NOT NULL DEFAULT '',
    config TEXT NOT NULL, schedule TEXT NOT NULL DEFAULT 'off', created REAL NOT NULL,
    updated REAL NOT NULL, last_run REAL, last_result TEXT, runs INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS arena (
    user_id TEXT PRIMARY KEY, name TEXT NOT NULL, name_lc TEXT UNIQUE NOT NULL,
    strategy TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS magic_links_email ON magic_links(email, created);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, created REAL NOT NULL, user_id TEXT, agent TEXT,
    config TEXT NOT NULL, result TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS runs_agent ON runs(agent, created);
CREATE TABLE IF NOT EXISTS agent_runs (
    user_id TEXT NOT NULL, at REAL NOT NULL, equity REAL, orders INTEGER,
    decisions INTEGER, ok INTEGER NOT NULL DEFAULT 1, run_id TEXT);
CREATE INDEX IF NOT EXISTS agent_runs_user ON agent_runs(user_id, at);
"""


class DB:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._c.execute("PRAGMA journal_mode=WAL")
            self._c.execute("PRAGMA busy_timeout=5000")
            self._c.executescript(SCHEMA)

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            return self._c.execute(sql, params)

    def _one(self, sql: str, params=()) -> dict | None:
        with self._lock:
            r = self._c.execute(sql, params).fetchone()
        return dict(r) if r else None

    # ------------------------------- users -------------------------------- #

    def user(self, uid: str) -> dict | None:
        return self._one("SELECT * FROM users WHERE id=?", (uid,))

    def user_by_email(self, email: str) -> dict | None:
        return self._one("SELECT * FROM users WHERE email=?", (email,))

    def login_user(self, email: str) -> dict:
        """Find or create the account for a verified email; returns it."""
        now = time.time()
        with self._lock:
            row = self._c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if row is None:
                uid = "u_" + secrets.token_hex(8)
                self._c.execute("INSERT INTO users (id, email, created, last_login) VALUES (?,?,?,?)",
                                (uid, email, now, now))
            else:
                uid = row["id"]
                self._c.execute("UPDATE users SET last_login=? WHERE id=?", (now, uid))
        return self.user(uid)  # type: ignore[return-value]

    # --------------------------- OAuth clients ---------------------------- #

    def put_client(self, client_id: str, info_json: str) -> None:
        self._exec("INSERT OR REPLACE INTO oauth_clients (client_id, info, created) VALUES (?,?,?)",
                   (client_id, info_json, time.time()))

    def get_client(self, client_id: str) -> str | None:
        r = self._one("SELECT info FROM oauth_clients WHERE client_id=?", (client_id,))
        return r["info"] if r else None

    # --------------------- pending authorization requests ----------------- #

    def put_auth_request(self, rid: str, client_id: str, params: dict, ttl: float) -> None:
        now = time.time()
        self._exec("INSERT INTO auth_requests (id, client_id, params, created, expires) VALUES (?,?,?,?,?)",
                   (rid, client_id, json.dumps(params), now, now + ttl))

    def get_auth_request(self, rid: str) -> dict | None:
        r = self._one("SELECT * FROM auth_requests WHERE id=? AND expires>?", (rid, time.time()))
        if r:
            r["params"] = json.loads(r["params"])
        return r

    def delete_auth_request(self, rid: str) -> None:
        self._exec("DELETE FROM auth_requests WHERE id=?", (rid,))

    # ----------------------------- magic links ---------------------------- #

    def put_magic_link(self, token_hash: str, email: str, rid: str, ttl: float) -> None:
        now = time.time()
        self._exec("INSERT INTO magic_links (token_hash, email, req_id, created, expires) VALUES (?,?,?,?,?)",
                   (token_hash, email, rid, now, now + ttl))

    def get_magic_link(self, token_hash: str) -> dict | None:
        return self._one("SELECT * FROM magic_links WHERE token_hash=? AND used IS NULL AND expires>?",
                         (token_hash, time.time()))

    def use_magic_link(self, token_hash: str) -> bool:
        """Mark a link used. Atomic: exactly one caller wins a double click."""
        now = time.time()
        cur = self._exec("UPDATE magic_links SET used=? WHERE token_hash=? AND used IS NULL AND expires>?",
                         (now, token_hash, now))
        return cur.rowcount == 1

    def recent_links(self, email: str, window: float) -> int:
        r = self._one("SELECT COUNT(*) AS n FROM magic_links WHERE email=? AND created>?",
                      (email, time.time() - window))
        return int(r["n"]) if r else 0

    # -------------------------- authorization codes ----------------------- #

    def put_code(self, code_hash: str, data: dict, ttl: float) -> None:
        self._exec("INSERT INTO auth_codes (code_hash, data, expires) VALUES (?,?,?)",
                   (code_hash, json.dumps(data), time.time() + ttl))

    def get_code(self, code_hash: str) -> dict | None:
        r = self._one("SELECT data FROM auth_codes WHERE code_hash=? AND expires>?", (code_hash, time.time()))
        return json.loads(r["data"]) if r else None

    def delete_code(self, code_hash: str) -> None:
        self._exec("DELETE FROM auth_codes WHERE code_hash=?", (code_hash,))

    # -------------------------------- tokens ------------------------------ #

    def put_token(self, token_hash: str, kind: str, client_id: str, user_id: str,
                  scopes: list[str], pair: str, ttl: float | None) -> None:
        now = time.time()
        self._exec("INSERT INTO tokens (token_hash, kind, client_id, user_id, scopes, pair, created, expires) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (token_hash, kind, client_id, user_id, " ".join(scopes), pair, now,
                    now + ttl if ttl else None))

    def get_token(self, token_hash: str, kind: str) -> dict | None:
        return self._one("SELECT * FROM tokens WHERE token_hash=? AND kind=? AND revoked IS NULL "
                         "AND (expires IS NULL OR expires>?)", (token_hash, kind, time.time()))

    def revoke_pair(self, pair: str) -> None:
        self._exec("UPDATE tokens SET revoked=? WHERE pair=? AND revoked IS NULL", (time.time(), pair))

    # -------------------------------- arena ------------------------------- #

    def arena_by_name(self, name: str) -> dict | None:
        return self._one("SELECT * FROM arena WHERE name_lc=?", (name.lower(),))

    def arena_get(self, user_id: str) -> dict | None:
        return self._one("SELECT * FROM arena WHERE user_id=?", (user_id,))

    def arena_put(self, user_id: str, name: str, strategy: str) -> None:
        now = time.time()
        with self._lock:
            row = self._c.execute("SELECT created FROM arena WHERE user_id=?", (user_id,)).fetchone()
            created = row["created"] if row else now
            self._c.execute("INSERT OR REPLACE INTO arena (user_id, name, name_lc, strategy, created, updated) "
                            "VALUES (?,?,?,?,?,?)", (user_id, name, name.lower(), strategy, created, now))

    def arena_delete(self, user_id: str) -> bool:
        return self._exec("DELETE FROM arena WHERE user_id=?", (user_id,)).rowcount == 1

    def arena_list(self) -> list[dict]:
        with self._lock:
            rows = self._c.execute("SELECT * FROM arena ORDER BY created").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------ sessions ------------------------------ #

    def put_session(self, token_hash: str, user_id: str, ttl: float) -> None:
        now = time.time()
        self._exec("INSERT INTO sessions (token_hash, user_id, created, expires) VALUES (?,?,?,?)",
                   (token_hash, user_id, now, now + ttl))

    def get_session(self, token_hash: str) -> dict | None:
        return self._one("SELECT * FROM sessions WHERE token_hash=? AND revoked IS NULL AND expires>?",
                         (token_hash, time.time()))

    def revoke_session(self, token_hash: str) -> None:
        self._exec("UPDATE sessions SET revoked=? WHERE token_hash=?", (time.time(), token_hash))

    # ------------------------------- agents ------------------------------- #

    def agent_get(self, user_id: str) -> dict | None:
        r = self._one("SELECT * FROM agents WHERE user_id=?", (user_id,))
        if r:
            r["config"] = json.loads(r["config"] or "{}")
            r["last_result"] = json.loads(r["last_result"]) if r.get("last_result") else None
        return r

    def agent_put(self, user_id: str, name: str, strategy: str, config: dict, schedule: str) -> None:
        """Create or update the account's agent; run history survives an update."""
        now = time.time()
        self._exec("INSERT INTO agents (user_id, name, strategy, config, schedule, created, updated) "
                   "VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, "
                   "strategy=excluded.strategy, config=excluded.config, schedule=excluded.schedule, "
                   "updated=excluded.updated",
                   (user_id, name, strategy, json.dumps(config), schedule, now, now))

    def agent_delete(self, user_id: str) -> bool:
        return self._exec("DELETE FROM agents WHERE user_id=?", (user_id,)).rowcount == 1

    def agents_due(self, older_than: float) -> list[dict]:
        with self._lock:
            rows = self._c.execute("SELECT * FROM agents WHERE schedule='hourly' AND "
                                   "(last_run IS NULL OR last_run<?) ORDER BY COALESCE(last_run,0)",
                                   (older_than,)).fetchall()
        out = []
        for r in rows:
            d = dict(r); d["config"] = json.loads(d["config"] or "{}"); out.append(d)
        return out

    def agent_ran(self, user_id: str, result: dict, run_id: str | None = None) -> None:
        self._exec("UPDATE agents SET last_run=?, last_result=?, runs=runs+1 WHERE user_id=?",
                   (time.time(), json.dumps(result), user_id))
        self.put_agent_run(user_id, result, run_id)

    def agent_by_name(self, name: str) -> dict | None:
        r = self._one("SELECT * FROM agents WHERE lower(name)=?", (name.lower(),))
        if r:
            r["config"] = json.loads(r["config"] or "{}")
            r["last_result"] = json.loads(r["last_result"]) if r.get("last_result") else None
        return r

    # -------------------------------- runs -------------------------------- #

    def put_run(self, run_id: str, config: dict, result: dict,
                user_id: str | None = None, agent: str | None = None) -> None:
        self._exec("INSERT OR REPLACE INTO runs (id, created, user_id, agent, config, result) VALUES (?,?,?,?,?,?)",
                   (run_id, time.time(), user_id, agent, json.dumps(config), json.dumps(result)))

    def get_run(self, run_id: str) -> dict | None:
        r = self._one("SELECT * FROM runs WHERE id=?", (run_id,))
        if r:
            r["config"] = json.loads(r["config"] or "{}")
            r["result"] = json.loads(r["result"] or "{}")
        return r

    def agent_history(self, user_id: str, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._c.execute("SELECT at, equity, orders, decisions, ok, run_id FROM agent_runs "
                                   "WHERE user_id=? ORDER BY at DESC LIMIT ?", (user_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def put_agent_run(self, user_id: str, res: dict, run_id: str | None = None) -> None:
        self._exec("INSERT INTO agent_runs (user_id, at, equity, orders, decisions, ok, run_id) VALUES (?,?,?,?,?,?,?)",
                   (user_id, time.time(), res.get("equity"), res.get("orders"), res.get("decisions"),
                    1 if res.get("ok") else 0, run_id))

    def prune_runs(self, keep_days: float = 30.0) -> int:
        """Anonymous shared runs expire; runs belonging to an account stay."""
        cur = self._exec("DELETE FROM runs WHERE user_id IS NULL AND created<?",
                         (time.time() - keep_days * 86400,))
        return cur.rowcount

    # ------------------------------- hygiene ------------------------------ #

    def cleanup(self) -> None:
        now = time.time()
        self._exec("DELETE FROM auth_requests WHERE expires<?", (now,))
        self._exec("DELETE FROM magic_links WHERE expires<?", (now - 86400,))
        self._exec("DELETE FROM auth_codes WHERE expires<?", (now,))
        self._exec("DELETE FROM tokens WHERE (expires IS NOT NULL AND expires<?) OR revoked<?",
                   (now - 86400, now - 86400))
