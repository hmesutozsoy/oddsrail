"""Bounded public-read work and caching, with no network or operator state."""

import asyncio
import json
import os
import subprocess
import sys

import pytest

from oddsrail.cloud.app import RateLimiter
from oddsrail.cloud.read_admission import ReadAdmission, ReadBusy, market_success, portfolio_success


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


async def ready_value():
    return {"ok": True, "markets": [{"title": "Example"}]}


async def test_identical_concurrent_reads_share_one_job_and_independent_results():
    gate = ReadAdmission()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await ready_value()

    tasks = [asyncio.create_task(gate.get("same", loader, ttl=10, cache_if=market_success)) for _ in range(10)]
    await entered.wait()
    assert calls == 1 and len(gate._flights) == 1
    release.set()
    results = await asyncio.gather(*tasks)
    results[0]["markets"][0]["title"] = "Changed locally"
    assert all(result["markets"][0]["title"] == "Example" for result in results[1:])
    assert (await gate.get("same", loader, ttl=10, cache_if=market_success))["markets"][0]["title"] == "Example"
    assert calls == 1


async def test_saturation_rejects_distinct_work_but_allows_cache_and_bounded_coalescing():
    gate = ReadAdmission(max_active=1, max_waiters=2)
    await gate.get("cached", ready_value, ttl=10, cache_if=market_success)
    entered, release = asyncio.Event(), asyncio.Event()

    async def loader():
        entered.set()
        await release.wait()
        return await ready_value()

    first = asyncio.create_task(gate.get("running", loader, ttl=10, cache_if=market_success))
    await entered.wait()
    joined = asyncio.create_task(gate.get("running", loader, ttl=10, cache_if=market_success))
    await asyncio.sleep(0)
    with pytest.raises(ReadBusy):
        await gate.get("different", loader, ttl=10, cache_if=market_success)
    with pytest.raises(ReadBusy):
        await gate.get("running", loader, ttl=10, cache_if=market_success)
    assert (await gate.get("cached", ready_value, ttl=10, cache_if=market_success))["ok"]
    release.set()
    await asyncio.gather(first, joined)
    assert not gate._flights
    assert (await gate.get("different", ready_value, ttl=10, cache_if=market_success))["ok"]


async def test_ttl_expires_at_boundary_and_refresh_uses_completion_time():
    clock = Clock()
    gate = ReadAdmission(clock=clock)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        clock.now += 2
        return await ready_value()

    await gate.get("key", loader, ttl=10, cache_if=market_success)
    clock.now = 11.999
    await gate.get("key", loader, ttl=10, cache_if=market_success)
    assert calls == 1
    clock.now = 12
    await gate.get("key", loader, ttl=10, cache_if=market_success)
    assert calls == 2


async def test_cache_count_and_byte_caps_use_lru_and_skip_oversized_entries():
    gate = ReadAdmission(max_entries=2)
    for key in ("a", "b", "a", "c"):
        await gate.get(key, ready_value, ttl=10, cache_if=market_success)
    assert list(gate._cache) == ["a", "c"]
    size = len(json.dumps(await ready_value(), separators=(",", ":")).encode())
    gate = ReadAdmission(max_entries=10, max_bytes=size * 2, max_entry_bytes=size)
    for key in ("a", "b", "c"):
        await gate.get(key, ready_value, ttl=10, cache_if=market_success)
    assert gate._bytes == size * 2 and list(gate._cache) == ["b", "c"]
    async def big():
        return {"ok": True, "markets": ["x" * 1000]}
    assert (await gate.get("big", big, ttl=10, cache_if=market_success))["ok"]
    assert "big" not in gate._cache and gate._bytes == size * 2


async def test_expired_entries_are_removed_even_for_different_keys():
    clock = Clock()
    gate = ReadAdmission(clock=clock)
    await gate.get("old", ready_value, ttl=5, cache_if=market_success)
    clock.now = 5
    await gate.get("new", ready_value, ttl=5, cache_if=market_success)
    assert list(gate._cache) == ["new"]


async def test_zero_ttl_disables_storage_and_invalid_ttl_starts_no_job():
    gate = ReadAdmission()
    calls = 0
    async def loader():
        nonlocal calls
        calls += 1
        return await ready_value()
    for _ in range(2):
        await gate.get("key", loader, ttl=0, cache_if=market_success)
    assert calls == 2 and not gate._cache
    for ttl in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            await gate.get("key", loader, ttl=ttl, cache_if=market_success)
    assert calls == 2 and not gate._flights


