# Releasing

Maintainer notes. Not needed to *use* oddsrail.

## Order of operations

1. Bump the version in **both** `pyproject.toml` and `server.json`
   (`version` and `packages[].version`) — the registry rejects a publish whose
   version is not already on PyPI.
2. `rm -rf dist build *.egg-info && python3 -m build && python3 -m twine check dist/*`
3. `python3 -m twine upload dist/*`
4. Wait a minute — PyPI's simple index (what pip reads) lags its JSON API.
5. `mcp-publisher login dns --domain oddsrail.app --private-key <hex>`
   then `mcp-publisher publish`

## Gotchas paid for in blood

- `server.json.description` must be **<= 100 chars** (422 otherwise).
- DNS auth and GitHub auth overwrite each other's stored token and have no
  permission over each other's namespaces. Registry JWTs expire in ~a day.
- The `mcp-name:` marker in README.md must exactly match `server.json.name`;
  changing the name requires a new PyPI release carrying the new marker.
- `mcp-publisher status` needs flags **before** the positional server name,
  and prompts for confirmation.
