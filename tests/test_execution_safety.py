"""Offline regressions for account caps and ambiguous live order responses.

Use the installed SDK's real paginator with a fake fetch function, and replace
the secure client. No key, exchange call, automatic retry, or home ledger.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from polymarket.pagination import AsyncPaginator, Page

from oddsrail import guard, polymarket as pm, trading


class FakeClient:
    def __init__(self):
        self.pages = {None: Page(items=(), has_more=False)}
        self.fetches = []
        self.submissions = []
        self.response = {"ok": True, "id": "offline-order"}
        self.submission_error = None

    def list_open_orders(self):
        async def fetch(cursor):
            self.fetches.append(cursor)
            page = self.pages[cursor]
            if isinstance(page, Exception):
                raise page
            return page

        return AsyncPaginator(fetch)

    async def place_limit_order(self, **order):
        self.submissions.append(order)
        if self.submission_error:
            raise self.submission_error
        return self.response


@pytest.fixture
def client(monkeypatch, tmp_path):
    fake = FakeClient()
    monkeypatch.setenv("ODDSRAIL_DRY_RUN", "0")
    monkeypatch.setenv("ODDSRAIL_PAPER", "0")
    monkeypatch.setenv("ODDSRAIL_PAPER_LEDGER", str(tmp_path / "paper.json"))
    guard.reset_session()

    async def secure():
        return fake

    async def no_network(*args, **kwargs):
        pytest.fail("execution safety tests must never make an HTTP request")

    monkeypatch.setattr(trading, "_client", secure)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    yield fake
    guard.reset_session()


async def place():
    return await trading.place_order("offline-token", "BUY", 0.5, 10)


def assert_refused_without_submission(result, client):
    assert result["accepted"] is False
    assert result["execution_state"] == "not_submitted"
    assert result["blocked_by"] == "guardrail"
    assert result["rule"] == "max_open_orders"
    assert client.submissions == []
    assert guard.status()["session_live_orders"] == 0
    assert guard.status()["session_notional_used_usd"] == 0


@pytest.mark.parametrize("failure_page", [None, "second"])
async def test_unavailable_account_count_refuses_order(client, monkeypatch, failure_page):
    monkeypatch.setenv("ODDSRAIL_MAX_OPEN_ORDERS", "3")
    client.pages[None] = Page(items=("first-order",), has_more=True, next_cursor="second")
    client.pages[failure_page] = httpx.ConnectError("account read failed")

    result = await place()

    assert_refused_without_submission(result, client)
    assert result["reason"] == "open_orders_unavailable"
    assert result["requested"] is None
    assert result["error_type"] == "ConnectError"


async def test_orders_on_later_pages_enforce_account_cap(client, monkeypatch):
    monkeypatch.setenv("ODDSRAIL_MAX_OPEN_ORDERS", "3")
    client.pages = {
        None: Page(items=("one",), has_more=True, next_cursor="second"),
        "second": Page(items=("two", "three"), has_more=True, next_cursor="unused"),
    }

    result = await place()

    assert_refused_without_submission(result, client)
    assert result["requested"] == 3
    assert client.fetches == [None, "second"]


async def test_complete_multiple_pages_below_cap_submit_once(client, monkeypatch):
    monkeypatch.setenv("ODDSRAIL_MAX_OPEN_ORDERS", "4")
    client.pages = {
        None: Page(items=("one",), has_more=True, next_cursor="second"),
        "second": Page(items=("two",), has_more=True, next_cursor="third"),
        "third": Page(items=(), has_more=False),
    }

    result = await place()

    assert result["accepted"] is True
    assert result["execution_state"] == "accepted"
    assert client.fetches == [None, "second", "third"]
    assert len(client.submissions) == 1
    assert guard.status()["session_notional_used_usd"] == 5


@pytest.mark.parametrize("bad_page", [
    Page(items=(), has_more=True, next_cursor=None),
    Page(items=(), has_more=True, next_cursor=""),
    SimpleNamespace(items=(), has_more=None),
    SimpleNamespace(items=()),
])
async def test_malformed_pagination_never_permits_submission(client, monkeypatch, bad_page):
    monkeypatch.setenv("ODDSRAIL_MAX_OPEN_ORDERS", "3")
    client.pages[None] = bad_page

    result = await place()

    assert_refused_without_submission(result, client)
    assert result["reason"] == "open_orders_unavailable"


async def test_repeating_cursor_refuses_without_looping(client, monkeypatch):
    monkeypatch.setenv("ODDSRAIL_MAX_OPEN_ORDERS", "3")
    client.pages = {
        None: Page(items=(), has_more=True, next_cursor="repeat"),
        "repeat": Page(items=(), has_more=True, next_cursor="repeat"),
    }

    result = await place()

    assert_refused_without_submission(result, client)
    assert client.fetches == [None, "repeat"]


async def test_page_budget_exhaustion_refuses_partial_count(client, monkeypatch):
    monkeypatch.setenv("ODDSRAIL_MAX_OPEN_ORDERS", "3")
    monkeypatch.setattr(trading, "_OPEN_ORDER_COUNT_MAX_PAGES", 2)
    client.pages = {
        None: Page(items=(), has_more=True, next_cursor="second"),
        "second": Page(items=(), has_more=True, next_cursor="third"),
    }

    result = await place()

    assert_refused_without_submission(result, client)
    assert client.fetches == [None, "second"]
    assert "page limit" in result["error"]


async def test_account_read_deadline_refuses_without_submitting(client, monkeypatch):
    monkeypatch.setenv("ODDSRAIL_MAX_OPEN_ORDERS", "3")
    monkeypatch.setattr(trading, "_OPEN_ORDER_COUNT_TIMEOUT_SECONDS", 0.01)

    async def stalled_fetch(cursor):
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "list_open_orders", lambda: AsyncPaginator(stalled_fetch))
    result = await place()

    assert_refused_without_submission(result, client)
    assert result["error_type"] == "TimeoutError"


async def test_unconfigured_cap_does_not_require_account_read(client):
    client.pages = {}
    result = await place()
    assert result["accepted"] is True
    assert client.fetches == []
    assert len(client.submissions) == 1


@pytest.mark.parametrize("error", [TimeoutError("no response"),
                                  httpx.ReadError("connection lost"),
                                  ValueError("unparseable submission result")])
async def test_submission_errors_are_unknown_and_never_retried(client, error):
    client.submission_error = error
    result = await place()

    assert result["accepted"] is False
    assert result["execution_state"] == "unknown"
    assert "rejected_code" not in result
    assert "open_orders and my_fills" in result["note"]
    assert "Do not retry automatically" in result["note"]
    assert len(client.submissions) == 1
    assert guard.status()["session_notional_used_usd"] == 5


async def test_failure_before_order_call_is_not_submitted(client, monkeypatch):
    async def failed_setup():
        raise httpx.ConnectError("authentication unavailable")

    monkeypatch.setattr(trading, "_client", failed_setup)
    result = await place()

    assert result["execution_state"] == "not_submitted"
    assert result["accepted"] is False
    assert client.submissions == []
    assert guard.status()["session_live_orders"] == 0


async def test_unparseable_exchange_response_is_unknown(client, monkeypatch):
    def cannot_dump(response):
        raise ValueError("unparseable response")

    monkeypatch.setattr(pm, "dump", cannot_dump)
    result = await place()

    assert result["execution_state"] == "unknown"
    assert "open_orders and my_fills" in result["note"]
    assert len(client.submissions) == 1


@pytest.mark.parametrize("response", [{}, None, {"ok": "false"}, {"ok": None}, {"ok": 1}])
async def test_unrecognised_acceptance_value_is_unknown(client, response):
    client.response = response
    result = await place()
    assert result["execution_state"] == "unknown"
    assert result["accepted"] is False
    assert "rejected_code" not in result
    assert "open_orders and my_fills" in result["note"]


async def test_explicit_exchange_rejection_remains_distinct(client):
    client.response = {"ok": False, "code": "INSUFFICIENT_FUNDS", "message": "balance too low"}
    result = await place()
    assert result["execution_state"] == "rejected"
    assert result["accepted"] is False
    assert result["rejected_code"] == "INSUFFICIENT_FUNDS"


async def test_dry_run_with_account_cap_never_reads_or_submits(client, monkeypatch):
    monkeypatch.setenv("ODDSRAIL_DRY_RUN", "1")
    monkeypatch.setenv("ODDSRAIL_MAX_OPEN_ORDERS", "3")
    client.pages = {}
    result = await place()
    assert result["dry_run"] is True
    assert client.fetches == []
    assert client.submissions == []
