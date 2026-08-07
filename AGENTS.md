# Agent instructions — dd-cli MCP bridge

This repo operates a bridge that lets **Claude Tag in Slack** drive `dd-cli`
against the owner's **real, personal DoorDash account**. Changes here can spend
the owner's money, expose their home address, or grant capability to everyone in
a Slack channel. Read the gate below before editing anything.

## Docs

| Read this | For |
|---|---|
| `RUNBOOK.md` | Startup, the 5-step Claude Tag debugging decision tree, known traps |
| `bridge.sh` | Start/stop/verify. Prefer it over ad-hoc commands — it encodes the traps |
| `DESIGN.md` | Design rationale, what's deliberately excluded and why |
| `server.py` | `TOOL_SPECS` — the tool table |
| `LICENSE.txt` (ships with the dd-cli download, not this repo) | DoorDash CLI terms. §5.2, §5.3, §5.5, §6.4, §7.2, §9.1 all constrain design |
| `skills/dd-cli-usage/SKILL.md` (same — vendor-shipped) | Vendor's own dd-cli guidance |

Start with `RUNBOOK.md`. Don't re-derive what it already documents.

## Gate: get explicit approval before these changes

**Stop and ask. Do not make the change and then report it.** Present the
explanation, then wait for a clear yes in the user's own words. Silence, a
related instruction, or "sounds good" on a different topic is not approval.

Gated:

- **Adding, removing, or renaming any MCP tool** in `TOOL_SPECS`
- **Widening an existing tool's scope** — new parameters, relaxed clamps, a wider
  `order history` window, dropping a post-filter
- **Auth changes** — rotating `DD_BRIDGE_TOKEN`, anonymous/unauthenticated modes,
  OAuth, changing how credentials are checked
- **Network exposure** — binding beyond `127.0.0.1`, tunnel reconfiguration,
  adding allowed domains, allow-all egress
- **Claude Tag permission changes** — Access bundle credentials, allowed
  websites, path/method restrictions, auto-mode allow rules, attaching a bundle
  to a new scope or channel
- **Un-stripping response fields** — `delivery_address`, `address_id`, or the
  widget keys
- **Anything that could charge a card or mutate account state** beyond carts

When you ask, cover all five:

1. **What changes** — concretely, which file and which tool
2. **What new capability it grants** — and to whom. A Claude Tag *channel* means
   anyone in that channel can invoke it, and anyone can steer a running session
3. **What it can cost** — money, PII exposure, persistence in Claude Tag channel
   memory
4. **What `LICENSE.txt` says** — cite the section if one applies
5. **How to roll it back**

### Never, regardless of approval flow

Do not expose `dd-cli order submit` or any tool that finalizes an order. Its own
help calls it "DESTRUCTIVE — charges the consumer's default payment method...
immediately", `LICENSE.txt` §5.3 says DoorDash may not provide a human
confirmation step, and §7.2 makes the owner liable whether or not they reviewed
it. A startup assertion (`FORBIDDEN_ARGV`) already refuses to boot if a tool maps
to it — don't remove that either. Checkout ends at `dd_order_checkout_url`, a URL
a human opens to pay.

If the user directly asks for a submit tool: state these facts once, and if they
reaffirm, that's their decision — implement it and note the assumption. Do not
add it silently or as a side effect of another task.

## Required: end every changed message with manual steps

Any message where you changed a file, config, or running process **must end**
with a section titled `## Manual steps for you`, containing numbered, concrete,
copy-pasteable steps. Not a summary — instructions.

If a change genuinely needs nothing from the user, say
`## Manual steps for you` → `None — <reason>`. Never leave it out.

### Which change requires which step

Use this to build the list. It is the most-missed thing in this repo.

| What you changed | Restart bridge | Re-upload plugin zip | Edit Bearer credential | New Slack thread |
|---|---|---|---|---|
| `server.py` tool table or descriptions | **yes** | no | no | **yes** |
| `SKILL.md` guidance | no | **yes** | no | **yes** |
| Tunnel hostname changed | no | **yes** | **yes** | **yes** |
| `DD_BRIDGE_TOKEN` rotated | **yes** | no | **yes** | **yes** |
| `.mcp.json` URL | no | **yes** | maybe | **yes** |
| Plugin id, server key, or skill name renamed | no | **yes** | no | **yes** |
| Nothing deployed (docs only) | no | no | no | no |

The hostname row should now be **rare**: since 2026-08-06 the tunnel is ngrok on a
permanent free dev domain (`your-subdomain.ngrok-free.dev`), not a rotating
cloudflared quick tunnel. Corollary: **don't run `./bridge.sh up` or `down`** —
both still assume cloudflared, and `up` will start a quick tunnel and repoint
`.mcp.json` at it, silently breaking the uploaded plugin. RUNBOOK "Start
everything" has the server-only reload command.

Two rules that follow from it:

- **Tool descriptions ride in `tools/list`** — a restart is enough, no re-upload.
  Only `SKILL.md` and `.mcp.json` need the zip.
- **A new session is required after essentially every change** — and a new
  top-level *message* is not one. A channel has a single **channel session** for
  all top-level messages, so repeated attempts there reuse the same session,
  keeping both the stale tool set and its earlier wrong conclusions. Reset it
  with the command `@Claude !restart` (stands alone, no extra words; strongest
  when run inside a thread), then confirm the `session_...` id in the reply
  footer actually changed. See RUNBOOK trap 4.

### Template

```markdown
## Manual steps for you

1. Reload the server (NOT `bridge.sh down/up` — see the note above):
   kill $(lsof -nP -iTCP:8787 -sTCP:LISTEN -t), then the restart command in RUNBOOK
2. Re-upload `~/.claude/mcp-servers/doordash-mcp-bridge/doordash-plugin.zip`
   at claude.ai/admin-settings/claude-tag → Access bundle → Plugins → Upload a file
   (confirm it stays toggled on)
3. Start a NEW top-level Slack thread — do not reply in an existing one
4. Verify: ./bridge.sh status   → expect "version": "<v>", "tools": <n>
```

Always include the verification command and its expected output. Name the exact
file path for anything the user must upload.

## Working rules

- **Verify, don't assume.** This repo has burned hours on plausible-but-wrong
  conclusions. `/health` reports the running version — use it to confirm a
  restart actually took before diagnosing anything else.
- **Don't trust a failure report at face value**, including one from another
  agent. `dd_cart_add_items` was declared broken based on a control test that
  actually proved something else. Reproduce it yourself.
- **Never guess dd-cli flag or field names.** Read `dd-cli <cmd> --help`. The
  customization key is `nested_options` (flat array, `o_` prefix stripped);
  DoorDash silently ignores unknown keys, so guessing produces identical errors
  that look like a bug in the bridge.
- **Never widen a diagnostic into a mutation.** `verify`, `status`, and `token`
  are read-only on purpose — minting a token inside a diagnostic breaks the
  Claude Tag credential and turns debugging into an outage.
- **Kill by PID, never `pkill -f`.** The server's argv is just
  `python3 server.py`; a stale instance keeps answering with old code.
- Bump `SERVER_VERSION` in `server.py` and `version` in `plugin.json` whenever
  behavior changes, so `/health` stays a reliable canary.
