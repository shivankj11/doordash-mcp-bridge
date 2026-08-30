# doordash-mcp-bridge

Exposes a fixed subset of `dd-cli` as a remote MCP server, so a Claude Tag
session in Slack can search DoorDash, browse menus, build and price carts, and
hand back a checkout URL. It cannot place an order or charge a card.

## Why a bridge

dd-cli can't run inside a Claude Tag sandbox. Three independent blockers:

1. The only published build is `Mach-O 64-bit executable arm64` (macOS, Apple
   Silicon). There is no Linux release; Claude Tag sandboxes are Linux.
2. `dd-cli login` completes an OAuth redirect on **localhost** and stores
   credentials in the **OS keychain**. A sandbox has neither.
3. Claude Tag sandboxes are ephemeral — "the thread is durable; the sandbox is
   not" — so even a successful login wouldn't survive the first idle period.

So don't move dd-cli to the sandbox. Keep it on the Mac and let the sandbox call
it:

```
Slack @Claude → Claude Tag sandbox → HTTPS tunnel → this server → dd-cli → DoorDash
```

## Scope: 24 tools, no way to charge a card

Each tool is one entry in `TOOL_SPECS` with a hard-coded dd-cli argv. Callers
supply option *values* — never option names, never a subcommand — so no request
can reach a command that isn't in the table. `python3 server.py --list-tools`
prints the current set.

| | Tools |
|---|---|
| **Read** | `dd_search`, `dd_find_nearby_stores`, `dd_find_items`, `dd_menu`, `dd_store_details`, `dd_restaurant_item_details`, `dd_promo_list`, `dd_order_history`, `dd_order_status`, `dd_order_receipt`, `dd_cart_list`, `dd_cart_show`, `dd_order_preview`, `dd_address_find`, `dd_order_checkout_url` |
| **Write** (no charge) | `dd_cart_add_items`, `dd_cart_remove_item`, `dd_cart_delete`, `dd_order_reorder`, `dd_build_grocery_list`, `dd_promo_apply`, `dd_promo_remove`, `dd_address_set`, `dd_address_add` |

**Absent on purpose:**

- `order submit` — the only command that charges a card. A startup assertion
  (`FORBIDDEN_ARGV`) fails the process if a spec ever maps to it.
- `address list`, `payment-method list` — would put a home address and card
  metadata into a Slack thread and into Claude Tag's channel memory, which
  persists.
- `login` — only ever runs on the Mac, interactively.

Checkout ends at `dd_order_checkout_url`, which returns a URL a human opens to
pay. That is a safety property, not an oversight. From `LICENSE.txt`:

- **§5.3** — DoorDash "may not provide a human confirmation step before an order
  initiated through the CLI is finalized," and by configuring an agent you
  "expressly preauthorize" it to complete transactions "without additional human
  intervention at the point of checkout."
- **§7.2** — you are responsible for all charges "regardless of whether you
  personally initiated, reviewed, or approved each individual transaction."

Meanwhile the Claude Tag docs warn that Claude "may follow directions from other
messages in the context," and *anyone in a channel can steer a running session*.
Search can waste a query. Checkout can't be taken back. **Before adding any write
tool, re-read §5.3 and §7.2.**

Also relevant if you ever widen this: **§5.5** limits the agent to the *single*
DoorDash account tied to the credentials, and **§5.2** makes that account's owner
solely responsible. A shared Claude Tag channel identity driving your personal
DoorDash account sits badly against both — prefer a DM or a single-member private
channel.

## Response handling

`dd-cli --json-output` returns a GUI-oriented envelope:
`{content: [...], structuredContent: {...}, isError: bool}` where `content[0].text`
duplicates `structuredContent` verbatim. The server forwards only
`structuredContent`, minus:

| Dropped | Why |
|---|---|
| `widget_type`, `assistant_instructions` | dd-cli's own `--help` says agents must ignore these. `assistant_instructions` reads "The interactive widget is now displayed… Do NOT output additional text" — in Slack there is no widget, so passing it through makes Claude go silent instead of answering. It's upstream text that would land in the model's context as instruction. |
| `delivery_address`, `address_id` | Every search response carries the consumer's saved delivery address. Search results don't need it, and anything returned here can reach a Slack thread and Claude Tag's channel/workspace memory, which persists — against `LICENSE.txt` §6.4, which limits retention to what the transaction requires. Set `DD_BRIDGE_INCLUDE_ADDRESS=1` to keep them. |
| `session_id`, `trace_id` | Internal handles, useless to a caller here. |

## Run it

