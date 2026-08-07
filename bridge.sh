#!/usr/bin/env bash
# bridge.sh — start, stop, inspect, and diagnose the dd-cli MCP bridge.
#
#   ./bridge.sh up        start the MCP server + tunnel, sync the plugin, report the URL
#   ./bridge.sh status    what's running, which version, which hostname
#   ./bridge.sh verify    run the local diagnostic ladder (health, auth, tool list)
#   ./bridge.sh logs      tail both logs
#   ./bridge.sh down      stop both
#   ./bridge.sh token     print the bearer token (for the Claude Tag credential)
#   ./bridge.sh host <h>  record the hostname of a tunnel started outside this script
#
# See RUNBOOK.md for the Claude Tag debugging decision tree.
#
# Two things this exists to prevent:
#   1. `pkill -f` does NOT match the server — its argv is just `python3 server.py`.
#      A stale instance keeps the port and keeps answering with OLD code while a
#      restart dies on EADDRINUSE. Everything here kills by PID via lsof.
#   2. A quick tunnel gets a NEW random hostname on every start, which silently
#      invalidates both the plugin's .mcp.json and the Claude Tag credential's
#      allowed-websites entry. `up` rewrites .mcp.json and tells you when the
#      credential needs editing.
set -uo pipefail

PORT="${DD_BRIDGE_PORT:-8787}"
BRIDGE_DIR="${DD_BRIDGE_DIR:-$HOME/.claude/mcp-servers/doordash-mcp-bridge}"
SERVER="$BRIDGE_DIR/server.py"
PLUGIN_DIR="$BRIDGE_DIR/doordash-marketplace/plugins/doordash"
MCP_JSON="$PLUGIN_DIR/.mcp.json"

STATE_DIR="${DD_BRIDGE_STATE:-$HOME/.config/dd-bridge}"
TOKEN_FILE="$STATE_DIR/token"
HOST_FILE="$STATE_DIR/hostname"
# Default search coordinates live in the private state dir, not in this file —
# a delivery address doesn't belong in a script that might get shared.
LOCATION_FILE="$STATE_DIR/location"
SERVER_LOG="$STATE_DIR/server.log"
TUNNEL_LOG="$STATE_DIR/tunnel.log"

PYTHON="${DD_BRIDGE_PYTHON:-$(command -v python3 || echo /usr/bin/python3)}"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn()  { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
fail()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
info()  { printf '        %s\n' "$*"; }

# --- process helpers -------------------------------------------------------

server_pid() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1; }
tunnel_pid() { pgrep -f "cloudflared tunnel" 2>/dev/null | head -1; }

# The tunnel hostname, from the cloudflared log (quick tunnel) or config.yml
# (named tunnel). Named tunnels are stable; quick ones are not.
tunnel_host() {
    local cfg="$HOME/.cloudflared/config.yml"
    if [ -f "$cfg" ] && grep -qE '^\s*-?\s*hostname:' "$cfg" 2>/dev/null; then
        grep -E '^[[:space:]]*-?[[:space:]]*hostname:' "$cfg" | head -1 \
            | sed -e 's/.*hostname:[[:space:]]*//' -e 's/[»"'"'"']//g' -e 's/[[:space:]]*$//'
        return
    fi
    if [ -f "$TUNNEL_LOG" ]; then
        local from_log
        from_log="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | tail -1 | sed 's|https://||')"
        if [ -n "$from_log" ]; then printf '%s' "$from_log"; return; fi
    fi
    # A tunnel started outside this script logs to its own terminal, so fall
    # back to whatever was last recorded (`./bridge.sh host <hostname>`) or an
    # explicit override.
    if [ -n "${DD_BRIDGE_HOST:-}" ]; then printf '%s' "$DD_BRIDGE_HOST"; return; fi
    [ -s "$HOST_FILE" ] && cat "$HOST_FILE"
}

ensure_token() {
    mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR" 2>/dev/null
    if [ -n "${DD_BRIDGE_TOKEN:-}" ]; then
        # An env token wins, but persist it so later restarts reuse the same
        # value — a changed token silently breaks the Claude Tag credential.
        printf '%s' "$DD_BRIDGE_TOKEN" > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
    elif [ ! -s "$TOKEN_FILE" ]; then
        openssl rand -hex 32 > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
        warn "generated a NEW token at $TOKEN_FILE"
        info "update the Claude Tag Bearer credential to match, or every call 401s"
    fi
    DD_BRIDGE_TOKEN="$(cat "$TOKEN_FILE")"
    export DD_BRIDGE_TOKEN
}

# --- subcommands -----------------------------------------------------------

