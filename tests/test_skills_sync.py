"""skills/*/SKILL.md must be exactly what scripts/gen_skills.py renders from
the MCP prompts, and the plugin manifests must agree with the package."""

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _gen():
    spec = importlib.util.spec_from_file_location("gen_skills", ROOT / "scripts" / "gen_skills.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_skills_match_prompts():
    gen = _gen()
    for name, fn, kwargs, desc in gen.SKILLS:
        path = ROOT / "skills" / name / "SKILL.md"
        assert path.exists(), f"missing {path}; run scripts/gen_skills.py"
        assert path.read_text() == gen.render(name, fn, kwargs, desc), f"stale {path}"


def test_every_skill_has_frontmatter_and_names_tools():
    for path in (ROOT / "skills").glob("*/SKILL.md"):
        text = path.read_text()
        assert text.startswith("---\nname: " + path.parent.name + "\n")
        assert "description:" in text.split("---")[1]
        assert "oddsrail" in text


def test_plugin_manifests_agree_with_package():
    from oddsrail.server import VERSION
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    mcp = json.loads((ROOT / ".mcp.json").read_text())
    assert plugin["name"] == "oddsrail" and plugin["version"] == VERSION
    assert market["plugins"][0]["name"] == "oddsrail" and market["plugins"][0]["version"] == VERSION
    assert "oddsrail" in mcp["mcpServers"] and mcp["mcpServers"]["oddsrail"]["args"] == ["oddsrail"]