```bash
export DD_BRIDGE_TOKEN=$(openssl rand -hex 32)   # required; server refuses to start without it
export DD_LAT=37.7749 DD_LNG=-122.4194           # optional fallback location
python3 server.py --port 8787
```

Binds `127.0.0.1` by default — put the tunnel in front of it rather than binding
a public interface. Then expose it over HTTPS with a stable hostname:

```bash
# current setup — ngrok's permanent free dev domain (since 2026-08-06)
ngrok http http://127.0.0.1:8787 --url=https://your-subdomain.ngrok-free.dev

# what this used to be: a cloudflared quick tunnel, new hostname every restart
cloudflared tunnel --url http://127.0.0.1:8787
```

Either way, pass the upstream as `http://127.0.0.1:8787`, never a bare port or
`localhost` — `localhost` resolves to `::1` first and the server binds IPv4 only,
so every request burns a refused connection before falling back, and when the
fallback doesn't happen you get 502s that look like flakiness.

Local smoke test:

```bash
curl -s 127.0.0.1:8787/health
curl -s -X POST 127.0.0.1:8787/mcp -H "Authorization: Bearer $DD_BRIDGE_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

To test it as a local stdio server in Claude Code instead:

```bash
claude mcp add doordash --scope user -- python3 ~/.claude/mcp-servers/doordash-mcp-bridge/server.py --stdio
```

## Run it without holding terminals open

### Why not Cloudflare Workers

You can't move this to a Worker. dd-cli ships **darwin-arm64 only** (no Linux
build, no Docker image, as of v0.2.1), Workers are V8 isolates that can't execute
a native binary at all, and Containers / Sandbox SDK are **linux/amd64** — there
is no macOS target anywhere on the platform. Unpacking the PyInstaller bundle to
get a Linux-runnable tree runs into `LICENSE.txt` §80(c) (no reverse-engineering
or decompiling to derive architecture or proprietary methods) and §4.3
(no circumvention).

Auth rules it out independently: `dd-cli login` needs a browser redirect to
localhost plus the macOS keychain, and a serverless instance is ephemeral, so
you'd re-authenticate constantly even if a Linux build appeared.

**Something must run on this Mac.** What you can remove is the *terminal windows*.

### 1. The bridge as a LaunchAgent

`com.example.doordash-mcp-bridge.plist` in this directory. Put your existing token in
it (the one already in the Claude Tag Bearer credential — a new one breaks that),
then:

```bash
cp com.example.doordash-mcp-bridge.plist ~/Library/LaunchAgents/
chmod 600 ~/Library/LaunchAgents/com.example.doordash-mcp-bridge.plist   # it holds a secret
launchctl load ~/Library/LaunchAgents/com.example.doordash-mcp-bridge.plist
```

`RunAtLoad` + `KeepAlive` means it starts at login and restarts if it dies. Logs,
including the per-request `auth=` line, go to
`~/Library/Logs/doordash-mcp-bridge.log`. To pick up a code change:

```bash
launchctl kickstart -k gui/$(id -u)/com.example.doordash-mcp-bridge
```

Interpreter is pinned to `/opt/homebrew/bin/python3`. The bridge runs on macOS's
own 3.9 too, but pinning avoids depending on whichever `python3` launchd resolves.

### 2. A stable hostname instead of a quick tunnel

The quick tunnel's hostname changes on every restart, which invalidates both the
plugin's `.mcp.json` and the credential's Allowed-websites entry.

**What this bridge does now (2026-08-06):** ngrok's free plan assigns one permanent
dev domain per account — no domain to own, no DNS to configure, endpoints don't
expire. Caps are 20k requests and 1 GB/month, 3 concurrent endpoints, and the
browser interstitial doesn't apply to programmatic clients (a `claude-code/*` user
agent gets a clean 200).

```bash
brew install --cask ngrok
ngrok config add-authtoken <token>
ngrok http http://127.0.0.1:8787 --url=https://your-subdomain.ngrok-free.dev
```

The cloudflared **named tunnel** below is the alternative, and it's what we tried
first. It needs a zone already in your Cloudflare account — which meant a
nameserver change at the registrar, and that registrar account was locked out. If
you have a zone, it's still the cleaner option, and unlike ngrok it installs as a
launchd service directly:

```bash
cloudflared tunnel login                          # interactive, needs a browser
cloudflared tunnel create dd-bridge
cloudflared tunnel route dns dd-bridge dd.example.com
# ~/.cloudflared/config.yml:
#   tunnel: <tunnel-id>
#   credentials-file: $HOME/.cloudflared/<tunnel-id>.json
#   ingress:
#     - hostname: dd.example.com
#       service: http://127.0.0.1:8787
#     - service: http_status:404
sudo cloudflared service install
```

Then update `.mcp.json` and the Bearer credential's Allowed websites to
`dd.example.com` **once**, and never again.

### 3. Sleep is the remaining gap

Both services still need the Mac awake. A sleeping laptop means the tool fails
with an unreachable host. On AC power, disable sleep for the bridge's sake
(Settings → Battery → Options, or `caffeinate -s`), or accept that it's available
only while you're at the machine.

## Wire it to Claude Tag

Requires **Team or Enterprise** — Claude Tag isn't available on Free, Pro, or Max.
At [`claude.ai/admin-settings/claude-tag`](https://claude.ai/admin-settings/claude-tag),
in an Access bundle:

1. **Plugins.** Not a zip upload. Claude Tag takes org plugins from a **git repo
   registered as a plugin marketplace**. (The console can also upload individual
   skills one at a time, but that route can't carry the `.mcp.json` this bridge
   needs.) Push `doordash-marketplace/` to a GitHub repo in your org, add it as an
   organization plugin source with **Sync automatically** on, then in the bundle's
   **Plugins** section select **+** and add the marketplace. Registering makes it
   *available*, not active — toggle it on. An `.mcp.json` in a repo Claude merely
   *clones* is not loaded; it works only because it ships inside an attached plugin.

   Layout, both manifests passing `claude plugin validate`:

   ```
   doordash-marketplace/
   ├── .claude-plugin/marketplace.json      # catalog: name, owner, plugin list
   └── plugins/doordash/
       ├── .claude-plugin/plugin.json       # plugin manifest — name: doordash
       └── .mcp.json                        # server key: doordash; points at the tunnel host
   ```

   A plugin can also ship a `skills/<name>/SKILL.md` alongside these, which is
   how you'd give a model prose guidance rather than only tool schemas. This
   repo doesn't include one — the tool descriptions in `TOOL_SPECS` carry that
   guidance instead, and they ride in `tools/list` so a restart is enough to
   update them.

   If you rename the plugin, seven things move together: `marketplace.json`,
   `plugin.json` (`name`, `displayName`, `version`), `.mcp.json`'s server key, both
   directory names, the `SKILL.md` frontmatter, and — easiest to miss —
   `bridge.sh`'s `PLUGIN_DIR` and the server key it writes into `.mcp.json`. Miss
   either of the last two and `./bridge.sh up` breaks silently, either pointing at
   a directory that no longer exists or adding a second, stale server entry.

   The bare tool names (`dd_search`, `dd_menu`, …) don't change on a rename — only
   the namespace does, since it's derived as
   `mcp__plugin_<pluginName>_<serverKey>__<toolName>`. With plugin name and server
   key both `doordash`, that's `mcp__plugin_doordash_doordash__dd_search`; the
   doubled segment is expected, not a typo.
2. **Credentials tab** → **Connect another tool** → credential type **Bearer**,
   value `$DD_BRIDGE_TOKEN`, **Allowed websites** set to your tunnel host only.
   The Agent Proxy attaches it on the way out, so the token never sits in the
   sandbox or in `.mcp.json`.
3. Attach the bundle to the narrowest scope that needs it.
4. Verify in a **new** thread (a running thread keeps the plugin set it started
   with): `@Claude what DoorDash tools can you access from this channel?`

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `DD_BRIDGE_TOKEN` | yes (HTTP) | Bearer token every request must present. No default — fails closed. |
| `DD_CLI_PATH` | no | Path to dd-cli. Defaults to `which dd-cli`, then `~/.local/bin/dd-cli`. |
| `DD_LAT` / `DD_LNG` | no | Fallback location when a caller sends no coordinates. Both or neither. Without them dd-cli falls back to a Cupertino default, which returns confidently wrong results. |
| `DD_BRIDGE_INCLUDE_ADDRESS` | no | `1` keeps `delivery_address`/`address_id` in responses. |

## Guardrails in the code

- **No shell.** `subprocess.run` takes a fixed argv list, never a string.
  Verified: a query of `pizza"; rm -f /tmp/canary; echo "` reached DoorDash as a
  literal search string and the canary file survived.
- **Fails closed on auth.** Missing `DD_BRIDGE_TOKEN` aborts startup; bad tokens
  get 401 via `hmac.compare_digest`.
- **Bounded inputs.** `query` ≤ 200 chars, `intent` ≤ 1000, `limit` and `max` range-checked (out-of-range is rejected, not clamped), coordinates range- and finiteness-checked, latitude/longitude required as
  a pair so a half-specified location can't pair with a stale env value.
- **Bounded work.** 64 KB max body, 60 s subprocess timeout, and one search per
  2 s process-wide (`LICENSE.txt` §9.1 obliges compliance with rate limits).

## Order history is scoped to the current day

`dd_order_status`, `dd_order_receipt`, and `dd_order_reorder` all need an
`order_uuid`, and `order history` is the only command that lists them —
`order checkout-url` can't, because no order exists yet when it returns (the cart
"stays editable there until the consumer completes checkout"). So
`dd_order_history` is exposed, but hard-limited to **today**:

- `--days 1` is pinned in `fixed_args`, and no caller-settable `days` parameter
  exists, so the window can't be widened from the outside.
- `--days 1` is a *rolling* 24 hours, which still leaks yesterday. So
  `keep_only_today` post-filters on `order_date`, **converted to local time**.
  This matters: an order placed 7:41pm PDT is stamped `02:41Z` the following day,
  and a naive UTC date comparison would withhold today's dinner while admitting
  last night's. Verified by test.
- Unparseable timestamps are withheld — it fails closed.
- `max` is 1–10 (dd-cli's own default is 50) and out-of-range values are
  **rejected**, not silently clamped, so a caller can't quietly get a different
  window than it asked for.
- Every response carries `bridge_scope_note` stating the date and how many orders
  were withheld, so an empty list can't be misread as "never ordered anything".

`dd_address_set` has the same id-discovery shape: it needs an `address_id`, and
`address list` stays unexposed. That one is intentional and stays — the address
list is exactly the PII worth keeping out of Slack.

## Adding an address without being able to read the address list

dd-cli 0.2.3 added `address find` (resolve free text into candidates, saves
nothing) and `address add` (save a candidate *and* make it the account default).
Both are exposed; `address list` still is not. That combination is deliberate but
it has a sharp edge worth stating plainly.

dd-cli's own guidance for both commands is "check `address list` first, because
`address add` does not dedupe." This bridge cannot follow that advice — the list
is the PII the bridge exists to withhold. So the dedupe check is pushed to the
human: `dd_address_find`'s `next_step` and `dd_address_add`'s description both
tell the caller to ask the requester whether the address is already saved, and
to *not* auto-retry a failed add, since a retry is how you get the same address
saved twice.

Two further notes:

- `address find` returns full street text in `candidates[].description`, and that
  is not redacted. It is the one place a street address is allowed through,
  because the address came *from the requester in this conversation* and there is
  no way to confirm the right candidate without showing it. This is different in
  kind from `address list`, which would surface a saved home address that nobody
  in the thread asked about.
- `address add --yes` is pinned in `fixed_args`, exactly as for `address set`.
  Its `--help` is explicit that a non-TTY caller omitting `--yes` hangs on an
  interactive `Proceed?`. `stdin` is already `/dev/null`, so the prompt would EOF
  rather than hang to the timeout, but the call would fail for no reason. The
  cost is that there is no second confirmation gate inside dd-cli — the tool
  description carries that weight instead.

### `next_step` is rewritten, not forwarded

`address find` returns a `next_step` string that ends "...call `save_address`
with the chosen `place_id` and `confirmed=true`." No such tool exists here; the
bridge's is `dd_address_add` and it has no `confirmed` argument. Forwarded as-is,
that is upstream-authored text instructing the model to call something it cannot
call — the same failure mode `WIDGET_KEYS` exists to prevent. `retarget_address_next_step`
replaces it with the bridge's own wording. It is rewritten rather than dropped
because the sequence it describes (show candidates → explicit human confirmation
→ only then save) is the behaviour we want; only the tool name and the dedupe
advice were wrong.

## Don't use "Add custom connector"

The claude.ai connector gallery has an **Add custom connector** dialog that takes
an MCP URL. It looks like the right place. It isn't, for this bridge:

- It offers only **OAuth Client ID/Secret** and **Individual sign-in**. There is no
  static-header field, and this server implements no OAuth — so it saves and then
  lists no tools, because the `401 WWW-Authenticate: Bearer` it gets back is not an
  OAuth discovery response.
- Static bearer tokens for connectors exist only via `static_headers`, an org-admin
  beta not exposed in that dialog.
- A token in the URL is not a workaround: the MCP authorization spec prohibits
  access tokens in the query string.
- Connectors added there are personal, so they apply in Claude Tag **DMs only**,
  not channels.

Use the Access bundle's **Credentials → Connect another tool → Bearer** instead,
per "Wire it to Claude Tag" above.

## Gotcha

`pkill -f doordash-mcp-bridge` will **not** match this server — argv is just
`python3 server.py`. A stale instance holding the port makes a restart die with
`EADDRINUSE` while the *old code* keeps answering, which looks exactly like your
change not working. Find and kill it by PID:

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN
```
