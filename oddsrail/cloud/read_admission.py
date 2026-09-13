"""Small process-local admission/cache for public reads, not edge DDoS protection.

One instance is shared by market search, URL resolution and public portfolio
reads. There is no waiting queue for a new upstream job. Identical requests may
share an active job, with a bounded number of waiters. Only successful results
chosen by the caller are cached, as bounded JSON bytes; each caller receives
its own decoded object. Wallet-address entries stay in server memory only.
"""

from __future__ import annotations

import asyncio
import collections
import json
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class ReadBusy(Exception):
    """No upstream capacity is available; the HTTP caller should retry later."""

    retry_after = 2


@dataclass
class _Flight:
    task: asyncio.Task
    waiters: int = 0


class ReadAdmission:
    def __init__(self, *, max_active=8, max_waiters=32, max_entries=128,
                 max_bytes=4 * 1024 * 1024, max_entry_bytes=512 * 1024,
                 timeout=20.0, clock=None):
        for value in (max_active, max_waiters, max_entries, max_bytes, max_entry_bytes):
            if type(value) is not int or value < 1:
                raise ValueError("Read limits must be positive integers")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Read timeout must be positive")
        self.max_active, self.max_waiters = max_active, max_waiters
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.max_entry_bytes, self.timeout = max_entry_bytes, timeout
        self._clock = clock or time.monotonic
        self._cache = collections.OrderedDict()
        self._bytes = 0
        self._flights: dict[str, _Flight] = {}

    def _drop(self, key):
        _, payload = self._cache.pop(key)
        self._bytes -= len(payload)

    def _prune(self):
        now = self._clock()
        for key, (expires, _) in list(self._cache.items()):
            if expires <= now:
                self._drop(key)

    def _store(self, key, payload, ttl):
        if ttl <= 0 or len(payload) > min(self.max_entry_bytes, self.max_bytes):
            return
        self._prune()
        if key in self._cache:
            self._drop(key)
        while self._cache and (len(self._cache) >= self.max_entries or self._bytes + len(payload) > self.max_bytes):
            self._drop(next(iter(self._cache)))
        self._cache[key] = (self._clock() + ttl, payload)
        self._bytes += len(payload)

    async def _run(self, key, loader, ttl, cache_if):
        try:
            async with asyncio.timeout(self.timeout):
                value = await loader()
            payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if cache_if(value):
                self._store(key, payload, ttl)
            return payload
        finally:
            self._flights.pop(key, None)

    def _finished(self, key, task):
        # Cancellation before the coroutine starts does not run its finally.
        flight = self._flights.get(key)
        if flight is not None and flight.task is task:
            self._flights.pop(key, None)
        # A disconnected last waiter may leave a completed failing task. Read
        # its exception even then; awaiting callers still receive the exception.
        if not task.cancelled():
            task.exception()

    async def get(self, key: str, loader: Callable[[], Awaitable[dict]], *, ttl: float,
                  cache_if: Callable[[dict], bool]) -> dict:
        if not isinstance(key, str) or len(key) > 2200:
            raise ValueError("Read cache key is too large")
        if not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl < 0:
            raise ValueError("Read cache TTL must be finite and nonnegative")
        self._prune()
        cached = self._cache.get(key)
        if cached:
            self._cache.move_to_end(key)
            return json.loads(cached[1])
        flight = self._flights.get(key)
        if flight is not None and flight.task.cancelling():
            raise ReadBusy()
        if flight is None:
            if len(self._flights) >= self.max_active:
                raise ReadBusy()
            task = asyncio.create_task(self._run(key, loader, ttl, cache_if))
            task.add_done_callback(lambda task: self._finished(key, task))
            flight = _Flight(task)
            self._flights[key] = flight
        if flight.waiters >= self.max_waiters:
            raise ReadBusy()
        flight.waiters += 1
        try:
            # One cancelled HTTP request must not cancel another caller's read.
            return json.loads(await asyncio.shield(flight.task))
        finally:
            flight.waiters -= 1
            if flight.waiters == 0 and not flight.task.done():
                flight.task.cancel()


def market_success(value):
    return isinstance(value, dict) and value.get("ok") is True and isinstance(value.get("markets"), list)


def portfolio_success(value):
    """Unavailable cash is expected; unresolved or partial public reads are not cached."""
    return (isinstance(value, dict) and value.get("ok") is True
            and isinstance(value.get("account"), dict) and value["account"].get("status") == "resolved"
            and all(isinstance(value.get(section), dict) and value[section].get("status") == "ok"
                    and value[section].get("truncated") is False for section in ("positions", "history"))
            and isinstance(value.get("summary"), dict) and value["summary"].get("holdings_status") == "ok")
