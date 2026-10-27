"""Tool metadata a client and a directory reviewer read before any call:
titles, behaviour hints, and descriptions that say exactly what a tool does.

Annotations are promises. A tool marked read-only that writes a ledger, or a
destructive hint on pure arithmetic, makes a client ask for the wrong
confirmations, so the hints that were once wrong are pinned here."""

import ast
import json
import os
import pathlib
import subprocess
import sys

import httpx
import pytest

from oddsrail import check as ck
from oddsrail import geo
from oddsrail import server as S

DISCLAIMER_BITS = ("not financial advice", "the user decides every order",
                   "not affiliated with or endorsed by Polymarket or Kalshi")


async def _tools() -> dict:
    return {t.name: t for t in await S.srv.list_tools()}


async def test_every_tool_has_a_unique_title_and_explicit_safety_hints():
    tools = await _tools()
    assert len(tools) == 42
    titles = [t.title for t in tools.values()]
    assert all(isinstance(x, str) and x.strip() for x in titles), [
        n for n, t in tools.items() if not (t.title or "").strip()]
    assert len(set(titles)) == len(titles), "titles must be unique"
    for name, t in tools.items():
        # What goes over the wire: both hints present, not left to defaults.
        wire = t.model_dump(by_alias=True, exclude_none=True)
        hints = wire.get("annotations") or {}
        assert "readOnlyHint" in hints and "destructiveHint" in hints, name
        assert wire["title"] == t.title


async def test_corrected_annotations():
    tools = await _tools()
    size = tools["position_size"].annotations
    assert (size.read_only_hint, size.destructive_hint,
            size.idempotent_hint, size.open_world_hint) == (True, False, True, False)
    reset = tools["paper_reset"].annotations
    assert (reset.read_only_hint, reset.destructive_hint,
            reset.idempotent_hint, reset.open_world_hint) == (False, True, True, False)
    # paper_positions fills crossed resting paper orders and saves the ledger.
    pos = tools["paper_positions"].annotations
    assert (pos.read_only_hint, pos.destructive_hint,
            pos.idempotent_hint, pos.open_world_hint) == (False, False, False, True)
    # the cloud app reuses these constants for its own registrations
    assert reset == S.LEDGER_RESET and pos == S.LEDGER_SETTLE and size == S.LOCAL_READ


async def test_descriptions_make_no_premium_or_shouted_claims():
    for name, t in (await _tools()).items():
        d = t.description or ""
        assert "premium" not in d.lower(), name
        for label in ("CHECK BEFORE YOU PLACE", "READ BEFORE TRUSTING", "KILL SWITCH"):
            assert label not in d, (name, label)
        assert "\u2014" not in d and "\u2014" not in (t.title or ""), name  # published prose: no em dashes


async def test_builder_code_wording_matches_the_default():
    place = (await _tools())["place_order"].description
    assert "0 bps" in place and "ODDSRAIL_BUILDER_CODE" in place
    assert "OddsRail's own" in S.srv.instructions


async def test_check_order_asks_for_a_paraphrase_not_the_conversation():
    co = (await _tools())["check_order"]
    intent = co.input_schema["properties"]["intent"]["description"].lower()
    assert "paraphrase" in intent and "no other conversation content" in intent
    for d in (intent, co.description.lower()):
        assert "verbatim" not in d and "own words" not in d
    assert "paraphrase" in co.description.lower()
    # parameter name and default unchanged
    assert co.input_schema["properties"]["intent"]["default"] == ""
    assert "intent" not in co.input_schema.get("required", [])


def test_the_suggested_paraphrase_still_passes_the_intent_checks():
    example = S.INTENT_DESC.split("e.g. '")[1].split("'")[0]
    title = "Will Spain win the 2026 FIFA World Cup?"
    assert ck.intent_overlap(example, title) >= 0.5
    assert ck.intent_outcome(example) == "yes"
    # Size and price have their own parameters; asking for them in the intent
    # would add tokens no market title contains and lower the overlap.
    assert "size and price go in their own parameters" in S.INTENT_DESC.lower()
    assert "price" not in S.INTENT_DESC.lower().split("size and price")[0]


async def test_find_fade_setup_prompt_has_no_em_dashes_and_asks_for_a_paraphrase():
    for text in (S.find_fade_setup("fed"), S.hosted_find_fade_setup("fed")):
        assert "\u2014" not in text
        assert "paraphrase" in text and "operator's words" not in text


async def test_prompt_texts_have_no_em_dashes():
    texts = {"find_fade_setup": S.find_fade_setup("fed"),
             "hosted find_fade_setup": S.hosted_find_fade_setup("fed"),
             "check_cross_venue_edge": S.check_cross_venue_edge("fed"),
             "settle_resolved": S.settle_resolved(),
             "daily_review": S.daily_review(),
             "--help": S.USAGE}
    for p in await S.srv.list_prompts():  # every registered prompt is covered
        assert p.name in texts, p.name
    for name, text in texts.items():
        assert "\u2014" not in text, name