cmd_up() {
    [ -f "$SERVER" ] || { fail "no server.py at $SERVER"; exit 1; }
    bold "1. MCP server (port $PORT)"
    local pid; pid="$(server_pid)"
    if [ -n "$pid" ]; then
        ok "already running (PID $pid) — not restarting"
        info "to load code changes: ./bridge.sh down && ./bridge.sh up"
        # Do NOT mint a token here. A running server's token lives only in its
        # own environment; minting one would persist a value it rejects, and the
        # next `down && up` would start a server the Claude Tag credential can't
        # authenticate to.
        if [ -z "${DD_BRIDGE_TOKEN:-}" ] && [ ! -s "$TOKEN_FILE" ]; then
            warn "no token on disk, and the running server's token isn't readable"
            info "seed it so a later restart reuses the same value:"
            info "  DD_BRIDGE_TOKEN=<value in the Claude Tag credential> ./bridge.sh up"
        fi
    else
        ensure_token
        # Without DD_LAT/DD_LNG, dd-cli silently falls back to a Cupertino
        # default and returns zero results with needs_address: true — which
        # reads as "search is broken" rather than "no location configured".
        if [ -s "$LOCATION_FILE" ]; then
            # shellcheck disable=SC1090
            . "$LOCATION_FILE"; export DD_LAT DD_LNG
            info "default location: $DD_LAT, $DD_LNG"
        elif [ -z "${DD_LAT:-}" ]; then
            warn "no default location set — searches without explicit coordinates"
            info "will return 0 results. Write one to $LOCATION_FILE:"
            info "  DD_LAT=37.7749"
            info "  DD_LNG=-122.4194"
        fi
        # nohup so it outlives this shell
        ( cd "$BRIDGE_DIR" && nohup "$PYTHON" "$SERVER" --port "$PORT" \
            >> "$SERVER_LOG" 2>&1 & )
        sleep 2
        pid="$(server_pid)"
        [ -n "$pid" ] && ok "started (PID $pid)" || { fail "did not start — see $SERVER_LOG"; tail -5 "$SERVER_LOG"; exit 1; }
    fi

    bold "2. Cloudflare tunnel"
    local tpid; tpid="$(tunnel_pid)"
    if [ -n "$tpid" ]; then
        ok "already running (PID $tpid)"
    elif ! command -v cloudflared >/dev/null 2>&1; then
        fail "cloudflared not installed — brew install cloudflared"; exit 1
    else
        local cfg="$HOME/.cloudflared/config.yml"
        if [ -f "$cfg" ] && grep -qE '^\s*tunnel:' "$cfg"; then
            nohup cloudflared tunnel run >> "$TUNNEL_LOG" 2>&1 &
            ok "started NAMED tunnel (stable hostname)"
        else
            : > "$TUNNEL_LOG"   # clear so hostname parsing finds the new one
            # 127.0.0.1, never "localhost": localhost resolves to ::1 first and
            # the server binds IPv4 only, so every request would burn a refused
            # IPv6 connection before falling back. When that fallback doesn't
            # happen you get 502s that look like intermittent flakiness.
            nohup cloudflared tunnel --url "http://127.0.0.1:$PORT" >> "$TUNNEL_LOG" 2>&1 &
            warn "started QUICK tunnel — hostname changes on every restart"
            info "see RUNBOOK.md 'Make the hostname stable' to stop re-editing config"
        fi
        # wait for the hostname to appear
        for _ in $(seq 1 20); do [ -n "$(tunnel_host)" ] && break; sleep 1; done
    fi

    local host; host="$(tunnel_host)"
    [ -n "$host" ] || { fail "no tunnel hostname yet — see $TUNNEL_LOG"; exit 1; }
    ok "hostname: $host"

    bold "3. Health through the tunnel"
    local body="" code=""
    for _ in $(seq 1 20); do
        body="$(curl -s -m 10 "https://$host/health" 2>/dev/null)"
        code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$host/health" 2>/dev/null)"
        [ "$code" = "200" ] && break
        sleep 2
    done
    if [ "$code" = "200" ]; then
        ok "$body"
    else
        fail "tunnel not serving yet (http=$code) — retry ./bridge.sh status in a few seconds"
    fi

    bold "4. Plugin config"
    # Change detection keys off whether .mcp.json actually moved, NOT off the
    # recorded hostname — `./bridge.sh host` overwrites that file, which would
    # otherwise make a real hostname change look like a no-op and suppress the
    # re-upload / credential-edit instructions.
    local url_changed=0
    printf '%s' "$host" > "$HOST_FILE"
    if [ -f "$MCP_JSON" ]; then
        local mcp_out
        mcp_out="$("$PYTHON" - "$MCP_JSON" "https://$host/mcp" <<'PY'
import json, sys
path, url = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
srv = cfg.setdefault("mcpServers", {}).setdefault("doordash", {"type": "http"})
changed = srv.get("url") != url
srv["url"] = url
json.dump(cfg, open(path, "w"), indent=2); open(path, "a").write("\n")
print(f"        .mcp.json {'UPDATED -> ' + url if changed else 'already correct'}")
PY
)"
        printf '%s\n' "$mcp_out"
        case "$mcp_out" in *UPDATED*) url_changed=1 ;; esac
        ( cd "$PLUGIN_DIR" && rm -f "$BRIDGE_DIR/doordash-plugin.zip" \
          && zip -rq "$BRIDGE_DIR/doordash-plugin.zip" . -x '.DS_Store' ) \
          && ok "repackaged doordash-plugin.zip"
    else
        warn "no .mcp.json at $MCP_JSON — skipping"
    fi

    bold "Next steps"
    if [ "$url_changed" = "1" ]; then
        warn "MCP URL CHANGED -> https://$host/mcp"
        info "1. re-upload $BRIDGE_DIR/doordash-plugin.zip in Claude Tag > Plugins"
        info "2. edit the Bearer credential's Allowed websites to: $host"
        info "3. start a NEW Slack thread (running threads keep their old tool set)"
    else
        info "hostname unchanged — just start a NEW Slack thread to pick up any changes"
    fi
    info "bearer token: ./bridge.sh token"
}

