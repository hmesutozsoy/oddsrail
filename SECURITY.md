# Security policy

## Reporting a vulnerability

Please report security problems privately. Do not open a public issue,
discussion or pull request for them.

Use GitHub private vulnerability reporting:
<https://github.com/hmesutozsoy/oddsrail/security/advisories/new>

A useful report says which component and version is affected, how to
reproduce the problem, and what an attacker could do with it. Never include
a real private key, API key, relayer key or session token, yours or anyone
else's.

You will get a reply once the report has been read. Fixes to the MCP server
ship as a new release and are noted in [CHANGELOG.md](CHANGELOG.md).

## Scope

In scope:

- The `oddsrail` MCP server package: the source in this repository, the
  package published on PyPI and the desktop extension bundle built from it.
- The hosted connector at `https://mcp.oddsrail.app/mcp`, including its
  OAuth and email sign-in.

Out of scope:

- Polymarket, Kalshi, their APIs and SDKs, and other third-party services.
  Report problems in those to their owners.
- Findings that require an already compromised machine, or keys that were
  leaked outside oddsrail.
- Denial of service by traffic volume, spam, and social engineering.

## Testing rules

Use only your own accounts and keys. On the hosted connector, test with your
own paper account, do not read or change anyone else's data, and do not run
high-volume automated scans. Keep live trading tests to amounts you can
afford to lose; the server is dry-run by default for a reason.

## No bounty

There is no bug bounty program, and no payment or reward is promised for a
report.
