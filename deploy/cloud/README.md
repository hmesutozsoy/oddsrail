# Deploying the hosted oddsrail

One VPS, one process, one sqlite file. Caddy in front for TLS.

```
apt install caddy
mkdir -p /var/lib/oddsrail-cloud && chown mm:mm /var/lib/oddsrail-cloud && chmod 700 /var/lib/oddsrail-cloud
cp deploy/cloud/env.example /etc/oddsrail-cloud.env && chmod 600 /etc/oddsrail-cloud.env   # then edit
cp deploy/cloud/oddsrail-cloud.service /etc/systemd/system/
cp deploy/cloud/Caddyfile /etc/caddy/Caddyfile
ufw allow 80/tcp && ufw allow 443/tcp
systemctl daemon-reload && systemctl enable --now oddsrail-cloud caddy
```

Checks:

```
curl -s https://mcp.oddsrail.app/healthz
curl -s https://mcp.oddsrail.app/.well-known/oauth-authorization-server
journalctl -u oddsrail-cloud -n 50
```

Users add `https://mcp.oddsrail.app/mcp` as a custom connector in Claude
(Settings, Connectors, Add custom connector) and sign in with their email.
Claude Code: `claude mcp add --transport http oddsrail https://mcp.oddsrail.app/mcp`.

What the hosted profile removes and why: `oddsrail/hosted.py`.