@pytest.mark.parametrize("options", [{"max_active": 0}, {"max_waiters": 0}, {"max_entries": 0},
                                     {"max_bytes": 0}, {"max_entry_bytes": 0}, {"timeout": 0},
                                     {"timeout": float("nan")}, {"timeout": float("inf")}])
def test_invalid_read_bounds_are_rejected(options):
    with pytest.raises(ValueError):
        ReadAdmission(**options)


async def test_exception_and_error_payload_are_not_cached_and_free_capacity():
    gate = ReadAdmission(max_active=1)
    calls = 0
    async def fail():
        nonlocal calls
        calls += 1
        raise RuntimeError("upstream failed")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="upstream failed"):
            await gate.get("fail", fail, ttl=10, cache_if=market_success)
    async def error_payload():
        nonlocal calls
        calls += 1
        return {"ok": False, "error": "unavailable"}
    for _ in range(2):
        assert not (await gate.get("error", error_payload, ttl=10, cache_if=market_success))["ok"]
    assert calls == 4 and not gate._cache and not gate._flights


async def test_timeout_and_cancelled_waiter_do_not_leak_capacity():
    gate = ReadAdmission(max_active=1, timeout=.01)
    async def slow():
        await asyncio.Event().wait()
    with pytest.raises(TimeoutError):
        await gate.get("slow", slow, ttl=10, cache_if=market_success)
    assert not gate._flights
    assert (await gate.get("new", ready_value, ttl=10, cache_if=market_success))["ok"]


async def test_cancelling_one_waiter_keeps_the_shared_read_alive():
    gate = ReadAdmission()
    entered, release = asyncio.Event(), asyncio.Event()
    async def loader():
        entered.set()
        await release.wait()
        return await ready_value()
    first = asyncio.create_task(gate.get("key", loader, ttl=10, cache_if=market_success))
    await entered.wait()
    second = asyncio.create_task(gate.get("key", loader, ttl=10, cache_if=market_success))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done()
    release.set()
    assert (await second)["ok"]


async def test_cancelling_last_waiter_cleans_up_even_before_loader_starts():
    gate = ReadAdmission(max_active=1)
    first = asyncio.create_task(gate.get("key", ready_value, ttl=10, cache_if=market_success))
    await asyncio.sleep(0)
    # Cancel the new upstream task before it gets its first event-loop turn.
    gate._flights["key"].task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.sleep(0)
    assert not gate._flights
    assert (await gate.get("key", ready_value, ttl=10, cache_if=market_success))["ok"]


async def test_disconnected_last_waiter_cancels_unneeded_work():
    gate = ReadAdmission(max_active=1)
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def loader():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            stopped.set()
    first = asyncio.create_task(gate.get("key", loader, ttl=10, cache_if=market_success))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(stopped.wait(), timeout=1)
    assert not gate._flights and not gate._cache


@pytest.mark.parametrize("change", ["account", "positions", "history", "holdings", "truncated"])
async def test_partial_or_unresolved_portfolio_never_enters_cache(change):
    value = {"ok": True, "account": {"status": "resolved"},
             "positions": {"status": "ok", "truncated": False},
             "history": {"status": "ok", "truncated": False},
             "summary": {"holdings_status": "ok", "cash_usd": None}}
    assert portfolio_success(value)
    if change == "holdings":
        value["summary"]["holdings_status"] = "unavailable"
    elif change == "truncated":
        value["positions"]["truncated"] = True
    else:
        value[change]["status"] = "unresolved" if change == "account" else "partial"
    gate = ReadAdmission()
    calls = 0
    async def loader():
        nonlocal calls
        calls += 1
        return value
    for _ in range(2):
        await gate.get("wallet", loader, ttl=10, cache_if=portfolio_success)
    assert calls == 2 and not gate._cache


def test_rate_map_is_bounded_without_resetting_active_clients():
    clock = Clock()
    limiter = RateLimiter(limit=2, window=10, max_keys=2, clock=clock)
    assert limiter.allow("a") and limiter.allow("b")
    for i in range(100):
        assert not limiter.allow("new-" + str(i))
    assert set(limiter._hits) == {"a", "b"}
    assert limiter.allow("a") and not limiter.allow("a")
    clock.now = 10
    assert limiter.allow("c")
    assert set(limiter._hits) == {"c"}