def test_no_runtime_string_in_server_py_has_an_em_dash():
    """Hints, notes, descriptions and help text reach an agent or a person;
    comments and docstrings do not, and may keep their dashes."""
    tree = ast.parse(pathlib.Path(S.__file__).read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    dashed = [node.lineno for node in ast.walk(tree)
              if isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings and "\u2014" in node.value]
    assert not dashed, f"em dash in server.py strings at lines {dashed}"


def test_instructions_carry_the_disclosure():
    for text in (S.srv.instructions, S.HOSTED_INSTRUCTIONS):
        for bit in DISCLAIMER_BITS:
            assert bit in text, bit


async def test_server_info_lists_signal_tools(monkeypatch):
    async def preflight(*a, **k):
        return {"blocked": None}

    async def reachability(*a, **k):
        return {}

    monkeypatch.setattr(geo, "preflight", preflight)
    monkeypatch.setattr(geo, "reachability", reachability)
    info = json.loads(await S.server_info())
    assert info["signal_tools"] == ["overshoot_signal", "dispute_risk"]
    assert "premium_tools" not in info


# The hosted profile is chosen at import and cannot be undone, so it gets its
# own interpreter. Nothing is served and nothing touches the network: the
# tools and prompts are listed, and the few calls made answer offline.
HOSTED_PROBE = """
import asyncio, json
import httpx
from oddsrail import geo, paper, trading
from oddsrail import server as S


async def offline(*a, **k):
    return {}

geo.preflight = offline
geo.reachability = offline


async def call(name, **args):
    res = await S.srv.call_tool(name, args)
    return json.loads(res.content[0].text)


async def main():
    tools = await S.srv.list_tools()
    prompts = []
    for p in await S.srv.list_prompts():
        got = await S.srv.get_prompt(p.name, {"query": "fed"})
        prompts.append({"name": p.name, "description": p.description,
                        "text": got.messages[0].content.text})
    async def fake_place(*a, **k):  # the intent a real order would carry
        return {"dry_run": True, "would_post": {"builder_code": "0xabc",
                "attribution": "on-chain: builder code signed into the order"}}
    trading.place_order = fake_place
    placed = await call("place_order", token_id="1", side="BUY", price=0.5, size=5)
    path = paper.ledger_path()
    before = path.read_bytes() if path.exists() else None
    unknown = await call("cancel_order", order_id="paper-nope")
    unknown_wrote = (path.read_bytes() if path.exists() else None) != before
    d = paper.load()
    d["open_orders"].append({"id": "paper-seeded", "time": 0, "token_id": "1", "title": None,
                             "side": "BUY", "price": 0.4, "size": 5})
    paper.save(d)
    seeded = await call("cancel_order", order_id="paper-seeded")
    return {"instructions": S.srv.instructions,
            "tools": [t.model_dump(by_alias=True, exclude_none=True) for t in tools],
            "prompts": prompts,
            "server_info": await call("server_info"),
            "cancel_unknown": unknown, "cancel_seeded": seeded, "placed": placed,
            "unknown_wrote": unknown_wrote,
            "resting_after": paper.load()["open_orders"],
            "unreachable": S._err_obj(httpx.ConnectError("boom"), host="the Polymarket CLOB")}

print(json.dumps(asyncio.run(main())))
"""


@pytest.fixture(scope="module")
def hosted_profile(tmp_path_factory):
    pytest.importorskip("oddsrail.cloud.auth")
    tmp = tmp_path_factory.mktemp("hosted")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ODDSRAIL_", "POLYMARKET_", "KALSHI_"))}
    env.update({"ODDSRAIL_HOSTED": "1", "ODDSRAIL_CLOUD_URL": "http://127.0.0.1:9",
                "ODDSRAIL_CLOUD_DATA": str(tmp),
                "ODDSRAIL_PAPER_LEDGER": str(tmp / "paper.json")})
    out = subprocess.run([sys.executable, "-c", HOSTED_PROBE], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    probe = json.loads(out.stdout)
    probe["tools"] = {t["name"]: t for t in probe["tools"]}
    return probe


def test_hosted_profile_keeps_titles_and_disclosure(hosted_profile):
    tools = hosted_profile["tools"]
    assert tools["place_order"]["title"] == "Place a paper Polymarket order"
    # Hosted cancel_order only edits the caller's paper ledger.
    cancel = tools["cancel_order"]
    assert cancel["title"] == "Cancel a paper order" and "PAPER" in cancel["description"]
    assert cancel["annotations"]["openWorldHint"] is False
    assert cancel["annotations"]["destructiveHint"] is True
    for name in ("place_order", "cancel_order"):
        assert "open_orders" not in tools[name]["description"], name
    titles = [t.get("title") for t in tools.values()]
    assert all(titles) and len(set(titles)) == len(titles)
    for name, t in tools.items():
        assert {"readOnlyHint", "destructiveHint"} <= set(t["annotations"]), name
        assert "premium" not in t["description"].lower(), name
    for bit in DISCLAIMER_BITS:
        assert bit in hosted_profile["instructions"], bit


# Hosted tools whose local wording names Kalshi, cross-venue work or an
# environment variable, re-registered with words that fit the connector.
REWORDED = ("find_markets", "closing_soon", "resolution_criteria", "quote_cost",
            "paper_positions", "paper_reset", "server_info")


def test_hosted_descriptions_promise_only_what_the_connector_does(hosted_profile):
    for name, t in hosted_profile["tools"].items():
        d = t["description"]
        low = d.lower()
        if "kalshi" in low:  # only ever to say it is not here
            assert "kalshi is not served" in low, name
        for gone in ("cross-venue", "both venues", "either venue", "dry-run", "dry_run",
                     "oddsrail_"):
            assert gone not in low, (name, gone)
        assert "—" not in d, name


async def test_hosted_rewordings_keep_the_local_functions_and_hints(hosted_profile):
    local = {n: t.model_dump(by_alias=True, exclude_none=True)
             for n, t in (await _tools()).items()}
    tools = hosted_profile["tools"]
    for name in REWORDED:
        assert tools[name]["description"] != local[name]["description"], name
        assert tools[name]["annotations"] == local[name]["annotations"], name
        assert tools[name]["inputSchema"] == local[name]["inputSchema"], name
        if name != "find_markets":  # "across venues" is not true on the connector
            assert tools[name]["title"] == local[name]["title"], name
    assert tools["find_markets"]["title"] == "Find Polymarket markets"


def test_hosted_fade_prompt_is_paper_only(hosted_profile):
    prompts = {p["name"]: p for p in hosted_profile["prompts"]}
    assert set(prompts) == {"find_fade_setup"}
    text = prompts["find_fade_setup"]["text"]
    assert text == S.hosted_find_fade_setup(query="fed")
    assert "ODDSRAIL_DRY_RUN" not in text and "dry-run" not in text.lower()
    assert "paper_positions" in text and "paper" in prompts["find_fade_setup"]["description"]
    assert "Kalshi" not in text and "—" not in text
    # the local prompt, which the skills are rendered from, is unchanged
    assert "ODDSRAIL_DRY_RUN=0" in S.find_fade_setup("fed")


def test_hosted_server_info_says_attribution_is_paper_only(hosted_profile):
    info = hosted_profile["server_info"]
    assert info["builder_code_configured"] is False
    for text in (info["attribution"], info["builder_code_source"],
                 info["venues"]["polymarket"]["attribution"]):
        assert "paper orders are never sent" in text, text
        assert "ODDSRAIL_BUILDER_CODE" not in text and "on-chain" not in text, text
        assert "—" not in text, text
    # Operator-profile details are not shown on the connector.
    assert "kalshi" not in info["geography"]
    assert "ledger" not in info["paper_trading"]
    assert "ODDSRAIL_" not in json.dumps(info["guardrails"])


def test_hosted_place_order_output_claims_no_attribution(hosted_profile):
    posted = hosted_profile["placed"]["would_post"]
    assert "builder_code" not in posted
    assert "paper orders are never sent" in posted["attribution"]
    assert "on-chain" not in posted["attribution"]


def test_hosted_cancel_answers_from_the_paper_ledger(hosted_profile):
    unknown = hosted_profile["cancel_unknown"]
    assert unknown["cancelled"] is False and unknown["sent"] is False
    assert "would_cancel" not in unknown and "paper_positions" in unknown["note"]
    assert hosted_profile["unknown_wrote"] is False, "an unknown id writes nothing"
    seeded = hosted_profile["cancel_seeded"]
    assert seeded["cancelled"] is True and seeded["order_id"] == "paper-seeded"
    assert hosted_profile["resting_after"] == []


def test_hosted_unreachable_hint_does_not_name_hidden_tools(hosted_profile):
    out = hosted_profile["unreachable"]
    assert out["failure_class"] == "unreachable"
    assert "open_orders" not in out["hint"] and "the Polymarket CLOB" in out["hint"]
    assert "—" not in out["hint"]
    # locally the same failure still points at open_orders, which exists there
    local = S._err_obj(httpx.ConnectError("boom"), host="the Polymarket CLOB")
    assert local["failure_class"] == "unreachable" and "open_orders" in local["hint"]
