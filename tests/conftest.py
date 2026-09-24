"""The suite must not read the operator's own environment.

An operator with ODDSRAIL_PAPER_BANKROLL, a guardrail cap or KALSHI keys
exported would otherwise see a dozen unrelated failures, and CI would pass
while a developer's machine went red. Every oddsrail, Polymarket and Kalshi
variable is cleared for the duration of each test; a test that wants one
sets it itself.
"""

import os

import pytest

PREFIXES = ("ODDSRAIL_", "POLYMARKET_", "KALSHI_")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in [k for k in os.environ if k.startswith(PREFIXES)]:
        monkeypatch.delenv(key, raising=False)
    yield