def test_rate_map_prunes_idle_keys_and_rejects_oversized_keys():
    clock = Clock()
    limiter = RateLimiter(limit=2, window=120, max_keys=3, clock=clock)
    assert not limiter.allow("x" * 513)
    assert limiter.allow("old")
    clock.now = 61
    assert limiter.allow("active")
    clock.now = 121
    assert limiter.allow("new")
    assert set(limiter._hits) == {"active", "new"}
    assert limiter.allow("active") and not limiter.allow("active")


def test_public_routes_share_admission_cache_and_preserve_private_http_headers(tmp_path):
    # Isolate build_app's MCP registration and auth globals from other tests.
    # ASGITransport makes in-process requests; every upstream loader is a stub.
    script = r'''
import asyncio
from collections import Counter
import httpx
from oddsrail import market_selection
from oddsrail.cloud import portfolio, read_admission
from oddsrail.cloud.app import build_app

async def main():
    original = read_admission.ReadAdmission
    gate = original(max_active=2)
    read_admission.ReadAdmission = lambda: gate
    calls = Counter()
    release = asyncio.Event()
    started = asyncio.Event()
    active = 0
    owner = "0x" + "ab" * 20
    partial_owner = "0x" + "cd" * 20
    async def market(q):
        nonlocal active
        calls["market:" + q] += 1
        if q.startswith("block"):
            active += 1
            if active == 2:
                started.set()
            await release.wait()
        if q == "failure":
            raise market_selection.SelectionError("Unavailable", "upstream_unavailable")
        return {"ok": True, "markets": [{"title": q}]}
    async def account(address):
        calls["portfolio:" + address] += 1
        return {"ok": True, "address": address, "account": {"status": "resolved"},
                "positions": {"status": "partial" if address == partial_owner else "ok", "truncated": address == partial_owner},
                "history": {"status": "ok", "truncated": False},
                "summary": {"holdings_status": "ok", "cash_usd": None, "portfolio_value_usd": None}}
    market_selection.search_selection = market
    market_selection.resolve_selection = market
    portfolio.load_portfolio = account
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787",
                                 headers={"origin": "https://oddsrail.app"}) as client:
        first = asyncio.create_task(client.get("/markets/search?q=block-a"))
        second = asyncio.create_task(client.get("/markets/resolve?q=block-b"))
        await asyncio.wait_for(started.wait(), timeout=2)
        joined = asyncio.create_task(client.get("/markets/search?q=block-a"))
        for path in ("/portfolio?address=" + owner, "/markets/search?q=third", "/markets/resolve?q=third"):
            response = await client.get(path)
            assert response.status_code == 503, response.text
            assert response.json()["code"] == "read_capacity"
            assert response.headers["retry-after"] == "2"
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["access-control-allow-origin"] == "https://oddsrail.app"
        assert not calls["portfolio:" + owner]
        release.set()
        for response in await asyncio.gather(first, second, joined):
            assert response.status_code == 200, response.text
        assert calls["market:block-a"] == calls["market:block-b"] == 1
        for _ in range(2):
            assert (await client.get("/markets/search?q=block-a")).status_code == 200
            assert (await client.get("/markets/resolve?q=block-b")).status_code == 200
            response = await client.get("/portfolio?address=" + owner)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            assert "set-cookie" not in response.headers
        assert calls["market:block-a"] == calls["market:block-b"] == calls["portfolio:" + owner] == 1
        # Endpoint namespace prevents cross-caching resolve vs search.
        assert (await client.get("/markets/resolve?q=block-a")).status_code == 200
        assert calls["market:block-a"] == 2
        for _ in range(2):
            assert (await client.get("/markets/search?q=failure")).status_code == 503
            assert (await client.get("/portfolio?address=" + partial_owner)).status_code == 200
        assert calls["market:failure"] == calls["portfolio:" + partial_owner] == 2
        for path in ("/markets/search?q=" + "x" * 161, "/markets/resolve?q=" + "x" * 2049):
            response = await client.get(path)
            assert response.status_code == 400, response.text
        for path in ("/markets/search", "/markets/resolve", "/portfolio"):
            assert (await client.options(path)).status_code == 204
    assert not gate._flights

asyncio.run(main())
'''
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ODDSRAIL_", "POLYMARKET_", "KALSHI_"))}
    env.update(ODDSRAIL_CLOUD_DATA=str(tmp_path), ODDSRAIL_CLOUD_URL="http://127.0.0.1:8787",
               ODDSRAIL_SCHEDULER="0", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