cmd_status() {
    bold "MCP server"
    local pid; pid="$(server_pid)"
    if [ -n "$pid" ]; then ok "listening on $PORT (PID $pid)"; else fail "nothing listening on $PORT"; fi
    if [ -f "$SERVER" ]; then info "on-disk version: $(grep -m1 'SERVER_VERSION = ' "$SERVER" | cut -d'"' -f2)"; fi

    bold "Tunnel"
    local tpid host; tpid="$(tunnel_pid)"; host="$(tunnel_host)"
    if [ -n "$tpid" ]; then ok "cloudflared running (PID $tpid)"; else fail "cloudflared not running"; fi
    [ -n "$host" ] && info "hostname: $host" || warn "no hostname found"

    if [ -n "$host" ]; then
        bold "Live /health"
        local body; body="$(curl -s -m 10 "https://$host/health")"
        if [ -n "$body" ]; then
            ok "$body"
            # The version served is the canary for "did my restart actually take".
            local live disk
            live="$(printf '%s' "$body" | "$PYTHON" -c 'import json,sys;print(json.load(sys.stdin).get("version","?"))' 2>/dev/null)"
            disk="$(grep -m1 'SERVER_VERSION = ' "$SERVER" | cut -d'"' -f2)"
            if [ "$live" != "$disk" ]; then
                fail "SERVING v$live BUT DISK IS v$disk — stale process, restart it"
                info "./bridge.sh down && ./bridge.sh up"
            fi
        else
            fail "no response through the tunnel"
        fi
    fi

    bold "Claude Tag checklist (verify by hand)"
    info "[ ] plugin uploaded AND toggled on in the Access bundle"
    info "[ ] Bearer credential exists, Allowed websites = ${host:-<hostname>}"
    info "[ ] credential has no path/method restriction blocking POST /mcp"
    info "[ ] testing in a NEW thread, not an existing one"
}

# Read the token without ever creating one. Minting a token here would mismatch
# both the running server and the Claude Tag credential, turning a diagnostic
# into a new outage.
require_token() {
    if [ -n "${DD_BRIDGE_TOKEN:-}" ]; then export DD_BRIDGE_TOKEN; return; fi
    if [ -s "$TOKEN_FILE" ]; then DD_BRIDGE_TOKEN="$(cat "$TOKEN_FILE")"; export DD_BRIDGE_TOKEN; return; fi
    fail "no token available (checked \$DD_BRIDGE_TOKEN and $TOKEN_FILE)"
    info "if a server is already running, its token lives only in that process's"
    info "environment. Seed it once so every later command reuses it:"
    info "  DD_BRIDGE_TOKEN=<the value in the Claude Tag credential> ./bridge.sh up"
    exit 1
}

