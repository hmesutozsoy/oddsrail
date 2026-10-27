"""Supported prediction price grids; each market supplies its active increment.

Source: https://docs.polymarket.com/market-data/market-details
This allowlist does not select a tick or enable another trading protocol.
"""

from decimal import Decimal


TICK_SIZES = frozenset(map(Decimal, ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001")))
