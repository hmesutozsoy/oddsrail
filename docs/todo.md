# Where oddsrail stands, and what is next

Updated 2026-09-11. The product works end to end; the audience is the gap.

## State of play

| Measure | Value |
|---|---|
| Version live everywhere | 0.17.0 (PyPI, MCP registry, VPS, site) |
| Hosted server | https://mcp.oddsrail.app, mail live, backups and watchdog running |
| Hosted accounts | 0 |
| Paper passes, last 7 days | 81, from 3 addresses, two of them the maintainer's |
| Arena entries | none yet, house reference row only |
| GitHub | 0 stars, 33 unique visitors in 14 days, 2 issues (both answered) |
| Builder rank this week | 137, $2,476 attributed, all the maintainer's own bot |
| Builder reward payouts | none ever received |
| Tests | 176, CI green |

The bottleneck is distribution, not the product.

## Mesut

Ordered by leverage. Nothing here needs code.

1. **Post where there is no age gate, this week.** r/ClaudeAI, r/mcp, X. The
   post is no longer "an MCP server"; it is "build a trading agent for
   Polymarket in ten seconds, no account", linking https://oddsrail.app/build.
   Draft text is in `docs/launch-post.md`.
2. **Directory submissions.** Each is one form or one GitHub issue.
   - Glama: sign in with GitHub at glama.ai, open the oddsrail listing, Claim
     ownership. The `glama.json` claim file is already in the repo.
   - mcpservers.org/submit, with the repo URL.
   - mcp.so: a new issue on github.com/chatmcp/mcpso.
   - mcpmarket.com/submit, with the repo URL.
   - Cline: an issue on github.com/cline/mcp-marketplace.
   - Smithery: now possible, the hosted endpoint exists and is in `server.json`.
3. **Google Search Console.** Add a Domain property for oddsrail.app, put the
   TXT record it gives you in Cloudflare, then submit
   https://oddsrail.app/sitemap.xml. Bing is already covered by IndexNow.
4. **Keep commenting on Hacker News** until submitting unlocks, then post the
   Show HN with the first comment from `docs/launch-post.md`. Never ask anyone
   for upvotes.
5. **Be the first arena entry.** Open the sign-in link that is already in your
   inbox, name an agent, tick hourly and the board. An empty board converts
   nobody.
6. **Decide on GitHub issue #1**, the Headline Arena venue proposal. My read:
   a no-funds forecasting venue is off-thesis while revenue comes from
   attributed Polymarket volume, so decline politely and keep the contact. Say
   the word and I will post that.
7. **Email the Polymarket BD contact** with the arena and builder links, the
   co-marketing ask, and the two open questions: whether unverified codes earn
   a pool share, and whether the realised payout rate can be published.
8. **Domain auto-renew** for oddsrail.app, if it is not already on.

## Claude

Ordered by what a first visitor or first developer hits soonest.

1. **The `market_id` ambiguity.** `find_markets` returns a token id, but
   `resolution_criteria`, `settlement_audit` and `dispute_risk` want a slug.
   Any agent chaining two tools trips on this. Accept either everywhere.
2. **Six footgun pages at `/notes/<slug>`.** The only content on the site with
   real search demand is compressed into one list on the home page. Each note
   is a page someone actually searches for at 2am.
3. **`place_order` dry-run has no `accepted` field**, unlike every other path,
   and a paper refusal hides under a top level that reads like success.
4. **No Polymarket balance tool.** Kalshi has one, so an agent cannot size
   against real collateral on the venue that matters.
5. **`daily_review` returns nothing on a fresh install**, because its first
   three steps all need a trading key. It should read the paper ledger.
6. **Guardrail claim is wrong for two of four rules.** Session notional and
   open-order caps are live only, while `server_info` implies dry-run
   enforcement.
7. **Stale marks never expire.** A position whose book vanishes keeps its last
   mark forever, so equity never writes down a dead market.
8. **Email normalisation.** `a+1@gmail.com` is a second account today, which
   matters now that the board has an activity floor to game.
9. **Cloud tests measure 0% coverage** because they drive a subprocess. Move
   most of them onto an in-process ASGI client and keep one boot test.
10. **CI gates only pytest.** Add the em-dash lint for prose, a JavaScript
    syntax check for `site/*.js`, and a scheduled smoke test against the live
    host.

## Standing checks

- Builder wallet 0xBCC1…FE0F for the first reward payout, and the realised
  rate against attributed volume when it arrives.
- The arena board and the hosted server after every release.
- Backups land nightly in /var/backups/oddsrail-cloud; the watchdog restarts a
  dead app or a stale scheduler.
