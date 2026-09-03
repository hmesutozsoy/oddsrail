"""Generate skills/*/SKILL.md from the MCP prompts registered in server.py.

The prompts are the source of truth for the workflows (the ORDER of
operations that keeps an agent out of trouble). Skills are the same text in
the shape Claude Code plugins and skills.sh expect, so the two can never
drift: tests/test_skills_sync.py regenerates into a temp dir and diffs.

    python scripts/gen_skills.py            # writes skills/
    python scripts/gen_skills.py --check    # exit 1 if skills/ is stale
"""

import pathlib
import sys

from oddsrail import server

ROOT = pathlib.Path(__file__).resolve().parents[1]

# (directory name, prompt function, kwargs used to render the template,
#  one-line description shown in skill listings)
SKILLS = [
    ("find-fade-setup", server.find_fade_setup,
     {"query": "$ARGUMENTS", "bankroll_usd": "<bankroll_usd>"},
     "Find and evaluate a fade (mean-reversion) setup on prediction markets with oddsrail: "
     "candidates, overshoot signal, book, walked cost, resolution criteria, Kelly size, dry-run order."),
    ("check-cross-venue-edge", server.check_cross_venue_edge,
     {"query": "$ARGUMENTS"},
     "Decide whether a Polymarket vs Kalshi price difference is a real edge or a settlement "
     "mismatch: compare_venues, settlement_audit, quote_cost on both legs, resolution criteria."),
    ("settle-resolved", server.settle_resolved, {},
     "Turn resolved and hedged Polymarket positions back into USDC, gasless, through the "
     "operator's relayer key, with a dry-run read-back first."),
    ("daily-review", server.daily_review, {},
     "Daily review of open prediction-market exposure with oddsrail: positions, resting "
     "orders, fills, markets closing soon, builder attribution."),
]

PREAMBLE = ("Use the tools of the `oddsrail` MCP server (install: `pip install oddsrail`, "
            "then `claude mcp add --transport stdio oddsrail -- oddsrail`, or enable the "
            "oddsrail plugin). Prices are implied probabilities in (0,1). Trading tools are "
            "dry-run unless the operator has set ODDSRAIL_DRY_RUN=0; never assume an order "
            "was placed without checking the `dry_run` field of the result.\n\n")


def render(name, fn, kwargs, description) -> str:
    import json
    body = fn(**kwargs)
    # YAML: an unquoted scalar containing ": " is a parse error, so the
    # description is emitted as a JSON string, which is valid YAML.
    return (f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\n\n"
            f"# {name.replace('-', ' ')}\n\n{PREAMBLE}{body.strip()}\n")


def main(check: bool = False) -> int:
    stale = []
    for name, fn, kwargs, desc in SKILLS:
        path = ROOT / "skills" / name / "SKILL.md"
        text = render(name, fn, kwargs, desc)
        if check:
            if not path.exists() or path.read_text() != text:
                stale.append(str(path.relative_to(ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print("wrote", path.relative_to(ROOT))
    if check and stale:
        print("stale:", *stale)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(check="--check" in sys.argv))
