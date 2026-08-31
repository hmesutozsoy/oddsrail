"""Settlement-divergence audit for a cross-venue pair, on LIVE data.

Two markets can ask the same question in the same words and still pay out
differently — different resolution source, different close time, different
void conditions. That gap is where a "cross-venue arbitrage" quietly becomes
an unhedged directional bet. This module makes the gap explicit instead of
leaving an agent to infer it from a price difference.

Deliberate contrast with the frozen-lookup approach: nothing here is
pre-curated. Both sides are fetched live, so any pair can be audited — and
when a check cannot be performed, it says so rather than staying silent.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _parse(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def _hours_apart(a, b):
    da, db = _parse(a), _parse(b)
    if da is None or db is None:
        return None
    return round(abs((da - db).total_seconds()) / 3600.0, 2)


def audit_pair(pm: dict, kx: dict, notional_usd: float | None = None) -> dict:
    """Compare a Polymarket resolution_criteria dict against a Kalshi one.

    Returns findings plus a verdict of ok / caution / block. The verdict is
    advisory and errs toward caution: a missing field is treated as unknown
    risk, never as a pass.
    """
    findings = []
    blocking = 0
    cautions = 0

    # --- settlement timing -------------------------------------------------
    pm_end = pm.get("end_date")
    kx_end = kx.get("close_time")
    gap = _hours_apart(pm_end, kx_end)
    if gap is None:
        findings.append({"check": "settlement_timing", "severity": "caution",
                         "detail": "one venue did not publish a close time — "
                                   "timing divergence cannot be ruled out",
                         "polymarket": pm_end, "kalshi": kx_end})
        cautions += 1
    elif gap >= 24:
        findings.append({"check": "settlement_timing", "severity": "block",
                         "detail": f"close times are {gap}h apart — these "
                                   "resolve on different days and are not a hedge",
                         "polymarket": pm_end, "kalshi": kx_end})
        blocking += 1
    elif gap >= 1:
        findings.append({"check": "settlement_timing", "severity": "caution",
                         "detail": f"close times differ by {gap}h — the legs can "
                                   "settle against different underlying states",
                         "polymarket": pm_end, "kalshi": kx_end})
        cautions += 1
    else:
        findings.append({"check": "settlement_timing", "severity": "ok",
                         "detail": f"close times within {gap}h",
                         "polymarket": pm_end, "kalshi": kx_end})

    # --- resolution source -------------------------------------------------
    pm_src = (pm.get("resolution_source") or "").strip()
    kx_srcs = kx.get("settlement_sources") or []
    kx_names = ", ".join(
        str(s.get("name") or s.get("url") or s) for s in kx_srcs
    ) if isinstance(kx_srcs, list) else str(kx_srcs)
    if not pm_src or pm_src == "(none named)":
        findings.append({"check": "resolution_source", "severity": "caution",
                         "detail": "Polymarket names no explicit source — it "
                                   "resolves via UMA on the description text, "
                                   "which is where disputes originate",
                         "polymarket": pm_src or None, "kalshi": kx_names or None})
        cautions += 1
    elif kx_names and pm_src.lower() not in kx_names.lower():
        findings.append({"check": "resolution_source", "severity": "caution",
                         "detail": "the venues read the outcome from different "
                                   "sources; they can legitimately disagree",
                         "polymarket": pm_src, "kalshi": kx_names})
        cautions += 1
    else:
        findings.append({"check": "resolution_source", "severity": "ok",
                         "detail": "sources appear to agree",
                         "polymarket": pm_src, "kalshi": kx_names or None})

    # --- open dispute on the Polymarket leg --------------------------------
    uma = str(pm.get("uma_resolution_status") or "").lower()
    if "dispute" in uma:
        findings.append({"check": "uma_status", "severity": "block",
                         "detail": f"Polymarket leg is in UMA dispute ({uma}) — "
                                   "its payout is not yet determined"})
        blocking += 1
    elif "proposed" in uma:
        findings.append({"check": "uma_status", "severity": "caution",
                         "detail": f"resolution proposed ({uma}); the challenge "
                                   "window is still open"})
        cautions += 1

    # --- multi-outcome structure ------------------------------------------
    if pm.get("neg_risk"):
        findings.append({"check": "market_structure", "severity": "caution",
                         "detail": "Polymarket leg is neg-risk (multi-outcome); "
                                   "a binary Kalshi leg may not be its complement"})
        cautions += 1

    verdict = "block" if blocking else ("caution" if cautions else "ok")
    return {
        "verdict": verdict,
        "blocking_findings": blocking,
        "caution_findings": cautions,
        "findings": findings,
        "polymarket": {"question": pm.get("question"),
                       "resolution_source": pm_src or None,
                       "end_date": pm_end},
        "kalshi": {"title": kx.get("title"), "yes_means": kx.get("yes_means"),
                   "settlement_sources": kx_names or None,
                   "close_time": kx_end},
        "notional_usd": notional_usd,
        "how_to_read_this": {
            "ok": "no divergence detected in the checks performed — this is not "
                  "a guarantee; read both rule texts yourself",
            "caution": "at least one real difference found; size accordingly or "
                       "treat the legs as independent bets",
            "block": "the legs do not hedge each other; a price difference here "
                     "is not an edge",
        },
        "checks_not_performed": [
            "full natural-language diff of the two rule texts",
            "void/cancellation condition comparison (neither venue exposes "
            "these as structured fields)",
            "historical settlement agreement between the venues",
        ],
    }


def kelly_size(bankroll: float, price: float, fair_value: float,
               max_fraction: float = 0.25) -> dict:
    """Fractional-Kelly stake for a binary contract.

    Kelly for a binary at price p with true probability q is
    (q - p) / (1 - p) of bankroll. It is famously aggressive and assumes your
    edge estimate is correct, so this caps at `max_fraction` of full Kelly and
    refuses negative-edge bets outright rather than returning a short.
    """
    if not (0 < price < 1):
        return {"error": "price must be a probability in (0,1)"}
    if not (0 < fair_value < 1):
        return {"error": "fair_value must be a probability in (0,1)"}
    if bankroll <= 0:
        return {"error": "bankroll must be positive"}

    edge = fair_value - price
    if edge <= 0:
        return {"recommended_stake_usd": 0.0, "edge": round(edge, 4),
                "reason": "no positive edge at this price — do not buy",
                "note": "a negative edge on YES is not automatically a NO trade; "
                        "price the NO leg separately"}
    full_kelly = edge / (1 - price)
    frac = full_kelly * max_fraction
    stake = bankroll * frac
    return {
        "recommended_stake_usd": round(stake, 2),
        "shares_at_price": round(stake / price, 2),
        "edge": round(edge, 4),
        "full_kelly_fraction": round(full_kelly, 4),
        "applied_fraction": round(frac, 4),
        "max_fraction_of_kelly": max_fraction,
        "assumptions": [
            "fair_value is YOUR estimate — Kelly is only as good as it",
            "ignores fees and slippage: run quote_cost and subtract before "
            "trusting this size",
            "assumes this is the only position; correlated bets compound risk",
            f"capped at {max_fraction:.0%} of full Kelly, which is still "
            "aggressive for a single-name binary",
        ],
    }
