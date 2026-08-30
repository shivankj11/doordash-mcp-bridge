# dd-cli MCP bridge — runbook

Operating guide for the bridge that lets **Claude Tag in Slack** drive `dd-cli`.
Written for an agent: run the command, read the expected output, follow the
branch. Prefer `./bridge.sh` over ad-hoc commands — it encodes the traps below.

| | |
|---|---|
| Script | `./bridge.sh` (this directory) |
| Bridge code | `~/.claude/mcp-servers/doordash-mcp-bridge/server.py` |
| Plugin | `~/.claude/mcp-servers/doordash-mcp-bridge/doordash-marketplace/plugins/doordash/` |
| Plugin id / server key | both `doordash` → tools are `mcp__plugin_doordash_doordash__dd_search` etc. |
| Tunnel | **ngrok**, permanent domain `your-subdomain.ngrok-free.dev` (was rotating cloudflared quick tunnels until 2026-08-06) |
| State (token, hostname, logs) | `~/.config/dd-bridge/` |
| Claude Tag admin | `claude.ai/admin-settings/claude-tag` |

## Architecture, in one line

```
Slack @Claude → Claude Tag sandbox (Anthropic, Linux) → HTTPS tunnel → server.py (this Mac) → dd-cli → DoorDash
```

`dd-cli` ships **darwin-arm64 only** and authenticates via a localhost browser
redirect plus the macOS keychain, so it cannot run in the sandbox — the Mac is
required. It cannot be moved to Cloudflare Workers or Containers either
(V8 isolates can't exec binaries; Containers are linux/amd64).

## Start everything

```bash
./bridge.sh up
```

Idempotent: if the server or tunnel is already up it says so and leaves them
alone. It then rewrites the plugin's `.mcp.json` to the current hostname,
repackages `doordash-plugin.zip`, and tells you whether the hostname changed.

> **⚠ Do not run `up` or `down` while the bridge is on ngrok.** Both still assume
> **cloudflared** is the tunnel. With ngrok serving, `up` sees no cloudflared,
> starts a *quick tunnel* anyway, then rewrites `.mcp.json` to that
> `trycloudflare.com` URL and repackages the zip — silently breaking the uploaded
> Claude Tag plugin. `down` would kill the server and leave ngrok pointing at
> nothing. Until `bridge.sh` is made ngrok-aware, reload the server **alone**:
>
> ```bash
> kill $(lsof -nP -iTCP:8787 -sTCP:LISTEN -t)
> cd ~/.claude/mcp-servers/doordash-mcp-bridge && \
>   DD_BRIDGE_TOKEN=$(cat ~/.config/dd-bridge/token) \
>   DD_LAT=37.7749 DD_LNG=-122.4194 \
>   DD_CLI_PATH=$HOME/.local/bin/dd-cli \
>   nohup /opt/homebrew/bin/python3 server.py --port 8787 \
>   >> ~/.config/dd-bridge/server.log 2>&1 &
> ```
>
> Start the tunnel by hand when needed — it reconnects to the same domain:
>
> ```bash
> nohup ngrok http http://127.0.0.1:8787 \
>   --url=https://your-subdomain.ngrok-free.dev \
>   --log=stdout >> ~/.config/dd-bridge/ngrok.log 2>&1 &
> ```
>
> Note `http://127.0.0.1:8787`, not a bare `8787` — bare resolves via `localhost`
> to `::1` first, and the server binds IPv4 only. Same failure as trap 6.

```bash
./bridge.sh status     # what's running, which version, which hostname
./bridge.sh verify     # 4-step diagnostic ladder (needs the token)
./bridge.sh logs       # both logs; server log has the per-request auth= line
./bridge.sh down       # stop both
./bridge.sh token      # the bearer token, for the Claude Tag credential
./bridge.sh host <h>   # record the hostname of a tunnel you started by hand
```

A healthy `status` ends with:

```
{"ok": true, "server": "doordash-mcp-bridge", "version": "2.2.0", "tools": 24}
```

One knowingly-stale reading while on ngrok, so it doesn't send you debugging
nothing:

- `status` prints a red **`FAIL cloudflared not running`**. Cosmetic — it hardcodes
  cloudflared as the tunnel type. The `/health` line below it is the real check.

`SERVER_VERSION` and `plugin.json` are both `2.2.0`, so a mismatch between
`/health` and the plugin now means a stale process, not a known quirk.

## Five traps that cost hours

1. **`pkill -f` does not match the server.** Its argv is just `python3
   server.py`. A stale instance keeps port 8787 and keeps answering **with old
   code**, while your restart dies on `EADDRINUSE` — so a fix looks like it
   didn't work. Kill by PID: `lsof -nP -iTCP:8787 -sTCP:LISTEN`. `./bridge.sh
   down` does this correctly.

2. **`/health` reports the running version — use it as the canary.** After any
   code change, confirm `version` matches `SERVER_VERSION` in `server.py`.
   `./bridge.sh status` compares them and shouts if they differ. This is the
   single fastest way to catch "I edited the file but never restarted."

3. **A quick tunnel gets a new hostname every start** — which is why the bridge no
   longer uses one. Every restart silently invalidated both `.mcp.json` *and* the
   credential's allowed-websites entry; on 2026-08-06 that burned an evening and
   four hostnames in one session. The bridge now runs on ngrok's permanent free
   dev domain, so the hostname survives restarts and reboots. See "Make the
   hostname stable" below.

4. **Sessions freeze their tool set and skills at start — and a new thread does
   not always mean a new session.** After *any* change — restart, plugin
   re-upload, credential edit — you need a fresh **session**, not just a fresh
   thread. On Claude Tag version **"New"** (channel → Configure → Claude Tag
   version) one session is reused for the whole channel and *resumes* across
   top-level threads: the reply shows a `Resumed session` marker and the same
   `session_...` id in its footer. A resumed session keeps its stale tool set
   **and its earlier conclusions**, so it will confidently report the tool
   doesn't exist without retrying — check the bridge log, and you'll see it made
   no request at all.

   **Why a new top-level message isn't enough:** each *thread* has its own
   session, but a channel also has one **channel session** used for everything at
   its top level. Posting three new top-level messages reuses that one session
   three times. Asking in prose ("start a NEW session") does nothing — it's a
   command, not a phrase:

   ```
   @Claude !restart
   ```

   It must stand alone; extra words make it an ordinary prompt.

   **Observed 2026-08-06 in `#your-channel`:** `!restart` only took effect in a
   thread that had its **own** thread session — the platform answered
   `♻️ Session restarted.` and the `session_...` id changed. In threads answered by
   the **channel** session (every top-level `@Claude` message), the same command
   fell through to the model, which replied that it has no such command — with
   and without a space after the mention. If that happens, get a thread session
   first: ask for something that spins one up (e.g. `@Claude create a code
   session`), then `!restart` inside that thread. After the restart the fresh
   session listed `dd_search`, `dd_menu`, `dd_find_items` immediately.

   Confirm it worked by comparing the `session_...` id in the reply footer against
   the previous one — if it matches, you are still in the old session and nothing
   you fixed will be visible. Docs:
   <https://claude.com/docs/claude-tag/users/commands>

5. **The token must not change.** The Claude Tag Bearer credential holds a copy,
   so a new token means every call 401s. Only `up` ever creates one — `verify`,
   `token`, and `status` are read-only by design, because minting a token inside
   a *diagnostic* would turn a debugging session into a fresh outage.

   **Seed the real token once.** If the server is already running, its token
   lives only in that process's environment and nothing on disk knows it:

   ```bash
   DD_BRIDGE_TOKEN=<the value in the Claude Tag credential> ./bridge.sh up
   ```

   That persists it to `~/.config/dd-bridge/token` (mode 600), and every later
   `up` / `verify` / `token` reuses it. Until you do this, `verify` and `token`
   will correctly refuse to run rather than guess.

6. **Point the tunnel at `127.0.0.1`, never `localhost`.** The server binds IPv4
   only; `localhost` resolves to `::1` first, which is refused. cloudflared falls
   back to IPv4 so it *works*, but every request burns a failed connection first,
   and when the fallback doesn't happen you get 502s that look like intermittent
   flakiness. The giveaway in the cloudflared log is
   `dial tcp [::1]:8787: connect: connection refused`. Note that during a real
   outage the same line appears, because `::1` is simply what it tried first —
   check whether anything is listening before blaming IPv6.

## Debugging Claude Tag: the decision tree

Symptom is almost always "Claude says it has no DoorDash tools" or "tool call
fails". Work top-down; each step isolates one layer.

### Step 1 — Is the bridge itself healthy?

```bash
./bridge.sh status
```

- `nothing listening on 8787` → `./bridge.sh up`
- `SERVING vX BUT DISK IS vY` → stale process: `./bridge.sh down && ./bridge.sh up`
- `cloudflared not running` → `./bridge.sh up`
- No `/health` response → tunnel is up but not routing; check `./bridge.sh logs`

### Step 2 — Can the sandbox reach it? (egress)

In a **new** Slack thread:

```
@Claude fetch https://<hostname>/health and show me the response
```

`/health` is unauthenticated on purpose, so this tests **egress only**.

- Returns `{"ok": true, ...}` → egress fine, go to Step 3.
- "can't reach" / names a blocked host → the host is on no allow layer. Add the
  Bearer credential (its allowed-websites doubles as the egress allow), or add
  the host on the bundle's **Domains** tab. A request to a host Claude Tag hasn't
  allowed is *blocked, not sent*.

### Step 3 — Is the credential being injected? (auth)

A passing Step 2 does **not** prove auth works — `/health` needs none. In a new
thread:

```
@Claude use curl to POST {"jsonrpc":"2.0","id":1,"method":"tools/list"} to
https://<hostname>/mcp with Content-Type: application/json, and show the raw response
```

Then read the server log — every request logs an `auth=` classification (never
the token itself):

```bash
./bridge.sh logs
```

| Log line | Meaning | Fix |
|---|---|---|
| `auth=ok` | Credential injected correctly | Auth is fine → Step 4 |
| `auth=ok-bare` | Token sent with no `Bearer ` scheme, accepted anyway | Auth is fine → Step 4. Expected from claude-code 2.1.224–2.1.226 and the claude.ai connector, which drop the prefix. When this stops appearing, the client was fixed — delete the bare-token branch in `_auth_state()` |
| `auth=absent` | Agent Proxy attached nothing | Credential missing, or its allowed-websites doesn't cover this host |
| `auth=bearer-mismatch` | Wrong token attached | Re-enter the credential value; compare with `./bridge.sh token` |
| `auth=wrong-scheme:Basic` | Wrong credential type | Must be **Bearer**, not Basic |
| *no line at all* | Request never arrived | Blocked upstream, or a **path/method restriction** on the credential |

**Check path/method restrictions.** Credential row → **⋮** → **Edit**. A
restriction allowing `GET` but not `POST`, or a path prefix excluding `/mcp`,
produces a working `/health` and an invisible tool — the two symptoms together.

### Step 4 — Did the plugin load?

```
@Claude what can you access from this channel?
```

- DoorDash tools not listed → the plugin isn't attached to this scope. In the
  Access bundle: uploaded **and toggled on** (registering only makes it
  *available*). Then a new **session** (trap 4 — a new thread may resume the old
  one). Check the channel's Access bundles list: an **inherited** bundle from the
  workspace default counts as attached, so "inherited" is not a problem to fix.
- Claude says the plugin/tool doesn't exist → before believing it, check whether
  the bridge logged **any** request for that turn. No new line means it never
  tried and is repeating a cached conclusion. Two searches that return nothing
  even when everything works: `SearchPlugins` reads the **public marketplace**,
  which an Access-bundle upload is not in; and the tool names use underscores
  (`dd_search`, `dd_cart_add_items`), so searching for the plugin id `doordash`
  misses. Re-ask by tool name in a new session.
- Listed but calls fail → Step 5.

### Step 5 — Tool call fails

Run the local ladder to rule out the bridge:

```bash
DD_BRIDGE_TOKEN=$(./bridge.sh token) ./bridge.sh verify
```

All four steps green means the bridge, auth, and tool table are all correct, and
the problem is in how the call is being *composed*. Common causes:

- **Customizations rejected** — the key is `nested_options`, a flat array of
  `{id, name, quantity}`, with the `o_` prefix stripped. Not `extras`, not
  `options`. DoorDash **silently ignores unknown keys**, so a wrong key name
  yields the same "Please select at least 1 options for X" error for every
  variant — that is evidence the *key* is wrong, not that the field was dropped
  in transit. Never conclude the tool is broken from identical errors alone.
- **Zero search results** — `dd_search`/`dd_find_nearby_stores` need
  `latitude`/`longitude` **together**. Without them dd-cli falls back to a
  Cupertino default and returns `[]` with `needs_address: true`, which reads as
  "search is broken". `DD_LAT`/`DD_LNG` in the environment supply the fallback.
- **Empty order history** — by design: only orders placed **today** (local time)
  are exposed. Check `bridge_scope_note` in the response before concluding
  anything.
- **Grocery item not found** — `dd_search` is restaurant-only. Use
  `dd_find_nearby_stores --vertical` then `dd_find_items`.

## Make the hostname stable

**Done — the bridge is on ngrok as of 2026-08-06.** ngrok's free plan assigns one
permanent dev domain per account, so the hostname survives restarts and reboots
with no domain and no DNS:

```bash
brew install --cask ngrok
ngrok config add-authtoken <token>          # dashboard.ngrok.com/get-started/your-authtoken
ngrok http http://127.0.0.1:8787 --url=https://your-subdomain.ngrok-free.dev
./bridge.sh host your-subdomain.ngrok-free.dev
```

Free-tier caps: 20k HTTP requests and 1 GB per month, 3 concurrent endpoints —
far above this workload. Endpoints don't expire. The browser interstitial does
**not** apply to programmatic clients; a `claude-code/2.1.223` user agent gets a
clean 200, verified.

`bridge.sh host` only records the hostname — it does **not** rewrite `.mcp.json`
or repackage the zip (that lives in `up`, which you can't use here — see the
warning under "Start everything"). Do those two by hand after a hostname change:

```bash
# edit .mcp.json's url, then:
cd ~/.claude/mcp-servers/doordash-mcp-bridge/doordash-marketplace/plugins/doordash && \
  rm -f ../../../doordash-plugin.zip && \
  zip -rq ../../../doordash-plugin.zip . -x '.DS_Store'
```

### The cloudflared named-tunnel route, if you ever want it back

A Cloudflare named tunnel needs a **zone in your Cloudflare account**, which means
a nameserver change at whatever registrar holds the domain. Worth knowing before
you start: there's no way around the registrar. Cloudflare's partial/CNAME setup
needs a record added at your current DNS host, and transferring the domain needs
an unlock plus an auth code from the registrar — so if you can't sign in there,
every route is blocked and ngrok's free dev domain is the way through.

If you do get a zone:

```bash
cloudflared tunnel login            # interactive, opens a browser
cloudflared tunnel create dd-bridge
cloudflared tunnel route dns dd-bridge dd.example.com
cat > ~/.cloudflared/config.yml <<'YAML'
tunnel: <tunnel-id>
credentials-file: $HOME/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: dd.example.com
    service: http://127.0.0.1:8787
  - service: http_status:404
YAML
sudo cloudflared service install    # auto-starts at boot
```

`bridge.sh` detects `config.yml` and runs the named tunnel instead of a quick
one. Update `.mcp.json` and the credential's allowed websites **once**.

## Survive terminal close and reboot

With a permanent hostname, autostart is finally safe — a restarting ngrok
reconnects to the **same** domain. Under a rotating quick tunnel it was actively
harmful: every launchd `KeepAlive` restart handed you a new hostname and silently
broke the uploaded plugin. Neither the server nor the tunnel LaunchAgent is
installed yet (`~/Library/LaunchAgents` has no `com.example.doordash-mcp-bridge.plist`),
and the shipped plist still has `REPLACE_WITH_YOUR_EXISTING_TOKEN` at line 34.


```bash
cp ~/.claude/mcp-servers/doordash-mcp-bridge/com.example.doordash-mcp-bridge.plist ~/Library/LaunchAgents/
chmod 600 ~/Library/LaunchAgents/com.example.doordash-mcp-bridge.plist   # holds the token
launchctl load ~/Library/LaunchAgents/com.example.doordash-mcp-bridge.plist
launchctl kickstart -k gui/$(id -u)/com.example.doordash-mcp-bridge      # to reload after edits
```

Put your **existing** token in the plist first. Remaining gap: a sleeping Mac
means an unreachable bridge — no configuration fixes that.

## What the bridge deliberately cannot do

- **`order submit`** is absent — the only command that charges a card. A startup
  assertion (`FORBIDDEN_ARGV`) refuses to boot if a tool ever maps to it.
  Checkout ends at `dd_order_checkout_url`, a URL a human opens to pay.
- **`address list` / `payment-method list`** are absent — they'd put a home
  address and card metadata into a Slack thread and into Claude Tag channel
  memory, which persists. `dd_address_find` / `dd_address_add` *are* exposed, for
  an address the requester supplies; the consequence is that the bridge cannot
  check for an existing duplicate before saving, and dd-cli does not dedupe. A
  failed `dd_address_add` should be checked in the app, not retried blindly.
- **Order history** is capped to the current local day.
- Responses are stripped, **recursively**, of `widget_type`,
  `assistant_instructions` (which would tell Claude to go silent and defer to a
  nonexistent widget), `delivery_address`, `address_id`, `session_id`, and
  `trace_id`.

Before adding any tool that spends money, re-read `LICENSE.txt` §5.3 and §7.2:
DoorDash may not provide a human confirmation step, and the account owner is
liable regardless of whether they reviewed the transaction. Anyone in a Claude
Tag channel can steer a running session.