cmd_verify() {
    require_token
    local host; host="$(tunnel_host)"
    [ -n "$host" ] || { fail "no tunnel hostname — run ./bridge.sh up"; exit 1; }
    local url="https://$host"

    bold "1. /health (unauthenticated — tests egress only)"
    local code; code="$(curl -s -m 10 -o /tmp/.bh -w '%{http_code}' "$url/health")"
    [ "$code" = "200" ] && ok "$(cat /tmp/.bh)" || fail "http=$code"

    bold "2. POST /mcp with NO token (must be 401)"
    code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST "$url/mcp" \
        -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"
    [ "$code" = "401" ] && ok "401 — auth is enforced" || fail "expected 401, got $code"

    bold "3. POST /mcp WITH token (must be 200 + tool list)"
    code="$(curl -s -m 30 -o /tmp/.btools -w '%{http_code}' -X POST "$url/mcp" \
        -H "Authorization: Bearer $DD_BRIDGE_TOKEN" -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"
    if [ "$code" = "401" ]; then
        fail "401 — the token does not match the running server"
        info "the server was started with a different DD_BRIDGE_TOKEN; restart it with"
        info "the value the Claude Tag credential holds, or update both to match"
    elif [ "$code" != "200" ]; then
        fail "http=$code"; head -c 200 /tmp/.btools
    else
        "$PYTHON" - /tmp/.btools <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
tools = (d.get("result") or {}).get("tools") or []
if not tools:
    print("  FAIL ", json.dumps(d)[:200]); raise SystemExit(1)
names = [t["name"] for t in tools]
print("  ok    %d tools" % len(tools))
print("        " + ", ".join(names[:6]) + " ...")
banned = [n for n in names if "submit" in n]
print("  %s  no submit tool exposed" % ("ok  " if not banned else "FAIL"), banned or "")
add = [t for t in tools if t["name"] == "dd_cart_add_items"]
if add:
    documented = "nested_options" in add[0]["description"]
    print("  %s  dd_cart_add_items documents nested_options" % ("ok  " if documented else "FAIL"))
PY
    fi

    bold "4. Same-day scope on order history"
    read -r -d '' HIST_REQ <<'JSON'
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"dd_order_history",
 "arguments":{"max":10,"intent":"Summary: bridge self-test for the account owner\nuser prompt/purpose: \"bridge.sh verify\""}}}
JSON
    code="$(curl -s -m 90 -o /tmp/.bhist -w '%{http_code}' -X POST "$url/mcp" \
        -H "Authorization: Bearer $DD_BRIDGE_TOKEN" -H 'Content-Type: application/json' \
        -d "$HIST_REQ")"
    if [ "$code" != "200" ]; then
        warn "skipped (http=$code)"
    else
        "$PYTHON" - /tmp/.bhist <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
try:
    b = json.loads(d["result"]["content"][0]["text"])
except Exception:
    print("  FAIL ", json.dumps(d)[:200]); raise SystemExit
r = b.get("result") or {}
orders = r.get("orders") or []
note = r.get("bridge_scope_note") or "NO SCOPE NOTE — same-day filter may not be active"
print("  ok    orders today: %d" % len(orders))
print("        " + note[:120])
PY
    fi
    rm -f /tmp/.bh /tmp/.btools /tmp/.bhist
}

cmd_logs() {
    bold "server log ($SERVER_LOG) — 'auth=' lines show credential injection"
    [ -f "$SERVER_LOG" ] && tail -25 "$SERVER_LOG" || info "(none yet)"
    bold "tunnel log ($TUNNEL_LOG)"
    [ -f "$TUNNEL_LOG" ] && tail -12 "$TUNNEL_LOG" || info "(none yet)"
}

cmd_down() {
    local pid tpid
    pid="$(server_pid)"; tpid="$(tunnel_pid)"
    if [ -n "$pid" ]; then kill "$pid" && ok "stopped MCP server (PID $pid)"; else info "server not running"; fi
    if [ -n "$tpid" ]; then kill "$tpid" && ok "stopped tunnel (PID $tpid)"; else info "tunnel not running"; fi
    sleep 1
    [ -z "$(server_pid)" ] || warn "port $PORT still held — check: lsof -nP -iTCP:$PORT -sTCP:LISTEN"
}

# Read-only, like verify. Minting here would hand back a token the running
# server rejects, and a later `up` would then start a server the Claude Tag
# credential can't authenticate to. Only `up` creates tokens.
cmd_token() { require_token; printf '%s\n' "$DD_BRIDGE_TOKEN"; }

# Record the hostname of a tunnel started outside this script (its log went to
# that terminal, so nothing here can discover it).
cmd_host() {
    if [ -z "${2:-}" ]; then
        local h; h="$(tunnel_host)"
        [ -n "$h" ] && printf '%s\n' "$h" || { fail "no hostname known — ./bridge.sh host <hostname>"; exit 1; }
        return
    fi
    mkdir -p "$STATE_DIR"
    printf '%s' "${2#https://}" > "$HOST_FILE"
    ok "recorded hostname: $(cat "$HOST_FILE")"
}

case "${1:-status}" in
    up)     cmd_up ;;
    status) cmd_status ;;
    verify) cmd_verify ;;
    logs)   cmd_logs ;;
    down)   cmd_down ;;
    token)  cmd_token ;;
    host)   cmd_host "$@" ;;
    *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
