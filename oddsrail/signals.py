"""Signal tools: the premium layer of oddsrail.

overshoot: adapted from the polymarket-wc in-play overshoot analyzer —
detects endogenous price jumps in a recent price series and reports whether
the market is currently inside a fresh jump window (the fade-the-overreaction
setup) plus how much past jumps in this series reverted.

dispute_risk: heuristic scoring of how likely a market's resolution gets
messy (UMA disputes), from market metadata. Honest v0: a transparent
rules-based score with reasons, not a trained model.
"""

import bisect
import statistics


# --------------------------------------------------------------------------- #
# Overshoot                                                                    #
# --------------------------------------------------------------------------- #

def _price_at(times, prices, t):
    i = bisect.bisect_right(times, t) - 1
    return prices[i] if i >= 0 else None


def detect_jumps(times, prices, *, lookback=60.0, threshold=0.05,
                 settle=30.0, debounce=90.0, horizons=(30, 60, 120, 300)):
    """Find jumps >= threshold within lookback seconds; measure reversion.

    Returns a list of event dicts. Reversion fraction: +1.0 = fully retraced,
    0.0 = sticky, <0 = kept running (momentum).
    """
    events = []
    last_t = None
    n = len(times)
    for i in range(n):
        t, p = times[i], prices[i]
        ref = _price_at(times, prices, t - lookback)
        if ref is None or abs(p - ref) < threshold:
            continue
        if last_t is not None and (t - last_t) < debounce:
            continue

        direction = 1 if p > ref else -1
        ext_p, ext_t = p, t
        j = i
        while j < n and times[j] <= t + settle:
            better = prices[j] > ext_p if direction == 1 else prices[j] < ext_p
            if better:
                ext_p, ext_t = prices[j], times[j]
            j += 1

        jump = (ext_p - ref) * direction
        if jump <= 0:
            continue

        rev = {}
        for h in horizons:
            ph = _price_at(times, prices, ext_t + h)
            rev[h] = None if ph is None else round(((ext_p - ph) * direction) / jump, 3)

        events.append({
            "detected_at": t, "direction": "up" if direction == 1 else "down",
            "pre_jump_price": round(ref, 4), "extreme_price": round(ext_p, 4),
            "extreme_at": ext_t, "jump_size": round(jump, 4),
            "reversion_by_horizon_s": rev,
        })
        last_t = t
    return events


def overshoot_report(times, prices, *, lookback=60.0, threshold=0.05,
                     settle=30.0, debounce=90.0, fresh_window=300.0):
    """Live overshoot signal for one price series (oldest -> newest)."""
    if len(times) < 5:
        return {"ok": False, "error": "not enough price history points"}

    events = detect_jumps(times, prices, lookback=lookback, threshold=threshold,
                          settle=settle, debounce=debounce)
    now = times[-1]
    current = prices[-1]
    fresh = [e for e in events if now - e["extreme_at"] <= fresh_window]

    # historical tendency of this series: median reversion at 120s
    hist = [e["reversion_by_horizon_s"].get(120) for e in events
            if e["reversion_by_horizon_s"].get(120) is not None]
    tendency = round(statistics.median(hist), 3) if hist else None

    out = {
        "ok": True,
        "last_price": round(current, 4),
        "jumps_detected": len(events),
        "median_reversion_120s": tendency,
        "series_span_s": round(now - times[0], 1),
        "events": events[-5:],
        "fade_setup_active": False,
    }
    if fresh:
        e = fresh[-1]
        elapsed = now - e["extreme_at"]
        retraced = ((e["extreme_price"] - current)
                    / e["jump_size"] if e["direction"] == "up"
                    else (current - e["extreme_price"]) / e["jump_size"])
        out.update({
            "fade_setup_active": True,
            "active_jump": e,
            "seconds_since_extreme": round(elapsed, 1),
            "retraced_so_far": round(retraced, 3),
            "note": ("price jumped {} by {:.1%}; historically this series' jumps "
                     "retrace a median of {} of the move within 120s"
                     ).format(e["direction"], e["jump_size"],
                              f"{tendency:.0%}" if tendency is not None else "n/a"),
        })
    return out


# --------------------------------------------------------------------------- #
# Dispute risk                                                                 #
# --------------------------------------------------------------------------- #

# words that historically correlate with contested resolutions: subjective
# criteria, source ambiguity, or human-judgment resolution language
_AMBIGUOUS = (
    "consensus of", "credible report", "official announcement", "widely report",
    "public statement", "in the opinion", "substantially", "materially",
    "generally accepted", "confirmed by", "according to sources", "de facto",
    "attempt", "significant", "major", "formal", "informal",
)

_CLEAN = (
    "final score", "closing price", "settlement price", "official result",
    "as reported by the associated press", "coin market cap", "coingecko",
    "chainlink", "binance", "opening weekend", "box office",
)


def dispute_risk(market: dict) -> dict:
    """Score 0-100 how likely this market's resolution gets contested.

    Transparent heuristic v0 built from the failure patterns in 2026's UMA
    dispute wave (subjective wording, long-tail topics, deadline-edge risk,
    big open interest attracting oracle manipulation).
    """
    score = 0
    reasons = []

    desc = " ".join(str(market.get(k, "")) for k in
                    ("description", "question", "title")).lower()

    hits = [w for w in _AMBIGUOUS if w in desc]
    if hits:
        pts = min(35, 12 * len(hits))
        score += pts
        reasons.append(f"+{pts}: ambiguous resolution wording ({', '.join(hits[:4])})")

    clean_hits = [w for w in _CLEAN if w in desc]
    if clean_hits:
        score -= 15
        reasons.append(f"-15: objective settlement source named ({clean_hits[0]})")

    uma = str(market.get("umaResolutionStatus", "") or
              market.get("uma_resolution_status", "")).lower()
    if "dispute" in uma:
        score += 40
        reasons.append("+40: UMA status shows an active or past dispute")
    elif "proposed" in uma:
        score += 10
        reasons.append("+10: resolution proposed, challenge window open")

    if market.get("negRisk") or market.get("neg_risk"):
        score += 5
        reasons.append("+5: neg-risk (multi-outcome) market — more edge cases")

    try:
        vol = float(market.get("volume", 0) or 0)
        if vol > 5_000_000:
            score += 15
            reasons.append("+15: large open interest (>$5M) — worth manipulating")
        elif vol > 1_000_000:
            score += 8
            reasons.append("+8: meaningful open interest (>$1M)")
    except (TypeError, ValueError):
        pass

    # deadline-edge risk: events that can happen right at the boundary
    for w in ("by ", "before ", "deadline", "by the end of"):
        if w in desc:
            score += 8
            reasons.append("+8: deadline-conditioned question (boundary-case risk)")
            break

    score = max(0, min(100, score))
    band = ("low" if score < 25 else
            "moderate" if score < 50 else
            "elevated" if score < 75 else "high")
    return {
        "score": score,
        "band": band,
        "reasons": reasons,
        "disclaimer": ("heuristic v0 — transparent rules, not a model; "
                       "treat as a triage flag, not a probability"),
    }
