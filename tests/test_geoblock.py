"""Tests for jurisdiction awareness — the paths where a blocked agent must
get an actionable answer instead of a retry loop or a false "no such market".

All offline: the preflight fetcher is never called; its pure mapping is."""

import json

import httpx
import pytest

from oddsrail import geo, trading
from oddsrail.kalshi import InterceptedResponseError
from oddsrail.server import _err_obj, find_markets
import oddsrail.kalshi as kx_mod
import oddsrail.polymarket as pm_mod


# --------------------------------------------------------------------------- #
# verdict mapping — the lossy boolean, un-flattened                            #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("country,region,blocked,want", [
    ("IR", None, True, "blocked"),       # OFAC: cannot even close
    ("SY", None, True, "blocked"),
    ("UA", "43", True, "blocked"),       # Crimea — region-scoped
    ("UA", "30", True, "close_only"),    # Kyiv is NOT OFAC-scoped
    ("US", None, True, "close_only"),    # the big middle tier
    ("GB", None, True, "close_only"),
    ("ZZ", None, True, "close_only"),    # unknown-but-blocked defaults DOWN
    ("IE", None, True, "permitted"),     # frontend-only: API not restricted
    ("JP", None, True, "permitted"),
    ("TR", None, False, "permitted"),
    ("KZ", None, False, "permitted"),
    (None, None, None, "unknown"),       # endpoint unreachable
])
def test_verdict_tiers(country, region, blocked, want):
    assert geo._verdict(country, region, blocked) == want


# --------------------------------------------------------------------------- #
# failure classification — a 403, a reset, and an ISP page are different       #
# --------------------------------------------------------------------------- #

def test_403_and_451_read_as_geo_suspected():
    assert geo.classify(Exception("Forbidden"), 403) == "geo_suspected"
    assert geo.classify(Exception("gone"), 451) == "geo_suspected"


def test_kalshi_403_keeps_the_tool_hint():
    # A Kalshi 403 is most often credentials or a bot-check; it must never
    # inherit Polymarket's jurisdiction verdict or wording.
    assert geo.classify(Exception("Forbidden"), 403, venue="kalshi") is None
    out = _err_obj(FakeSDKError("forbidden", status=403),
                   hint="needs KALSHI_KEY_ID", host="the Kalshi API")
    assert out["hint"] == "needs KALSHI_KEY_ID"
    assert "failure_class" not in out


def test_a_received_status_is_never_unreachable():
    # Any HTTP status proves the venue answered — even if the message echoes
    # body text like "connection refused", no connection-level class applies.
    assert geo.classify(Exception("connection refused"), 500) is None
    assert geo.classify(Exception("ssl handshake blah"), 404) is None


def test_sdk_transport_wrapper_is_classified_by_its_cause():
    # The Polymarket SDK wraps EVERY httpx.HTTPError in TransportError.
    class TransportError(Exception):
        pass
    slow = TransportError("Request failed")
    slow.__cause__ = httpx.ReadTimeout("timed out")
    assert geo.classify(slow) is None      # slow venue != filter; order may rest
    dead = TransportError("Request failed")
    dead.__cause__ = httpx.ConnectError("boom")
    assert geo.classify(dead) == "unreachable"


def test_unreachable_hint_never_claims_nothing_was_sent():
    h = geo.HINTS["unreachable"]
    assert "nothing was sent" not in h.lower()
    assert "open_orders" in h              # tells the agent how to resolve it


def test_connection_failures_read_as_unreachable():
    req = httpx.Request("GET", "https://clob.polymarket.com/")
    assert geo.classify(httpx.ConnectError("boom", request=req)) == "unreachable"
    assert geo.classify(Exception("[Errno 54] Connection reset by peer")) == "unreachable"
    assert geo.classify(Exception("SSL: handshake reset by peer")) == "unreachable"


def test_html_where_json_expected_reads_as_intercepted():
    try:
        json.loads("<!DOCTYPE html>")
    except json.JSONDecodeError as e:
        assert geo.classify(e) == "intercepted"
    e = InterceptedResponseError("https://x/y", 200, "text/html")
    assert geo.classify(e) == "intercepted"


def test_ordinary_errors_are_not_claimed():
    assert geo.classify(ValueError("side must be BUY or SELL")) is None
    assert geo.classify(KeyError("token_id")) is None


def test_every_hint_formats_cleanly():
    for k, v in geo.HINTS.items():
        assert v.format(host="the venue")  # no stray braces, non-empty


