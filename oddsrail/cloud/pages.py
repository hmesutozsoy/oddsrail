"""The few HTML pages the hosted server serves itself: sign-in, link sent,
confirm, errors, and a front page that says what this endpoint is."""

from __future__ import annotations

from html import escape

CSS = """
:root{--bg:#0e1116;--bg2:#141922;--line:#232b38;--ink:#e8ebf0;--soft:#b6bdc9;--muted:#7d8797;--amber:#f59e0b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.6 -apple-system,BlinkMacSystemFont,"Inter","Segoe UI",Roboto,sans-serif}
main{max-width:520px;margin:0 auto;padding:56px 22px}
a{color:var(--amber);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.01em}.k{font:600 12px/1 ui-monospace,Menlo,monospace;
letter-spacing:.08em;text-transform:uppercase;color:var(--amber);margin:0 0 16px}
p{margin:0 0 14px;color:var(--soft)}.muted{color:var(--muted);font-size:14px}
form{margin:18px 0}label{display:block;font-size:14px;color:var(--soft);margin-bottom:6px}
input[type=email]{width:100%;padding:11px 12px;font:16px inherit;color:var(--ink);background:var(--bg2);
border:1px solid var(--line);border-radius:8px}
button{margin-top:12px;padding:11px 18px;font:600 15px inherit;color:#0e1116;background:var(--amber);
border:0;border-radius:8px;cursor:pointer}
code{font-family:ui-monospace,Menlo,monospace;font-size:.92em;background:var(--bg2);padding:2px 6px;border-radius:5px;overflow-wrap:anywhere}
.err{border-left:2px solid #e06c6c;padding-left:12px;color:#f0b4b4}
.box{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:16px 0}
ul{padding-left:20px;color:var(--soft)}li{margin:4px 0}
"""


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta name=\"robots\" content=\"noindex\"><title>{escape(title)}</title>"
            f"<style>{CSS}</style></head><body><main><p class=\"k\">oddsrail</p>{body}</main></body></html>")


def login(rid: str, client_name: str, error: str | None = None) -> str:
    err = f"<p class=\"err\">{escape(error)}</p>" if error else ""
    return page("Sign in to oddsrail", f"""
<h1>Sign in</h1>
<p><b>{escape(client_name)}</b> wants to connect to your oddsrail account.
Enter your email and we will send you a sign-in link. No password, no key.</p>
{err}
<form method="post" action="/login">
  <input type="hidden" name="req" value="{escape(rid)}">
  <label for="email">Email</label>
  <input id="email" name="email" type="email" required autocomplete="email" autofocus placeholder="you@example.com">
  <button type="submit">Send sign-in link</button>
</form>
<p class="muted">Your account holds a paper-trading ledger and nothing else. The hosted
server keeps no wallet keys and sends no real orders. <a href="https://oddsrail.app/privacy">Privacy</a></p>
""")


def sent(email: str, dev_link: str | None) -> str:
    dev = ""
    if dev_link:
        dev = (f"<div class=\"box\"><p class=\"muted\">Development mode: no mail service is configured, "
               f"so here is the link.</p><p><a href=\"{escape(dev_link)}\">{escape(dev_link)}</a></p></div>")
    return page("Check your email", f"""
<h1>Check your email</h1>
<p>We sent a sign-in link to <b>{escape(email)}</b>. Open it within 15 minutes and press
<b>Continue</b> there; this page can be closed.</p>
{dev}
<p class="muted">Nothing arrived? Check spam, or go back to the sign-in page and try again.</p>
""")


def confirm(token: str, email: str, client_name: str) -> str:
    return page("Confirm sign-in", f"""
<h1>Connect {escape(client_name)}</h1>
<p>Sign in as <b>{escape(email)}</b> and connect <b>{escape(client_name)}</b> to your
oddsrail account? It will be able to read market data and trade your <b>paper</b> ledger.
It cannot move money: there is none here.</p>
<form method="post" action="/login/verify">
  <input type="hidden" name="t" value="{escape(token)}">
  <button type="submit">Continue</button>
</form>
<p class="muted">Not you? Close this page; nothing happens until Continue is pressed.</p>
""")


def error(message: str) -> str:
    return page("oddsrail", f"<h1>That did not work</h1><p class=\"err\">{escape(message)}</p>"
                            f"<p class=\"muted\"><a href=\"https://oddsrail.app/\">oddsrail.app</a></p>")


def home(base: str, version: str) -> str:
    return page("oddsrail cloud", f"""
<h1>oddsrail cloud</h1>
<p>The hosted oddsrail MCP server, version {escape(version)}: Polymarket market data, signals,
deterministic order checks and paper trading, with a virtual bankroll per account. No keys,
no real money, nothing to install.</p>
<div class="box">
  <p><b>Add it to Claude</b></p>
  <ul>
    <li>Claude web or desktop: Settings, Connectors, Add custom connector.</li>
    <li>Server URL: <code>{escape(base)}/mcp</code></li>
    <li>Sign in with your email when Claude asks. That is the whole setup.</li>
  </ul>
  <p class="muted">Claude Code: <code>claude mcp add --transport http oddsrail {escape(base)}/mcp</code></p>
</div>
<p>Live trading with your own wallet is self-hosted, so your keys never leave your
machine: <code>pip install oddsrail</code>. Docs and the full story at
<a href="https://oddsrail.app/">oddsrail.app</a>. <a href="https://oddsrail.app/privacy">Privacy</a>.</p>
""")
