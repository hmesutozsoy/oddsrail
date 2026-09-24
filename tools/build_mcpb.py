#!/usr/bin/env python3
"""Build oddsrail.mcpb, the one-click installer for Claude Desktop.

Usage:
    python tools/build_mcpb.py            # writes dist/oddsrail-<version>.mcpb

Claude Desktop reads the bundle, supplies uv and a managed Python itself, and
asks the user for the settings declared in user_config. Nothing here needs
Python or a terminal on the user's machine.

The bundle pins the oddsrail version on PyPI, so build it only after that
version is published, or it will fail to install for everyone.

SAFETY: the server treats only "0", "false" and "no" in ODDSRAIL_DRY_RUN as
live. So the user-facing switch is "Paper trading", default ticked, mapped
straight onto ODDSRAIL_DRY_RUN. Ticked or unset or anything unexpected all
stay on paper; only unticking it sends real orders. Never expose a "live"
switch mapped onto that variable: its default of false would trade for real.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "mcpb"


def version() -> str:
    m = re.search(r'^VERSION = "([^"]+)"', (ROOT / "oddsrail" / "server.py").read_text(), re.M)
    return m.group(1)


def manifest(v: str) -> dict:
    return {
        "manifest_version": "0.4",
        "name": "oddsrail",
        "display_name": "OddsRail",
        "version": v,
        "description": "Polymarket tools for Claude, with local signing and operator-set spending limits.",
        "long_description": (
            "OddsRail gives Claude live Polymarket and Kalshi market data, costed fills and order "
            "routing, operator-set spending limits and a separate check_order tool to review a "
            "proposal before submission. Ask Claude to run that check before each order.\n\n"
            "It starts in paper trading, filled against the live order book, so nothing real is "
            "sent until you untick Paper trading. Your wallet key is optional, is stored in your "
            "operating system's keychain by Claude Desktop and used by the local server to sign. "
            "Live orders need an existing funded Polymarket account with trading approvals. "
            "This is a local tool connection, not an always-on runner for website strategies. "
            "Open source, MIT; OddsRail adds no trading fee. Venue fees may apply."),
        "author": {"name": "OddsRail", "url": "https://oddsrail.app"},
        "repository": {"type": "git", "url": "https://github.com/hmesutozsoy/oddsrail"},
        "homepage": "https://oddsrail.app",
        "documentation": "https://github.com/hmesutozsoy/oddsrail#readme",
        "support": "https://github.com/hmesutozsoy/oddsrail/issues",
        "icon": "icon.png",
        "server": {
            "type": "uv",
            "entry_point": "server/main.py",
            "mcp_config": {
                "command": "uv",
                "args": ["run", "--directory", "${__dirname}", "server/main.py"],
                "env": {
                    "POLYMARKET_PRIVATE_KEY": "${user_config.polymarket_private_key}",
                    "POLYMARKET_WALLET_ADDRESS": "${user_config.polymarket_wallet_address}",
                    "ODDSRAIL_DRY_RUN": "${user_config.paper_trading}",
                    "ODDSRAIL_MAX_ORDER_NOTIONAL": "${user_config.max_order_usd}",
                    "ODDSRAIL_MAX_SESSION_NOTIONAL": "${user_config.max_session_usd}",
                },
            },
        },
        "tools_generated": True,
        "prompts_generated": True,
        "keywords": ["polymarket", "kalshi", "prediction markets", "trading", "ai agents"],
        "license": "MIT",
        "privacy_policies": ["https://oddsrail.app/privacy"],
        "compatibility": {"platforms": ["darwin", "win32", "linux"], "runtimes": {"python": ">=3.11"}},
        "user_config": {
            "paper_trading": {
                "type": "boolean", "title": "Paper trading",
                "description": ("Ticked, every order is simulated against the live order book and "
                                "nothing real is sent. Untick only when you mean to trade real money."),
                "default": True, "required": False},
            "polymarket_private_key": {
                "type": "string", "title": "Polymarket wallet private key",
                "description": ("Optional. Use the signer key for your funded Polymarket account, only in these "
                                "settings. Claude Desktop stores it securely; the local server uses it to sign. "
                                "Never paste it in a chat. Leave empty to browse and simulate."),
                "sensitive": True, "required": False},
            "polymarket_wallet_address": {
                "type": "string", "title": "Polymarket wallet address",
                "description": ("Your existing Polymarket trading account address. This is a public address, "
                                "not a private key. Set up funding and trading approvals on Polymarket first."),
                "required": False},
            "max_order_usd": {
                "type": "number", "title": "Largest single order, in dollars",
                "description": "Any order above this is refused before it is sent. Checked in paper trading too.",
                "default": 25, "min": 1, "max": 100000, "required": False},
            "max_session_usd": {
                "type": "number", "title": "Most to spend per session, in dollars",
                "description": "Caps submitted order notional in this local server process. Restarting the "
                               "extension resets it. This is not a daily loss or account-wide cap.",
                "default": 100, "min": 1, "max": 1000000, "required": False},
        },
    }


def pyproject(v: str) -> str:
    return f'''[project]
name = "oddsrail-claude-desktop"
version = "{v}"
description = "Claude Desktop bundle for oddsrail; installs the published package."
requires-python = ">=3.11"
dependencies = ["oddsrail=={v}"]
'''


def icon(path: Path) -> None:
    # Pillow is only needed to regenerate the image, not to inspect or test
    # the install manifest in the locked runtime/CI environment.
    from PIL import Image, ImageDraw

    size = 512
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=112, fill=(24, 25, 27, 255))
    # the site's mark: three slanted bars, the middle one tallest, in the accent colour
    x0, base, unit = 150, 372, 8.2
    for i, h in enumerate((18, 28, 22)):
        bx, top = x0 + i * 9 * unit, base - h * unit
        d.polygon([(bx + 4 * unit, top), (bx + 10 * unit, top), (bx + 6 * unit, base), (bx, base)],
                  fill=(189, 167, 255, 255))
    im.save(path, optimize=True)


def main() -> None:
    v = version()
    (BUNDLE / "manifest.json").write_text(json.dumps(manifest(v), indent=2) + "\n")
    (BUNDLE / "pyproject.toml").write_text(pyproject(v))
    icon(BUNDLE / "icon.png")
    out = ROOT / "dist" / f"oddsrail-{v}.mcpb"
    out.parent.mkdir(exist_ok=True)
    subprocess.run(["npx", "--yes", "@anthropic-ai/mcpb@2.1.2", "validate", str(BUNDLE / "manifest.json")], check=True)
    subprocess.run(["npx", "--yes", "@anthropic-ai/mcpb@2.1.2", "pack", str(BUNDLE), str(out)], check=True)
    print(f"built {out.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
