# Publishing oddsrail

Two publishes, in this order. The first unlocks the second.

## What PyPI is, and why it matters here

PyPI (the **Py**thon **P**ackage **I**ndex, pypi.org) is the public library
that `pip install` downloads from. Publishing oddsrail there means anyone can
run `pip install oddsrail` instead of cloning the repo — which is what agent
developers expect, and it's free.

It also unblocks the **official MCP registry**, which is how agents and
clients discover MCP servers. That registry proves you own the package by
looking for a marker line in your PyPI page's README:

    <!-- mcp-name: io.github.hmesutozsoy/oddsrail -->

That marker is already at the top of `README.md`, and `server.json` (which the
registry reads) already matches it. So the order is: PyPI first, registry
second.

## 1. Publish to PyPI

**Account (once):** sign up at https://pypi.org/account/register/, then turn
on 2FA — PyPI requires it for publishing.

**Token (once):** go to https://pypi.org/manage/account/token/, create an API
token scoped to "Entire account" (you can narrow it to the `oddsrail` project
after the first upload). It looks like `pypi-AgEIcHlwaS5vcmc...`. Copy it now;
it is shown only once.

**Store it (once)** in `~/.pypirc` so you don't paste it every time:

```ini
[pypi]
  username = __token__
  password = pypi-AgEIcHlwaS5vcmcAAAA...your-token...
```

Then `chmod 600 ~/.pypirc`.

**Build and upload** (the `dist/` files are already built and checked):

```bash
python3 -m build
```

```bash
python3 -m twine upload dist/*
```

Optional dry run against the PyPI test instance first — separate account at
test.pypi.org:

```bash
python3 -m twine upload --repository testpypi dist/*
```

**Verify:**

```bash
pip install oddsrail && oddsrail --help
```

### Releasing a new version later

Bump `version` in `pyproject.toml` (PyPI refuses to overwrite an existing
version), then rebuild and re-upload:

```bash
rm -rf dist && python3 -m build && python3 -m twine upload dist/*
```

## 2. Publish to the official MCP registry

Install the publisher CLI:

```bash
brew install mcp-publisher
```

Then, from the repo root:

```bash
mcp-publisher login github
```

```bash
mcp-publisher publish
```

It reads `server.json`, checks that your GitHub login owns
`io.github.hmesutozsoy/*`, and checks that the PyPI package's README carries
the matching `mcp-name:` marker. Confirm it landed:

```bash
curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=oddsrail"
```

## 3. Glama (nothing to do)

Glama crawls GitHub automatically. `glama.json` in the repo root claims
maintainership under the `hmesutozsoy` account when it does.

## 4. Smithery (later, needs hosting)

Smithery lists servers reachable over public HTTPS with the streamable-HTTP
transport, so it needs oddsrail hosted rather than run locally over stdio.
Two notes when that time comes: their scanner wants at least one tool callable
without credentials (the read tools qualify), and it expects `401` rather than
`403` for unauthenticated requests. Keep the **Kalshi** tools out of any hosted
multi-tenant deployment — Kalshi's Developer Agreement permits a member to use
the API for their own trading, not to facilitate others' (see README).
