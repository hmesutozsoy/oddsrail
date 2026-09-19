"""Read canonical, deployed Polymarket accounts for the signed-in owner.

Discovery uses CREATE2 derivation and current Polygon contract state, not Gamma
profile associations. There is no key, account creation, allowance, signing or
transaction path. All chain reads use one recent block on one fixed HTTPS RPC.

Derivation: https://github.com/Polymarket/py-sdk/blob/main/src/polymarket/_internal/wallet.py
Safe ownership: https://docs.safe.global/reference-smart-account/owners/getOwners
Proxy control: Polymarket/proxy-factories, ProxyWallet{,Lib,Factory}.sol. The
proxy's stored owner is its factory; the factory derives its caller's clone.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable

import httpx
from starlette.routing import Route

from .portfolio import normalize_address
from .wallet_auth import WalletAuth, WalletRequestError, _invalid_constant, _unique_object

RPC_URL = "https://polygon.drpc.org"
CHAIN_ID = 137
REQUEST_SECONDS = 6
LOOKUP_SECONDS = 18
MAX_RESPONSE_BYTES = 600000
MAX_CODE_BYTES = 32768
MAX_BLOCK_AGE = 180
_HEX = re.compile(r"0x(?:[0-9a-fA-F]{2})*\Z")
_QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,15})\Z")
_WORD = re.compile(r"0x[0-9a-fA-F]{64}\Z")
OWNER_SELECTOR = "0x8da5cb5b"
SAFE_OWNERS_SELECTOR = "0xa0e67e2b"
SAFE_THRESHOLD_SELECTOR = "0xe75235b8"
PROXY_IMPLEMENTATION_SELECTOR = "0xaaf10f42"
PROXY_OWNER_SLOT = "0x734a2a5caf82146a5ddd5263d9af379f9f72724959f0567ddc9df2c40cf2cc20"

# Fail closed if an installed SDK changes the contracts whose ownership paths
# are reviewed here. The SDK performs only local address derivation.
DERIVATION = {
    "proxy_factory": "0xab45c5a4b0c941a2f231c04c3f49182e1a254052",
    "proxy_implementation": "0x44e999d5c2f66ef0861317f9a4805ac2e90aeb4f",
    "safe_factory": "0xaacfeea03eb1561c4e67d661e40682bd20e3541b",
    "safe_init_code_hash": "0x2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf",
    "deposit_wallet_factory": "0x00000000000fb5c9adea0298d729a0cb3823cc07",
    "deposit_wallet_implementation": "0x58ca52ebe0dadfdf531cde7062e76746de4db1eb",
    "deposit_wallet_beacon": "0x7a18edfe055488a3128f01f563e5b479d92ffc3a",
}


class AccountLookupError(Exception):
    pass


def candidates(owner: str) -> list[dict]:
    from polymarket._internal.environment import PRODUCTION_CONFIG
    from polymarket._internal.wallet import (derive_beacon_deposit_wallet_address,
                                            derive_uups_deposit_wallet_address,
                                            derive_safe_wallet_address,
                                            derive_proxy_wallet_address)
    owner = normalize_address(owner)
    config = PRODUCTION_CONFIG.wallet_derivation
    if PRODUCTION_CONFIG.chain_id != CHAIN_ID or any(
            str(getattr(config, key, "")).lower() != value for key, value in DERIVATION.items()):
        raise AccountLookupError("Unsupported SDK wallet configuration")
    return [{"address": normalize_address(derive(owner, config)), "wallet_type": kind}
            for kind, derive in (("DEPOSIT_WALLET", derive_beacon_deposit_wallet_address),
                                 ("DEPOSIT_WALLET", derive_uups_deposit_wallet_address),
                                 ("GNOSIS_SAFE", derive_safe_wallet_address),
                                 ("POLY_PROXY", derive_proxy_wallet_address))]


def _quantity(value) -> int:
    if not isinstance(value, str) or not _QUANTITY.fullmatch(value):
        raise AccountLookupError("Invalid chain quantity")
    return int(value, 16)


def _address_word(value) -> str:
    if not isinstance(value, str) or not _WORD.fullmatch(value) or value[2:26] != "0" * 24:
        raise AccountLookupError("Invalid address return data")
    return "0x" + value[-40:].lower()


def _owners(value) -> list[str]:
    if not isinstance(value, str) or not _HEX.fullmatch(value) or not 130 <= len(value) <= 2178:
        raise AccountLookupError("Invalid Safe owners")
    words = [value[i:i + 64] for i in range(2, len(value), 64)]
    if int(words[0], 16) != 32 or int(words[1], 16) > 32 or len(value) != 130 + 64 * int(words[1], 16):
        raise AccountLookupError("Invalid Safe owner array")
    return [_address_word("0x" + word) for word in words[2:]]


async def _batch(client: httpx.AsyncClient, calls: list[tuple[str, list]]) -> list:
    payload = [{"jsonrpc": "2.0", "id": i + 1, "method": method, "params": params}
               for i, (method, params) in enumerate(calls)]
    async with client.stream("POST", RPC_URL, json=payload) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                raise AccountLookupError("RPC response is too large")
            data.extend(chunk)
    try:
        rows = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AccountLookupError("Invalid RPC JSON") from exc
    if not isinstance(rows, list) or len(rows) != len(calls):
        raise AccountLookupError("Incomplete RPC batch")
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or row.get("jsonrpc") != "2.0" or
                type(row.get("id")) is not int or not 1 <= row["id"] <= len(calls) or
                row["id"] in result or "error" in row or "result" not in row):
            raise AccountLookupError("Failed or mismatched RPC response")
        result[row["id"]] = row["result"]
    return [result[i + 1] for i in range(len(calls))]


def _deployed(code) -> bool:
    if not isinstance(code, str) or not _HEX.fullmatch(code) or len(code) > 2 + MAX_CODE_BYTES * 2:
        raise AccountLookupError("Invalid deployed code")
    return code != "0x"


async def load_accounts(owner: str, *, transport: httpx.AsyncBaseTransport | None = None,
                        clock: Callable[[], float] = time.time) -> dict:
    """Return a complete supported-account list, or raise; no partial answers."""
    owner = normalize_address(owner)
    possible = candidates(owner)
    async with asyncio.timeout(LOOKUP_SECONDS):
        async with httpx.AsyncClient(timeout=REQUEST_SECONDS, trust_env=False,
                                     follow_redirects=False, transport=transport) as client:
            chain, block = await _batch(client, [("eth_chainId", []), ("eth_getBlockByNumber", ["latest", False])])
            if _quantity(chain) != CHAIN_ID or not isinstance(block, dict):
                raise AccountLookupError("Wrong chain or missing block")
            number = block.get("number")
            if _quantity(number) <= 0 or not -30 <= clock() - _quantity(block.get("timestamp")) <= MAX_BLOCK_AGE:
                raise AccountLookupError("Block is stale or invalid")
            factories = [DERIVATION[key] for key in ("deposit_wallet_factory", "safe_factory", "proxy_factory")]
            codes = await _batch(client, [("eth_getCode", [row["address"], number]) for row in possible] +
                                 [("eth_getCode", [address, number]) for address in factories])
            if not all(_deployed(code) for code in codes[4:]):
                raise AccountLookupError("Canonical wallet factory is not deployed")
            deployed = [(row, code) for row, code in zip(possible, codes[:4]) if _deployed(code)]
            calls, checks = [], []
            for row, code in deployed:
                address, kind = row["address"], row["wallet_type"]
                if kind == "GNOSIS_SAFE":
                    checks.append((row, "safe", len(calls)))
                    calls.extend([("eth_call", [{"to": address, "data": selector}, number])
                                  for selector in (SAFE_OWNERS_SELECTOR, SAFE_THRESHOLD_SELECTOR)])
                elif kind == "POLY_PROXY":
                    expected = "0x363d3d373d3d3d363d73" + DERIVATION["proxy_implementation"][2:] + "5af43d82803e903d91602b57fd5bf3"
                    if code.lower() != expected:
                        raise AccountLookupError("Unsupported proxy implementation")
                    checks.append((row, "proxy", len(calls)))
                    calls.extend([("eth_getStorageAt", [address, PROXY_OWNER_SLOT, number]),
                                  ("eth_call", [{"to": DERIVATION["proxy_factory"],
                                                  "data": PROXY_IMPLEMENTATION_SELECTOR}, number])])
                else:
                    checks.append((row, "deposit", len(calls)))
                    calls.append(("eth_call", [{"to": address, "data": OWNER_SELECTOR}, number]))
            values = await _batch(client, calls) if calls else []
    accounts = []
    for row, kind, offset in checks:
        if kind == "safe":
            owners = _owners(values[offset])
            threshold = values[offset + 1]
            if not isinstance(threshold, str) or not _WORD.fullmatch(threshold):
                raise AccountLookupError("Invalid Safe threshold")
            controlled = owners == [owner] and int(threshold, 16) == 1
        elif kind == "proxy":
            controlled = (_address_word(values[offset]) == DERIVATION["proxy_factory"] and
                          _address_word(values[offset + 1]) == DERIVATION["proxy_implementation"])
        else:
            controlled = _address_word(values[offset]) == owner
        if controlled:
            accounts.append({**row, "deployed": True})
    return {"ok": True, "address": owner, "chain_id": CHAIN_ID,
            "accounts": accounts, "complete": True}


class TradingAccounts:
    def __init__(self, lookup: Callable[[str], Awaitable[dict]]):
        self._lookup = lookup

    def route(self, auth: WalletAuth) -> Route:
        return Route("/trading/accounts", auth.authenticated_post(self.check, keys=set(), max_body_bytes=1024),
                     methods=["POST", "OPTIONS"])

    async def check(self, identity: dict, body: dict) -> dict:
        owner = normalize_address(identity["address"])
        try:
            async with asyncio.timeout(LOOKUP_SECONDS + 2):
                result = await self._lookup(owner)
            if (not isinstance(result, dict) or result.get("ok") is not True or
                    normalize_address(result.get("address")) != owner or result.get("complete") is not True or
                    type(result.get("chain_id")) is not int or result["chain_id"] != CHAIN_ID or
                    not isinstance(result.get("accounts"), list) or len(result["accounts"]) > 4):
                raise AccountLookupError("Unverified account result")
            canonical = {row["address"]: row["wallet_type"] for row in candidates(owner)}
            seen = set()
            for row in result["accounts"]:
                if (not isinstance(row, dict) or row.get("deployed") is not True or
                        row.get("address") not in canonical or
                        canonical.get(row.get("address")) != row.get("wallet_type") or row.get("address") in seen):
                    raise AccountLookupError("Unverified account identity")
                seen.add(row["address"])
            return {**result, "address": identity["address"]}
        except Exception:
            raise WalletRequestError("trading_accounts_unavailable", 503,
                                     detail="Trading account ownership could not be verified. Try again shortly.") from None
