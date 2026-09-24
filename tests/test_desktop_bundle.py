"""The Claude Desktop bundle must start on paper, whatever the host sends.

The installer passes each setting to the server as text. The server treats
only "0", "false" and "no" in ODDSRAIL_DRY_RUN as live, so the switch the user
sees has to be "Paper trading", default ticked, mapped straight onto that
variable. The tempting inversion, a "Live trading" switch defaulting to
false, would send its default "false" and trade real money on first launch.
These tests make that edit fail loudly.
"""

import importlib.util
import json
import re
from pathlib import Path

from oddsrail import trading

ROOT = Path(__file__).resolve().parent.parent


def _build():
    spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "tools" / "build_mcpb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _source(manifest, env_var):
    template = manifest["server"]["mcp_config"]["env"][env_var]
    found = re.fullmatch(r"\$\{user_config\.(\w+)\}", template)
    assert found, f"{env_var} must come straight from one user setting, got {template!r}"
    return manifest["user_config"][found.group(1)]


def test_paper_trading_is_the_default_and_reads_as_paper():
    m = _build().manifest("0.0.0")
    option = _source(m, "ODDSRAIL_DRY_RUN")
    assert option["type"] == "boolean"
    assert option["default"] is True, "the bundle must install on paper"
    # the host substitutes the default as text; that text must not read as live
    assert str(option["default"]).lower() not in trading._LIVE_VALUES
    assert "paper" in option["title"].lower(), "the switch the user sees must say paper, not live"


def test_every_way_the_switch_can_arrive_is_read_correctly(monkeypatch):
    for sent, live in (("true", False), ("True", False), ("", False), ("1", False),
                       ("false", True), ("0", True)):
        monkeypatch.setenv("ODDSRAIL_DRY_RUN", sent)
        assert trading.dry_run() is (not live), f"ODDSRAIL_DRY_RUN={sent!r}"


def test_spending_caps_are_on_by_default():
    m = _build().manifest("0.0.0")
    for env_var in ("ODDSRAIL_MAX_ORDER_NOTIONAL", "ODDSRAIL_MAX_SESSION_NOTIONAL"):
        option = _source(m, env_var)
        assert option["type"] == "number" and option["default"] > 0, env_var


def test_the_wallet_key_is_secret_and_optional():
    option = _source(_build().manifest("0.0.0"), "POLYMARKET_PRIVATE_KEY")
    assert option["sensitive"] is True, "the key must go to the system keychain"
    assert option.get("required") is False, "browsing and paper trading need no key"


def test_the_committed_bundle_matches_the_builder():
    m = _build()
    committed = json.loads((ROOT / "mcpb" / "manifest.json").read_text())
    assert committed == m.manifest(committed["version"]), "run python tools/build_mcpb.py"
    assert f'"oddsrail=={committed["version"]}"' in (ROOT / "mcpb" / "pyproject.toml").read_text()
