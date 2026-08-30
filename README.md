# doordash-mcp-bridge

A small MCP server that exposes a **read-and-cart-only** subset of DoorDash's
`dd-cli` to Claude — so you can search restaurants, browse menus, price a cart,
and get a browser checkout URL from a chat client.

It runs on your own Mac, against your own DoorDash account, for your own orders.

> **Not affiliated with, endorsed by, or sponsored by DoorDash.** "DoorDash" is a
> trademark of its owner. This is an unofficial personal tool built on top of the
> vendor's own CLI.

## What it deliberately cannot do

This is the important part of the design, not a limitation to fix later:

- **It cannot place an order.** `dd-cli order submit` is not exposed, and a startup
  assertion (`FORBIDDEN_ARGV`) refuses to boot if any tool ever maps to it.
  Checkout ends at `dd_order_checkout_url` — a URL a human opens and pays on.
- **No saved-address or payment listing.** `address list` and `payment-method list`
  are absent, so a home address and card metadata never enter a chat transcript.
  You *can* add a new address (`dd_address_find` → `dd_address_add`), because that
  address comes from the requester in the conversation — but the bridge still
  can't read back what's already on the account, so it can't detect duplicates.
- **Order history is capped to the current local day.**
- **Responses are stripped** of `delivery_address`, `address_id`, `session_id`,
  `trace_id`, and the widget/`assistant_instructions` keys that would otherwise
  tell a model to go quiet and defer to a UI that doesn't exist. Stripping is
  recursive — dd-cli 0.2.3 nests `order status` under `result`, and a
  top-level-only filter missed it.

## Prerequisites

**`dd-cli` is not included in this repo, and can't be.** DoorDash's CLI terms
grant a *"personal, revocable, non-exclusive, non-transferable, non-sublicensable"*
license (§11.1), so redistributing the binary isn't permitted. Download it
yourself from DoorDash, accept their terms, and run `dd-cli login` once.

- macOS on Apple silicon — `dd-cli` ships `darwin-arm64` only, and authenticates
  through a localhost browser redirect into the macOS keychain. That's why this
  bridge exists at all: the CLI can't run in a Linux sandbox, a serverless
  isolate, or a linux/amd64 container.
- Python 3.9+ (3.13+ recommended)
- A tunnel, if you want a remote client to reach it — ngrok's free dev domain or a
  Cloudflare named tunnel both work

## Layout

```
server.py                       # the MCP server — the tool table lives in TOOL_SPECS
bridge.sh                       # start/stop/verify; encodes the operational traps
doordash-marketplace/           # Claude Code / Claude Tag plugin, as a marketplace
  .claude-plugin/marketplace.json
  plugins/doordash/
    .claude-plugin/plugin.json
    .mcp.json                   # points at your tunnel host
launchd/                        # example LaunchAgent, to survive reboot
RUNBOOK.md                      # startup, debugging decision tree, known traps
AGENTS.md                       # rules for AI agents working on this repo
DESIGN.md                       # why it's shaped this way; what's excluded and why
```

## Quickstart

```bash
git clone <this repo> ~/.claude/mcp-servers/doordash-mcp-bridge
cd ~/.claude/mcp-servers/doordash-mcp-bridge
DD_BRIDGE_TOKEN=$(openssl rand -hex 32) ./bridge.sh up
./bridge.sh verify        # expect 4 green: 401 enforced, tools listed, no submit tool
```

Then expose it and point the plugin at it:

```bash
ngrok http http://127.0.0.1:8787 --url=https://your-subdomain.ngrok-free.dev
./bridge.sh host your-subdomain.ngrok-free.dev
```

Use `http://127.0.0.1:8787`, never a bare port or `localhost` — the server binds
IPv4 only, and `localhost` resolves to `::1` first.

### Placeholders to replace

Everything below ships as an obvious placeholder. Nothing here is a real value.

| Placeholder | Where | Replace with |
|---|---|---|
| `your-subdomain.ngrok-free.dev` | `.mcp.json`, `RUNBOOK.md`, `AGENTS.md`, `DESIGN.md` | your tunnel hostname |
| `37.7749` / `-122.4194` | `bridge.sh`, `launchd/*.plist`, `RUNBOOK.md` | your default search coordinates |
| `/Users/YOUR_USERNAME` | `launchd/*.plist` | your home directory (launchd needs literal paths) |
| `com.example.doordash-mcp-bridge` | `launchd/*.plist` | your own reverse-DNS label |
| `REPLACE_WITH_YOUR_EXISTING_TOKEN` | `launchd/*.plist` | your `DD_BRIDGE_TOKEN` — **never commit this** |

## Security model

- The server binds **`127.0.0.1`** and every request must carry
  `Authorization: Bearer $DD_BRIDGE_TOKEN`. Put a tunnel in front of it rather
  than binding a public interface.
- `/health` is unauthenticated on purpose, so you can test egress without a token.
  It reveals nothing about your account.
- **The token never goes in `.mcp.json`.** With Claude Tag, the Agent Proxy
  attaches it from a Bearer credential, so it doesn't sit in the plugin or in the
  sandbox. Keep it that way.
- The token lives in `~/.config/dd-bridge/token` (mode 600), outside this repo.
- Every request logs an `auth=` classification — `ok`, `ok-bare`, `absent`,
  `bearer-mismatch`, `wrong-scheme:<scheme>`. A credential that **matches** is
  never echoed. A rejected one is truncated to 16 characters in the
  `wrong-scheme:` label, enough to tell two wrong values apart without writing a
  working one to disk. That line is the fastest way to diagnose a 401.
- `ok-bare` means the token arrived with no `Bearer ` scheme and was accepted
  anyway. Several Claude clients drop the prefix and it can't be fixed from their
  side, so the server tolerates it — see `_auth_state()` in `server.py`. The
  comparison is unchanged; only the framing is relaxed.

**Never commit a credential.** DoorDash's terms are explicit that credentials may
not be shared, published, or *"embed[ded] in publicly accessible code"* (§10.2).

## Using it responsibly — read before you deploy this

Anyone who can talk to a client wired to this bridge can spend money from the
account it's signed into. In a shared channel, that means everyone in the channel,
and anyone who can steer a running session.

DoorDash's CLI terms shape the design, and they should shape yours:

- **§4.1 Personal, non-commercial use only.** Don't run this as a service.
- **§5.5 Single account.** An agent may act for one account only.
- **§5.1–5.2 Agency and full responsibility.** You own every action your agent
  takes, whether or not you reviewed it.
- **§5.3 No human confirmation step.** DoorDash may not prompt before completing a
  transaction. That's precisely why `order submit` is absent here.
- **§7.2 Charges are yours.** Including ones an agent initiates by mistake.
- **§6.2 No export or portability.** Don't write CLI-accessed data to files,
  databases, or third-party services beyond completing your own order — and don't
  use it to train or evaluate models. Practically, for this repo: **no captured
  responses in issues, fixtures, or tests.** No menu dumps, no pricing snapshots,
  no cart or order JSON.

## Contributing

Read `AGENTS.md` first — it documents an approval gate for changes that widen tool
scope, touch auth, or change network exposure. Two hard rules: never add a tool
that finalizes an order, and never attach real DoorDash response data to a PR.

## License

Add your own license for the bridge code in this repo. It cannot and does not
grant any rights to `dd-cli`, which remains subject to DoorDash's CLI terms.