# --------------------------------------------------------------------------- #
# _err_obj — status must survive both exception shapes                         #
# --------------------------------------------------------------------------- #

class FakeSDKError(Exception):
    """Shaped like polymarket.errors.RequestRejectedError: carries
    .status/.code and has neither .response nor .request."""
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


def test_err_recovers_sdk_shaped_status():
    out = _err_obj(FakeSDKError("no orderbook exists", status=404, code="E404"))
    assert out["http_status"] == 404
    assert out["venue_code"] == "E404"


def test_err_still_reads_httpx_shaped_status():
    req = httpx.Request("GET", "https://gamma-api.polymarket.com/x")
    resp = httpx.Response(403, request=req)
    e = httpx.HTTPStatusError("forbidden", request=req, response=resp)
    out = _err_obj(e)
    assert out["http_status"] == 403
    assert out["failure_class"] == "geo_suspected"
    assert "server_info" in out["hint"]


def test_err_survives_httpx_error_with_unset_request():
    # httpx's .request property RAISES when unset; the error path must not.
    out = _err_obj(httpx.ConnectError("boom"))
    assert out["failure_class"] == "unreachable"


def test_err_keeps_the_tool_hint_when_nothing_geo_is_detected():
    out = _err_obj(ValueError("bad slug"), hint="use the slug field")
    assert out["hint"] == "use the slug field"
    assert "failure_class" not in out


# --------------------------------------------------------------------------- #
# order-response interpretation — an unrecognised shape is never "posted"      #
# --------------------------------------------------------------------------- #

INTENT = {"token_id": "1", "side": "BUY"}


def test_ok_true_is_accepted():
    out = trading._interpret_order_response({"ok": True, "id": "0xabc"}, INTENT)
    assert out["accepted"] is True


def test_ok_false_is_rejected_with_reason():
    out = trading._interpret_order_response(
        {"ok": False, "code": "GEO", "message": "restricted"}, INTENT)
    assert out["accepted"] is False
    assert out["rejected_code"] == "GEO"


@pytest.mark.parametrize("resp", [{}, {"id": "0xabc"}, ["weird"], None])
def test_unrecognised_shape_is_not_confirmed_either_way(resp):
    out = trading._interpret_order_response(resp, INTENT)
    assert out["accepted"] is False
    assert "open_orders" in out["note"]  # tells the agent how to resolve it


# --------------------------------------------------------------------------- #
# find_markets — a total outage must not read as "no such market"              #
# --------------------------------------------------------------------------- #

async def test_total_outage_is_a_failed_search_not_an_empty_one(monkeypatch):
    async def down(*a, **k):
        raise httpx.ConnectError("boom")
    monkeypatch.setattr(pm_mod, "search_markets", down)
    monkeypatch.setattr(kx_mod, "search_markets_detailed", down)
    out = json.loads(await find_markets("world cup"))
    assert out["markets"] == []
    assert len(out["venue_errors"]) == 2
    assert out["note"].startswith("NO MARKETS RETURNED")
    assert "server_info" in out["note"]


async def test_one_sided_outage_is_flagged_as_partial(monkeypatch):
    async def ok_pm(*a, **k):
        return []
    async def down(*a, **k):
        raise httpx.ConnectError("boom")
    monkeypatch.setattr(pm_mod, "search_markets", ok_pm)
    monkeypatch.setattr(kx_mod, "search_markets_detailed", down)
    out = json.loads(await find_markets("world cup"))
    assert out["note"].startswith("PARTIAL RESULT: the kalshi side")


# --------------------------------------------------------------------------- #
# preflight cache plumbing (no network)                                        #
# --------------------------------------------------------------------------- #

def test_cached_verdict_upgrades_polymarket_403_to_geo_blocked(monkeypatch):
    monkeypatch.setattr(geo, "_preflight_cache",
                        (0.0, {"polymarket_orders": "close_only"}))
    assert geo.classify(Exception("Forbidden"), 403,
                        venue="polymarket") == "geo_blocked"
    # ...but only for Polymarket: the verdict is about Polymarket's list.
    assert geo.classify(Exception("Forbidden"), 403, venue="kalshi") is None
    assert geo.classify(Exception("Forbidden"), 403) == "geo_suspected"
    monkeypatch.setattr(geo, "_preflight_cache", None)
    assert geo.classify(Exception("Forbidden"), 403,
                        venue="polymarket") == "geo_suspected"
