"""Jurisdiction awareness: failure classification, agent hints, and the
Polymarket eligibility preflight.

Why this exists: both venues restrict trading by jurisdiction, and for most
restricted places the failure arrives at the ORDER, not the connection —
public reads answer normally, so the server looks healthy right up until a
trade is rejected. Separately, some national filters block the domains at the
network level, which surfaces as DNS/TLS failures or an ISP block page served
where JSON was expected. These are opposite problems with opposite remedies,
and an agent that cannot tell them apart retries blindly.

The preflight uses https://polymarket.com/api/geoblock — a documented endpoint
(docs.polymarket.com/api-reference/geoblock) whose own docs instruct builders
to "implement geoblock checks in your application to provide users with
appropriate feedback before they attempt to trade". It is ADVISORY ONLY and
never gates a trade here, because:
- it lives on the polymarket.com frontend host, not the API servers, so its
  availability is independent of the venues';
- its boolean is lossy: the frontend-only tier (Ireland/Japan/Netherlands)
  is expected to return blocked=true — verified via ?country=IE, not from a
  real egress — even though Polymarket's docs say the API is not restricted
  there, and it does not distinguish "close-only" from "cannot even exit";
- an IP verdict is not a compliance check — the venues' terms bind on
  residence, citizenship and incorporation, not on egress IP.

Tier tables below were read directly off Polymarket's API reference on
2026-08-31 (LIST_AS_OF). The docs page is the authority; the live endpoint is
the cross-check. Polymarket updates the list without notice.

Kalshi publishes NO equivalent endpoint (verified 2026-08-31: candidate paths
404, and the site makes no geo call) — its restrictions (Member Agreement §VI)
are enforced server-side at signup and order time.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json as _json

import httpx

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
GEOBLOCK_DOCS = "https://docs.polymarket.com/api-reference/geoblock"
# Direct PDF: the latest Member Agreement notice in Kalshi's public bucket
# as of LIST_AS_OF (the bare bucket prefix is not browsable and 404s).
KALSHI_NOTICES = ("https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Kalshi%20Exchange%20Notice%20(Updated%20Member%20Agreement)%20(22%20June%202026).pdf")
LIST_AS_OF = "2026-08-31"

# OFAC tier: blocked completely on frontend AND API — existing positions
# cannot even be closed. Ukraine is region-scoped (Crimea 43, Donetsk 14,
# Luhansk 09), not country-wide.
_OFAC_COUNTRIES = {"IR", "SY", "CU", "KP"}
_OFAC_UA_REGIONS = {"43", "14", "09"}

# Frontend-only tier: Polymarket's docs say the API is NOT restricted here,
# but the geoblock endpoint still returns blocked=true for them — the lossy
# boolean this module exists to un-flatten. (MT is sports-only.)
_FRONTEND_ONLY = {"IE", "JP", "NL", "MT"}


HINTS = {
    "geo_blocked":
        "Blocked by jurisdiction. Polymarket restricts order placement by "
        "country and this machine's IP is on its restricted list. Restricted "
        "jurisdictions are close-only or fully blocked: you may be able to "
        "reduce an existing position but not open a new one. Call server_info "
        "for this machine's verdict and see "
        "https://docs.polymarket.com/api-reference/geoblock for the current "
        "list. Do not retry — the same request from this network will be "
        "rejected again. Tell the operator; this needs a human decision, not "
        "a retry.",

    "geo_suspected":
        "Rejected with an HTTP status (403/451) commonly used for "
        "jurisdiction blocks rather than for bad input — a suspicion, not a "
        "diagnosis; the venue does not document its geo-rejection shape. "
        "Rewriting the request will not help. Call server_info first: it "
        "reports Polymarket's geoblock verdict for this machine's IP. If it "
        "says blocked, stop and report to the operator. If it says not "
        "blocked, this may be a bot-check or a transient venue error — retry "
        "at most once, after a pause.",

    "unreachable":
        "Could not get a response from {host}: the connection itself failed "
        "(DNS, TLS, reset, or refused). The usual cause is a network problem "
        "or a network-level filter between this machine and the venue rather "
        "than anything about the request; some countries block "
        "prediction-market domains outright. Do not retry in a loop — report "
        "to the operator, who needs to confirm the host resolves from this "
        "machine. If this happened while placing or cancelling an order, the "
        "outcome is NOT confirmed: call open_orders before retrying.",

    "intercepted":
        "{host} answered with HTML where JSON was expected. That usually "
        "means the request was intercepted by a proxy, captive portal or ISP "
        "block page and never reached the venue — not that the API changed "
        "shape. Treat this venue as unreachable and report to the operator; "
        "retrying returns the same page.",

    "preflight_unavailable":
        "Eligibility preflight unavailable: could not reach "
        "https://polymarket.com/api/geoblock. This means unknown, not "
        "permitted and not blocked. The venue itself remains the authority "
        "on whether an order is accepted.",
}


# The failure-shape classes httpx raises for connection-level trouble.
# Matched by NAME so this module never has to import the SDK. ReadTimeout is
# deliberately absent: a slow venue is not a filter, and telling an agent to
# stop on one would be wrong. The SDK's TransportError is NOT here — it wraps
# EVERY httpx.HTTPError, timeouts included, so it is classified by its CAUSE.
_UNREACHABLE_TYPES = ("ConnectError", "ConnectTimeout", "ReadError",
                      "RemoteProtocolError")
_UNREACHABLE_MSG = ("connection reset", "connection refused",
                    "name or service not known", "nodename nor servname",
                    "ssl", "certificate", "temporary failure in name")
_INTERCEPTED_MSG = ("html", "<!doctype", "expecting value")


def classify(e: Exception, status=None, venue=None) -> str | None:
    """Map an exception (+ optional HTTP status) to a failure class, or None.

    None means "keep the tool's own hint" — this only speaks up when it has
    something more specific to say than the tool does. venue is "polymarket",
    "kalshi" or None; the geo classes are Polymarket-worded and backed by the
    Polymarket preflight, so they are never attached to Kalshi failures — a
    Kalshi 403 is most often credentials or a bot-check, and misreading it as
    another venue's jurisdiction verdict would send the agent away from a
    venue it can use.
    """
    if status is not None:
        # A received HTTP status is proof the venue answered: no
        # connection-level class can be right, so decide on the status alone.
        if status in (403, 451) and venue != "kalshi":
            if venue == "polymarket" and cached_verdict() in ("blocked",
                                                              "close_only"):
                return "geo_blocked"
            return "geo_suspected"
        return None
    name = type(e).__name__
    if name == "TransportError":
        # The Polymarket SDK wraps every httpx.HTTPError in TransportError —
        # classify by the wrapped cause so a ReadTimeout (a slow venue, sent
        # and possibly executed) is never called "unreachable".
        cause = getattr(e, "__cause__", None)
        if cause is not None:
            return classify(cause, None, venue)
    if isinstance(e, _json.JSONDecodeError) or name == "InterceptedResponseError":
        return "intercepted"
    msg = str(e).lower()
    if name == "UnexpectedResponseError" and any(s in msg for s in _INTERCEPTED_MSG):
        return "intercepted"
    if name in _UNREACHABLE_TYPES:
        return "unreachable"
    if any(s in msg for s in _UNREACHABLE_MSG):
        return "unreachable"
    return None


def _verdict(country, region, ip_blocked) -> str:
    """Map the geoblock endpoint's lossy boolean back to something useful,
    using the tier tables from Polymarket's API reference (LIST_AS_OF).

    Countries on the blocklist that this module does not recognise fall to
    "close_only" — that is the correct default for the large middle tier, and
    it never claims more freedom than the boolean did.
    """
    if ip_blocked is None:
        return "unknown"
    if not ip_blocked:
        return "permitted"
    c = str(country or "").upper()
    if c in _OFAC_COUNTRIES:
        return "blocked"
    if c == "UA" and str(region or "") in _OFAC_UA_REGIONS:
        return "blocked"
    if c in _FRONTEND_ONLY:
        return "permitted"
    return "close_only"


# MCP servers can live for days; a process-lifetime cache would keep
# reporting day-old "ok" while every real call fails. 5 minutes keeps the
# probes cheap without letting the verdict rot.
_CACHE_TTL = 300.0

_preflight_cache: tuple[float, dict] | None = None


def cached_verdict() -> str | None:
    """The last preflight's polymarket_orders verdict, if one has run.
    Never triggers a network call — classify() must stay synchronous."""
    if _preflight_cache is None:
        return None
    return _preflight_cache[1].get("polymarket_orders")


async def preflight(timeout: float = 4.0) -> dict:
    """Polymarket eligibility check for this machine's egress IP.

    Advisory only, cached for _CACHE_TTL seconds, never raises. Any non-200,
    timeout, or unrecognised shape means "unknown", not "blocked" and not
    "permitted" — the venue itself remains the authority.
    """
    global _preflight_cache
    import time as _time
    if (_preflight_cache is not None
            and _time.monotonic() - _preflight_cache[0] < _CACHE_TTL):
        return _preflight_cache[1]
    checked = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    base = {"source": GEOBLOCK_URL, "checked_at": checked,
            "cache_ttl_seconds": int(_CACHE_TTL)}
    try:
        async with httpx.AsyncClient(timeout=timeout) as h:
            r = await h.get(GEOBLOCK_URL)
        d = r.json() if r.status_code == 200 else {}
        blocked = d.get("blocked")
        if not isinstance(blocked, bool):
            raise ValueError(f"no usable 'blocked' field (HTTP {r.status_code})")
        country, region = d.get("country"), d.get("region")
        verdict = _verdict(country, region, blocked)
        out = {**base, "country": country, "region": region,
               "ip_blocked": blocked, "polymarket_orders": verdict,
               "note": ("advisory only — IP-based, not a compliance check. "
                        "The venues' terms bind on residence, citizenship and "
                        f"incorporation. Tier tables as of {LIST_AS_OF}; "
                        f"authority: {GEOBLOCK_DOCS}")}
        if verdict == "permitted" and blocked and str(country or "").upper() in _FRONTEND_ONLY:
            out["note"] = ("only Polymarket's frontend is restricted here; "
                           "its docs say the API is not. " + out["note"])
    except Exception as e:
        out = {**base, "country": None, "region": None, "ip_blocked": None,
               "polymarket_orders": "unknown",
               "error": f"{type(e).__name__}: {e}",
               "note": HINTS["preflight_unavailable"]}
    _preflight_cache = (_time.monotonic(), out)
    return out


# The hosts oddsrail actually talks to — external-api.kalshi.com is what
# kalshi.py uses, NOT api.elections.kalshi.com; they resolve to different
# edges and can be filtered independently.
# (url, expects_json): for the endpoints that answer JSON, a 200 carrying
# HTML is an interception, not reachability — the flagship national-filter
# scenario must not read as "ok" here while every real call fails.
_PROBES = {
    "gamma-api.polymarket.com":
        ("https://gamma-api.polymarket.com/markets?limit=1", True),
    "clob.polymarket.com": ("https://clob.polymarket.com/", False),
    "data-api.polymarket.com": ("https://data-api.polymarket.com/", False),
    "external-api.kalshi.com":
        ("https://external-api.kalshi.com/trade-api/v2/exchange/status", True),
}
_TRADING_PROBES = {
    "relayer-v2.polymarket.com": ("https://relayer-v2.polymarket.com/", False),
}

_reachability_cache: tuple[float, str, dict] | None = None


async def _probe(client: httpx.AsyncClient, host: str, url: str,
                 expects_json: bool) -> tuple[str, str]:
    """Any HTTP response — even a 4xx — means the host is reachable; only a
    transport-level failure counts as unreachable. A JSON endpoint answering
    non-JSON is reported as intercepted, not ok."""
    try:
        r = await client.get(url)
    except Exception as e:
        return host, f"unreachable ({type(e).__name__})"
    if expects_json:
        try:
            r.json()
        except ValueError:
            return host, ("intercepted (non-JSON response — a proxy or block "
                          "page may be answering instead of the venue)")
    return host, "ok"


async def reachability(include_trading_hosts: bool = False) -> dict:
    """One cheap unauthenticated GET per host, concurrent, 3s timeout,
    cached for _CACHE_TTL seconds, never raising."""
    global _reachability_cache
    import time as _time
    probes = dict(_PROBES)
    if include_trading_hosts:
        probes.update(_TRADING_PROBES)
    now = _time.monotonic()
    if (_reachability_cache is not None
            and now - _reachability_cache[0] < _CACHE_TTL
            and set(_reachability_cache[2]) >= set(probes)):
        _, checked, hosts = _reachability_cache
        return {"checked_at": checked, "cache_ttl_seconds": int(_CACHE_TTL),
                "hosts": {h: hosts[h] for h in probes}}
    checked = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            results = await asyncio.gather(
                *(_probe(c, h, u, ej) for h, (u, ej) in probes.items()))
        hosts = dict(results)
    except Exception as e:  # belt and braces: this tool must never raise
        hosts = {h: f"unknown ({type(e).__name__})" for h in probes}
    _reachability_cache = (now, checked, hosts)
    return {"checked_at": checked, "cache_ttl_seconds": int(_CACHE_TTL),
            "hosts": dict(hosts)}


KALSHI_GEO_NOTE = {
    "eligibility_check": ("none — Kalshi publishes no geoblock endpoint; "
                          "eligibility is enforced server-side at signup and "
                          "order time. See Member Agreement §VI (Restricted "
                          "Jurisdictions)."),
    "source": KALSHI_NOTICES,
}

DISCLAIMER = ("Advisory. Not a compliance check and not legal advice. "
              "Eligibility depends on residence, citizenship and "
              "incorporation, not on this machine's IP address.")
