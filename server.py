#!/usr/bin/env python3
"""doordash-mcp-bridge — expose a fixed subset of dd-cli as a remote MCP server.

Why this exists: dd-cli is a macOS/arm64 binary that authenticates through a
browser redirect to localhost and stores credentials in the OS keychain. None of
that works inside the ephemeral Linux sandbox a Claude Tag session runs in. So
instead of moving dd-cli to the sandbox, this server keeps dd-cli on the Mac and
lets the sandbox reach it over HTTPS:

    Slack @Claude -> Claude Tag sandbox -> tunnel -> this server -> dd-cli

Every tool is one entry in TOOL_SPECS with a hard-coded dd-cli argv. A caller
supplies option *values*, never option names and never a subcommand, so no
request can reach a command that isn't in the table.

`order submit` is deliberately absent. Its own --help calls it "DESTRUCTIVE —
charges the consumer's default payment method... immediately", and LICENSE.txt
7.2 makes the credential owner liable "regardless of whether you personally
initiated, reviewed, or approved each individual transaction". Checkout goes
through `dd_order_checkout_url`, which returns a browser URL a human completes.
Adding a submit tool means re-reading LICENSE.txt 5.3 and 7.2 first.

Also absent: `address list` and `payment-method list`, which would put a home
address and card metadata into a Slack thread and into Claude Tag's channel
memory, which persists.

Two ways to run it:

  1. HTTP MCP server (default) — what Claude Tag connects to:
       DD_BRIDGE_TOKEN=... python3 server.py --port 8787
  2. MCP stdio server, for local testing with Claude Code:
       python3 server.py --stdio

Auth: every HTTP request must carry `Authorization: Bearer $DD_BRIDGE_TOKEN`.
If DD_BRIDGE_TOKEN is unset the server refuses to start — it never serves open.
"""

import hmac
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "doordash-mcp-bridge"
SERVER_VERSION = "2.1.0"

TOKEN_ENV_VAR = "DD_BRIDGE_TOKEN"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

SUBPROCESS_TIMEOUT = 90  # seconds; cart/preview calls are slower than search
MAX_BODY_BYTES = 256 * 1024  # items-json payloads can be sizeable

# LICENSE.txt 9.1 obliges us to respect DoorDash's rate limits. One dd-cli
# invocation per this many seconds, process-wide.
MIN_SECONDS_BETWEEN_CALLS = 2.0

_throttle_lock = threading.Lock()
_last_call_at = 0.0


def resolve_token() -> str:
    token = os.environ.get(TOKEN_ENV_VAR, "")
    if not token:
        raise RuntimeError(
            f"{TOKEN_ENV_VAR} is not set. Generate one (e.g. `openssl rand -hex 32`) "
            "and set it in the environment before starting the server."
        )
    return token


def resolve_dd_cli() -> str:
    """Absolute path to the dd-cli binary. Explicit path beats PATH lookup."""
    configured = os.environ.get("DD_CLI_PATH")
    if configured:
        if not os.path.isfile(configured) or not os.access(configured, os.X_OK):
            raise RuntimeError(f"DD_CLI_PATH is not an executable file: {configured}")
        return configured
    found = shutil.which("dd-cli") or os.path.expanduser("~/.local/bin/dd-cli")
    if not os.path.isfile(found) or not os.access(found, os.X_OK):
        raise RuntimeError(
            "dd-cli not found. Install it, or set DD_CLI_PATH to the binary."
        )
    return found


# --- response normalization -----------------------------------------------
#
# `dd-cli --json-output` wraps its payload in an MCP-shaped envelope built for a
# GUI client: {content: [...], structuredContent: {...}, isError: bool}, where
# content[0].text duplicates structuredContent verbatim. We forward only
# structuredContent, minus the keys below.

# dd-cli's own --help says agents must ignore these two: they narrate a
# "displayed widget" that does not exist here, and assistant_instructions tells
# the caller "Do NOT output additional text" — which in Slack means Claude goes
# quiet instead of answering. Stripping them keeps text written by the upstream
# service from reaching the model as if it were instruction.
WIDGET_KEYS = ("widget_type", "assistant_instructions")

# Internal handles a caller has no use for.
OPAQUE_KEYS = ("session_id", "trace_id")

# The consumer's saved delivery address rides along in many responses. Anything
# returned here can land in a Slack thread and in Claude Tag's channel/workspace
# memory, which persists — against LICENSE.txt 6.4, which limits retention to
# what the transaction requires. Dropped by default.
ADDRESS_KEYS = ("delivery_address", "address_id")


def normalize_payload(envelope):
    """Pull the useful data out of dd-cli's widget envelope."""
    if not isinstance(envelope, dict):
        return envelope

    data = envelope.get("structuredContent")
    if not isinstance(data, dict):
        # No structuredContent: fall back to the duplicated text blob.
        content = envelope.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            try:
                data = json.loads(content[0].get("text") or "")
            except (json.JSONDecodeError, TypeError):
                return envelope
        if not isinstance(data, dict):
            return envelope

    dropped = list(WIDGET_KEYS) + list(OPAQUE_KEYS)
    if os.environ.get("DD_BRIDGE_INCLUDE_ADDRESS") != "1":
        dropped += list(ADDRESS_KEYS)
    return {k: v for k, v in data.items() if k not in dropped}


# --- response post-filters ------------------------------------------------


def parse_utc_iso(stamp):
    """Parse dd-cli's `2026-08-07T02:41:00.306Z` into an aware datetime.

    `datetime.fromisoformat` only accepts a literal trailing `Z` from Python 3.11
    on, and macOS still ships 3.9 at /usr/bin/python3. Normalizing the suffix
    keeps this working on either, so a launchd job pointed at the system
    interpreter doesn't silently withhold every order.
    """
    if not isinstance(stamp, str) or not stamp:
        raise ValueError("timestamp is not a string")
    text = stamp.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def keep_only_today(payload):
    """Drop every order not placed on today's LOCAL calendar date.

    `order history` offers `--days`, but its smallest window is a rolling 24
    hours, which leaks yesterday. dd-cli returns `order_date` as a UTC ISO
    timestamp, so the comparison has to happen in local time: an order placed at
    7:41pm PDT is stamped 02:41Z the *following* day, and a naive UTC date
    comparison would file every evening order under tomorrow.

    Fails closed — an order whose timestamp can't be parsed is withheld.
    """
    if not isinstance(payload, dict):
        return payload
    orders = payload.get("orders")
    if not isinstance(orders, list):
        return payload

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    kept, withheld = [], 0
    for order in orders:
        stamp = order.get("order_date") if isinstance(order, dict) else None
        try:
            local_date = parse_utc_iso(stamp).astimezone().strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            withheld += 1
            continue
        if local_date == today:
            kept.append(order)
        else:
            withheld += 1

    result = dict(payload)
    result["orders"] = kept
    # Never truncate silently: say what was dropped so an empty list doesn't
    # read as "you have never ordered anything".
    result["bridge_scope_note"] = (
        f"This bridge exposes only orders placed today ({today}, local time). "
        f"{withheld} older order(s) were withheld. Older history is not "
        f"retrievable through this bridge at all."
    )
    return result


# --- parameter coercion ---------------------------------------------------
#
# Every function below runs on caller-supplied JSON. Values are range-checked
# and type-checked before they reach argv.


def coerce_text(value, field: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters")
    return text


def coerce_int(value, field: str, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer")
    if not lo <= number <= hi:
        raise ValueError(f"{field} must be between {lo} and {hi}")
    return number


def coerce_enum(value, field: str, choices) -> str:
    if value not in choices:
        raise ValueError(f"{field} must be one of: {', '.join(choices)}")
    return value


def coerce_json_array(value, field: str, max_len: int) -> str:
    """Accept a JSON array as a real list or as a pre-serialized string."""
    if isinstance(value, (list, tuple)):
        parsed = list(value)
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be valid JSON: {exc.msg}")
    else:
        raise ValueError(f"{field} must be a JSON array")
    if not isinstance(parsed, list):
        raise ValueError(f"{field} must be a JSON array, not {type(parsed).__name__}")
    if not parsed:
        raise ValueError(f"{field} must not be an empty array")
    encoded = json.dumps(parsed, separators=(",", ":"))
    if len(encoded) > max_len:
        raise ValueError(f"{field} is too large ({len(encoded)} > {max_len} bytes)")
    return encoded


def coerce_text_list(value, field: str, max_len: int, max_items: int):
    """A repeatable flag: one value or a list of them."""
    values = value if isinstance(value, (list, tuple)) else [value]
    if len(values) > max_items:
        raise ValueError(f"{field} accepts at most {max_items} values")
    return [coerce_text(v, field, max_len) for v in values]


def coerce_coord(value, field: str, limit: float):
    """Return a finite in-range float, or None if the caller omitted the field."""
    if value is None or value == "":
        return None
    try:
        coord = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    if not math.isfinite(coord):
        raise ValueError(f"{field} must be a finite number")
    if not -limit <= coord <= limit:
        raise ValueError(f"{field} must be between -{limit} and {limit}")
    return coord


def resolve_location(args: dict):
    """Caller-supplied coords win; fall back to DD_LAT/DD_LNG from the env.

    dd-cli would otherwise silently fall back to a Cupertino default that
    returns zero results with `needs_address: true`, which reads as "search is
    broken". Latitude and longitude are required together so a half-specified
    location can't be paired with a stale env value.
    """
    lat = coerce_coord(args.get("latitude"), "latitude", 90.0)
    lng = coerce_coord(args.get("longitude"), "longitude", 180.0)
    if (lat is None) != (lng is None):
        raise ValueError("latitude and longitude must be supplied together")
    if lat is None:
        lat = coerce_coord(os.environ.get("DD_LAT"), "DD_LAT", 90.0)
        lng = coerce_coord(os.environ.get("DD_LNG"), "DD_LNG", 180.0)
        if (lat is None) != (lng is None):
            raise ValueError("DD_LAT and DD_LNG must both be set, or neither")
    return lat, lng


# --- tool table -----------------------------------------------------------

INTENT_DESC = (
    "Why this call is being made, as two lines:\n"
    "Summary: <who this is for and the goal>\n"
    "user prompt/purpose: \"<the verbatim request that started this>\"\n"
    "Trace this to the request that began the conversation, not the latest "
    "message. DoorDash may review this field. Never include health details, "
    "religion, race, credentials, payment details, information about other "
    "people, or precise location beyond a delivery address."
)

# dd-cli requires --intent on every command, so it is required on every tool.
INTENT_PARAM = {"flag": "--intent", "kind": "text", "max": 1000, "required": True, "desc": INTENT_DESC}

STORE_ID = {"flag": "--store-id", "kind": "text", "max": 64, "required": True,
            "desc": "Numeric store id, from a search or store listing."}
CART_UUID = {"flag": "--cart-uuid", "kind": "text", "max": 128, "required": True,
             "desc": "Cart UUID from dd_cart_list, dd_cart_add_items, or dd_order_reorder."}
ORDER_UUID = {"flag": "--order-uuid", "kind": "text", "max": 128, "required": True,
              "desc": "Order UUID from a past order listing."}
FULFILLMENT = {"flag": "--fulfillment", "kind": "enum", "choices": ["delivery", "pickup"],
               "desc": "Delivery or pickup. Omit to keep the cart's stored mode."}

TOOL_SPECS = [
    # ---- discovery (read-only) ----
    {
        "name": "dd_search",
        "argv": ["search"],
        "writes": False,
        "location": True,
        "description": (
            "Search DoorDash for nearby RESTAURANTS by text query. Returns stores with "
            "store_id, name, distance, delivery time, and rating. Restaurant-focused: "
            "grocery/household queries ('milk', 'cat food') often return nothing even "
            "when the store is on DoorDash — use dd_find_nearby_stores then dd_find_items "
            "for those verticals. Supply latitude/longitude whenever the request implies "
            "a place; the server's fallback location may not be the user's."
        ),
        "params": {
            "query": {"flag": "--query", "kind": "text", "max": 200, "required": True,
                      "desc": "Search text, e.g. 'sushi' or 'thin crust pizza'."},
            "limit": {"flag": "--limit", "kind": "int", "lo": 1, "hi": 25,
                      "desc": "Max restaurants to return (default 5)."},
            "intent": INTENT_PARAM,
        },
    },
    {
        "name": "dd_find_nearby_stores",
        "argv": ["find-nearby-stores"],
        "writes": False,
        "location": True,
        "description": (
            "Discover non-restaurant stores near a location, within roughly 16 miles, by "
            "vertical. This is the entry point for groceries, alcohol, convenience, pet "
            "supplies, and retail — dd_search will not find them. Follow with "
            "dd_find_items to search inside a store; non-restaurant catalogs are too "
            "large to enumerate with dd_menu."
        ),
        "params": {
            "vertical": {"flag": "--vertical", "kind": "enum",
                         "choices": ["grocery", "alcohol", "convenience", "pets", "retail", "nv"],
                         "desc": "Which vertical to search."},
            "max": {"flag": "--max", "kind": "int", "lo": 1, "hi": 25,
                    "desc": "Max stores to return."},
            "intent": INTENT_PARAM,
        },
    },
    {
        "name": "dd_find_items",
        "argv": ["find-items"],
        "writes": False,
        "description": (
            "Search for items inside one retail or grocery store. Prefer this over "
            "dd_menu for non-restaurant verticals. Accepts several queries at once to "
            "resolve a shopping list against a single store."
        ),
        "params": {
            "store_id": {"flag": "--store-id", "kind": "text", "max": 64, "required": True,
                         "desc": "Numeric store id from dd_find_nearby_stores."},
            "queries": {"flag": "--query", "kind": "text_list", "max": 120, "max_items": 25,
                        "required": True,
                        "desc": "One item name, or a list of them (e.g. ['milk','eggs'])."},
            "intent": INTENT_PARAM,
        },
    },
    {
        "name": "dd_menu",
        "argv": ["menu"],
        "writes": False,
        "description": (
            "Show the menu for a restaurant. The response carries a top-level menu_id "
            "and item ids; dd_restaurant_item_details and dd_cart_add_items both need "
            "them. Menu item ids appear with an 'i_' prefix — strip it before reuse."
        ),
        "params": {"store_id": STORE_ID, "intent": INTENT_PARAM},
    },
    {
        "name": "dd_store_details",
        "argv": ["store-details"],
        "writes": False,
        "description": (
            "Business metadata for one store: name, image, hours, and status. Does not "
            "return a live delivery ETA — build a cart and use dd_order_preview for that."
        ),
        "params": {"store_id": STORE_ID, "intent": INTENT_PARAM},
    },
    {
        "name": "dd_restaurant_item_details",
        "argv": ["restaurant-item-details"],
        "writes": False,
        "description": (
            "Full detail for one restaurant menu item: pricing, description, and the "
            "option/customization tree needed to add it to a cart correctly."
        ),
        "params": {
            "store_id": {"flag": "--store-id", "kind": "text", "max": 64, "required": True,
                         "desc": "Numeric restaurant store id."},
            "menu_id": {"flag": "--menu-id", "kind": "text", "max": 64, "required": True,
                        "desc": "menu_id from the dd_menu response for this store."},
            "item_id": {"flag": "--item-id", "kind": "text", "max": 64, "required": True,
                        "desc": "Menu item id, with any 'i_' prefix stripped."},
            "intent": INTENT_PARAM,
        },
    },
    {
        "name": "dd_promo_list",
        "argv": ["promo", "list"],
        "writes": False,
        "description": "List campaign promotions the signed-in account is eligible for at one store.",
        "params": {"store_id": STORE_ID, "intent": INTENT_PARAM},
    },
    {
        "name": "dd_order_history",
        "argv": ["order", "history"],
        # --days 1 is the tightest window dd-cli offers; keep_only_today then
        # trims the rolling-24h remainder down to the actual local date. The
        # caller cannot widen either one.
        "fixed_args": ["--days", "1"],
        "post": keep_only_today,
        "writes": False,
        "description": (
            "List the account's orders placed TODAY, to obtain an order_uuid for "
            "dd_order_status, dd_order_receipt, or dd_order_reorder. Scoped to the "
            "current local calendar day on purpose — earlier history is not retrievable "
            "through this bridge, so if someone asks about last week's order, say it "
            "isn't available here rather than implying they never ordered. An empty list "
            "means nothing was ordered today."
        ),
        "params": {
            "max": {"flag": "--max", "kind": "int", "lo": 1, "hi": 10,
                    "desc": "Max orders to return, 1-10 (default 10)."},
            "intent": INTENT_PARAM,
        },
    },
    {
        "name": "dd_order_status",
        "argv": ["order", "status"],
        "writes": False,
        "description": (
            "Check whether a submitted order actually went through. Use this to confirm "
            "an order placed in the browser via dd_order_checkout_url."
        ),
        "params": {"order_uuid": ORDER_UUID, "intent": INTENT_PARAM},
    },
    {
        "name": "dd_order_receipt",
        "argv": ["order", "receipt"],
        "writes": False,
        "description": "Fetch the itemized receipt for one past order.",
        "params": {"order_uuid": ORDER_UUID, "intent": INTENT_PARAM},
    },

    # ---- cart and pricing (mutate state, never charge) ----
    {
        "name": "dd_cart_list",
        "argv": ["cart", "list"],
        "writes": False,
        "description": (
            "List open (unsubmitted) carts. An account can hold only ONE cart per store, "
            "so check here before starting a new one — if a cart already exists at the "
            "target store, extend it or delete it first rather than silently starting over."
        ),
        "params": {
            "store_id": {"flag": "--store-id", "kind": "text", "max": 64,
                         "desc": "Optional: filter to carts at one store."},
            "intent": INTENT_PARAM,
        },
    },
    {
        "name": "dd_cart_show",
        "argv": ["cart", "show"],
        "writes": False,
        "description": (
            "Show a cart's current line items. No pricing — use dd_order_preview for "
            "totals and fees. Each line's items[].id is the cart_item_id that "
            "dd_cart_remove_item needs, which is NOT the menu item_id."
        ),
        "params": {"cart_uuid": CART_UUID, "intent": INTENT_PARAM},
    },
    {
        "name": "dd_cart_add_items",
        "argv": ["cart", "add-items"],
        "writes": True,
        "description": (
            "Add items to a cart, creating one if cart_uuid is omitted. For restaurants, "
            "menu_id must match the store.\n\n"
            "CUSTOMIZATIONS use the key `nested_options` — a FLAT array of "
            "{id, name, quantity} inside the item. Not `extras`, not `options`, not "
            "`option_ids`, and not a groups-and-options tree. Strip the `o_` prefix from "
            "option ids exactly as you strip `i_` from item ids:\n"
            "  [{\"item_id\":\"1000000001\",\"item_name\":\"Example Iced Tea\",\"quantity\":1,"
            "\"nested_options\":[{\"id\":\"2000000001\",\"name\":\"Example Topping Choice\",\"quantity\":1},"
            "{\"id\":\"2000000002\",\"name\":\"Example Milk Choice\",\"quantity\":1}]}]\n"
            "For combo items whose choice has its own sub-choice, nest a further "
            "`options`: [] array inside that entry.\n\n"
            "Call dd_restaurant_item_details first and satisfy EVERY group with "
            "min_num_options >= 1 (its `extras` list). Missing one fails with "
            "\"Please select at least 1 options for <group>\", naming only the first "
            "unsatisfied group — so fix them all rather than iterating one at a time.\n\n"
            "DoorDash silently IGNORES unrecognized keys, so a wrong key name produces "
            "that same required-group error and echoes your payload back unchanged. "
            "Identical errors across different key names mean the key is wrong; they do "
            "not mean the field was dropped in transit. Do not guess key names — the "
            "only one that works is `nested_options`."
        ),
        "params": {
            "store_id": {"flag": "--store-id", "kind": "text", "max": 64, "required": True,
                         "desc": "Numeric store id the items belong to."},
            "items_json": {"flag": "--items-json", "kind": "json_array", "max": 100000,
                           "required": True,
                           "desc": "JSON array of items to add (item_id, item_name, quantity)."},
            "menu_id": {"flag": "--menu-id", "kind": "text", "max": 64,
                        "desc": "Menu id for this store. Required for restaurants."},
            "cart_uuid": {"flag": "--cart-uuid", "kind": "text", "max": 128,
                          "desc": "Existing cart to add to. Omit to create a new cart."},
            "fulfillment": FULFILLMENT,
            "intent": INTENT_PARAM,
        },
    },
    {
        "name": "dd_cart_remove_item",
        "argv": ["cart", "remove-item"],
        "writes": True,
        "description": (
            "Remove one line item from a cart. cart_item_id is items[].id from "
            "dd_cart_show — NOT the menu item_id. Passing a menu item_id silently "
            "fails to match, so read the cart first."
        ),
        "params": {
            "cart_uuid": CART_UUID,
            "cart_item_id": {"flag": "--cart-item-id", "kind": "text", "max": 128, "required": True,
                             "desc": "Cart-line id from dd_cart_show items[].id."},
            "intent": INTENT_PARAM,
        },
    },
    {
        "name": "dd_cart_delete",
        "argv": ["cart", "delete"],
        "writes": True,
        "description": (
            "Empty a cart and abandon it. Destructive to cart state and not undoable — "
            "confirm with the requester before calling, especially in a shared channel "
            "where the cart may not be theirs."
        ),
        "params": {"cart_uuid": CART_UUID, "intent": INTENT_PARAM},
    },
    {
        "name": "dd_order_reorder",
        "argv": ["order", "reorder"],
        "writes": True,
        "description": (
            "Create a new cart from a past order. Does not place anything — it builds a "
            "cart you can then inspect with dd_cart_show, adjust, and price with "
            "dd_order_preview."
        ),
        "params": {"order_uuid": ORDER_UUID, "intent": INTENT_PARAM},
    },
    {
        "name": "dd_build_grocery_list",
        "argv": ["build-grocery-list"],
        "writes": True,
        "description": (
            "Assemble a multi-store grocery cart from a natural shopping list in one "
            "call. GROCERY AND HOUSEHOLD ONLY — do not route prepared restaurant food "
            "here. STATELESS: every call REPLACES the entire existing list, there is no "
            "append, so always send the complete list or you will wipe items the "
            "requester still wants."
        ),
        "params": {
            "items_json": {"flag": "--items-json", "kind": "json_array", "max": 100000,
                           "required": True,
                           "desc": "Complete JSON array of items, e.g. [{\"name\":\"milk\"}]."},
            "store_id": {"flag": "--store-id", "kind": "text", "max": 64,
                         "desc": "Optional: pin to one store id."},
            "desired_mx_name": {"flag": "--desired-mx-name", "kind": "text", "max": 120,
                                "desc": "Optional: prefer a merchant by name, e.g. 'Whole Foods'."},
            "servings": {"flag": "--servings", "kind": "int", "lo": 1, "hi": 50,
                         "desc": "Optional: recipe servings count."},
            "intent": INTENT_PARAM,
        },
    },
    {
        "name": "dd_promo_apply",
        "argv": ["promo", "apply"],
        "writes": True,
        "description": "Apply a promo code to a cart. Campaign promos also need their campaign/ad ids from dd_promo_list.",
        "params": {
            "cart_uuid": CART_UUID,
            "promo_code": {"flag": "--promo-code", "kind": "text", "max": 64, "required": True,
                           "desc": "Promo code to apply."},
            "campaign_id": {"flag": "--campaign-id", "kind": "text", "max": 128,
                            "desc": "Optional campaign id, for campaign promos."},
            "ad_group_id": {"flag": "--ad-group-id", "kind": "text", "max": 128,
                            "desc": "Optional ad-group id, for campaign promos."},
            "ad_id": {"flag": "--ad-id", "kind": "text", "max": 128,
                      "desc": "Optional ad-placement id, for campaign promos."},
            "intent": INTENT_PARAM,
        },
    },
    {
        "name": "dd_promo_remove",
        "argv": ["promo", "remove"],
        "writes": True,
        "description": "Remove a previously applied promo code from a cart. Pass the same ids used to apply it.",
        "params": {
            "cart_uuid": CART_UUID,
            "promo_code": {"flag": "--promo-code", "kind": "text", "max": 64, "required": True,
                           "desc": "Promo code to remove."},
            "campaign_id": {"flag": "--campaign-id", "kind": "text", "max": 128, "desc": "Optional campaign id."},
            "ad_group_id": {"flag": "--ad-group-id", "kind": "text", "max": 128, "desc": "Optional ad-group id."},
            "ad_id": {"flag": "--ad-id", "kind": "text", "max": 128, "desc": "Optional ad-placement id."},
            "intent": INTENT_PARAM,
        },
    },
    {
        "name": "dd_order_preview",
        "argv": ["order", "preview"],
        "writes": False,
        "description": (
            "Price a cart: subtotal, taxes, fees, credits, and the authoritative live "
            "delivery availability and ETA. Charges nothing. Always run this before "
            "handing anyone a checkout URL, and report the total in the thread."
        ),
        "params": {
            "cart_uuid": CART_UUID,
            "fulfillment": FULFILLMENT,
            "scheduled_time": {"flag": "--scheduled-time", "kind": "text", "max": 64,
                               "desc": "ISO 8601 scheduled delivery time (UTC)."},
            "priority": {"flag": "--priority", "kind": "bool",
                         "desc": "Quote Priority (express) delivery."},
            "include_work_benefits": {"flag": "--include-work-benefits", "kind": "bool",
                                      "desc": "Return eligible work-benefit budgets. Required when the requester mentions a work or company budget."},
            "selected_budget_id": {"flag": "--selected-budget-id", "kind": "text", "max": 128,
                                   "desc": "Apply one specific work-benefits budget id."},
            "no_apply_credits": {"flag": "--no-apply-credits", "kind": "bool",
                                 "desc": "Opt out of applying account credits."},
            "intent": INTENT_PARAM,
        },
    },
    {
        "name": "dd_address_set",
        "argv": ["address", "set"],
        # --yes is forced below: without it dd-cli waits on an interactive
        # confirmation prompt and the subprocess would hang to its timeout.
        "fixed_args": ["--yes"],
        "writes": True,
        "description": (
            "Change the account's DEFAULT delivery address. This is a persistent account "
            "change that affects every later search and order, not a per-order override. "
            "Confirm explicitly before calling. Address ids are not discoverable through "
            "this bridge, so the requester must supply one."
        ),
        "params": {
            "address_id": {"flag": "--address-id", "kind": "text", "max": 64, "required": True,
                           "desc": "Address id to make default."},
            "intent": INTENT_PARAM,
        },
    },

    # ---- checkout handoff (no charge from here) ----
    {
        "name": "dd_order_checkout_url",
        "argv": ["order", "checkout-url"],
        "writes": False,
        "description": (
            "Return a browser checkout URL for a cart. This bridge cannot place an order; "
            "a human opens the URL and completes payment themselves. Run dd_order_preview "
            "first and state the total, so nobody is asked to approve an unpriced cart."
        ),
        "params": {"cart_uuid": CART_UUID, "intent": INTENT_PARAM},
    },
]

SPECS_BY_NAME = {spec["name"]: spec for spec in TOOL_SPECS}

# A dd-cli subcommand must never appear here unless it is in TOOL_SPECS above.
FORBIDDEN_ARGV = {("order", "submit"), ("address", "list"), ("payment-method", "list"), ("login",)}
for _spec in TOOL_SPECS:
    if tuple(_spec["argv"]) in FORBIDDEN_ARGV:
        raise SystemExit(f"refusing to start: {_spec['name']} exposes {_spec['argv']}")


def build_schema(spec: dict) -> dict:
    """Derive the MCP inputSchema for a tool from its spec."""
    properties, required = {}, []
    for name, param in spec["params"].items():
        kind = param["kind"]
        if kind in ("text", "json_array"):
            prop = {"type": "string"}
        elif kind == "int":
            prop = {"type": "integer"}
        elif kind == "bool":
            prop = {"type": "boolean"}
        elif kind == "enum":
            prop = {"type": "string", "enum": list(param["choices"])}
        elif kind == "text_list":
            prop = {"type": "array", "items": {"type": "string"}}
        else:
            raise AssertionError(f"unknown kind {kind}")
        prop["description"] = param["desc"]
        properties[name] = prop
        if param.get("required"):
            required.append(name)

    if spec.get("location"):
        properties["latitude"] = {"type": "number",
                                  "description": "Latitude to search around. Must be paired with longitude."}
        properties["longitude"] = {"type": "number",
                                   "description": "Longitude to search around. Must be paired with latitude."}

    return {"type": "object", "properties": properties, "required": required}


TOOLS = [
    {
        "name": spec["name"],
        "description": spec["description"],
        "inputSchema": build_schema(spec),
    }
    for spec in TOOL_SPECS
]


def build_argv(spec: dict, args: dict):
    """Turn validated arguments into a dd-cli argv. Flags come from the spec only."""
    argv = [resolve_dd_cli(), "--json-output"] + list(spec["argv"])

    for name, param in spec["params"].items():
        if name not in args or args[name] is None or args[name] == "":
            if param.get("required"):
                raise ValueError(f"{name} is required")
            continue
        value, kind, flag = args[name], param["kind"], param["flag"]

        if kind == "text":
            argv += [flag, coerce_text(value, name, param["max"])]
        elif kind == "int":
            argv += [flag, str(coerce_int(value, name, param["lo"], param["hi"]))]
        elif kind == "enum":
            argv += [flag, coerce_enum(value, name, param["choices"])]
        elif kind == "json_array":
            argv += [flag, coerce_json_array(value, name, param["max"])]
        elif kind == "text_list":
            for item in coerce_text_list(value, name, param["max"], param["max_items"]):
                argv += [flag, item]
        elif kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
            if value:
                argv.append(flag)  # presence-only flag

    argv += list(spec.get("fixed_args", ()))

    if spec.get("location"):
        lat, lng = resolve_location(args)
        if lat is not None:
            argv += ["--lat", str(lat), "--lng", str(lng)]

    return argv


# --- execution ------------------------------------------------------------


def throttle() -> None:
    global _last_call_at
    with _throttle_lock:
        wait = MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def run_tool(name: str, args: dict) -> dict:
    spec = SPECS_BY_NAME.get(name)
    if spec is None:
        raise KeyError(f"unknown tool: {name}")
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")

    argv = build_argv(spec, args)

    throttle()
    try:
        proc = subprocess.run(  # no shell: argv is a list built from the spec
            argv,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            stdin=subprocess.DEVNULL,  # never block on an interactive prompt
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout",
                "detail": f"{' '.join(spec['argv'])} exceeded {SUBPROCESS_TIMEOUT}s"}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0:
        return {"ok": False, "error": f"dd_cli_exit_{proc.returncode}",
                "detail": stderr or stdout or "dd-cli failed with no output"}
    if not stdout:
        return {"ok": False, "error": "empty_response", "detail": stderr}

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return {"ok": True, "result_text": stdout}

    if isinstance(parsed, dict) and parsed.get("isError"):
        return {"ok": False, "error": "dd_cli_error", "detail": normalize_payload(parsed)}

    payload = normalize_payload(parsed)
    post = spec.get("post")
    if post is not None:
        payload = post(payload)
    return {"ok": True, "result": payload}


# --- JSON-RPC -------------------------------------------------------------


def make_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_request(msg: dict):
    """Return a response dict, or None for notifications."""
    method = msg.get("method")
    request_id = msg.get("id")

    if method == "initialize":
        requested = (msg.get("params") or {}).get("protocolVersion")
        return make_result(request_id, {
            "protocolVersion": requested or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "ping":
        return make_result(request_id, {})

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "tools/list":
        return make_result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            body = run_tool(name, args)
        except KeyError as exc:
            return make_error(request_id, -32602, str(exc))
        except ValueError as exc:  # bad arguments from the caller
            body = {"ok": False, "error": "invalid_arguments", "detail": str(exc)}
        except Exception as exc:  # surface as a tool error, not a transport crash
            body = {"ok": False, "error": "internal_error", "detail": str(exc)}
        return make_result(request_id, {
            "content": [{"type": "text", "text": json.dumps(body)}],
            "isError": not body.get("ok", False),
        })

    if request_id is None:
        return None  # unknown notification
    return make_error(request_id, -32601, f"method not found: {method}")


# --- HTTP transport -------------------------------------------------------


class MCPHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_state(self) -> str:
        """Classify the Authorization header without ever logging its value.

        Distinguishes "the client sent no credential" from "it sent the wrong
        one" — the difference between a proxy that isn't injecting a token and a
        token that doesn't match.
        """
        header = self.headers.get("Authorization", "")
        if not header:
            return "absent"
        prefix = "Bearer "
        if not header.startswith(prefix):
            scheme = header.split(" ", 1)[0][:16]
            return f"wrong-scheme:{scheme}"
        if hmac.compare_digest(header[len(prefix):].strip(), self.server.auth_token):
            return "ok"
        return "bearer-mismatch"

    def _authorized(self) -> bool:
        state = self._auth_state()
        sys.stderr.write(f"  auth={state} ua={self.headers.get('User-Agent', '-')[:60]}\n")
        return state == "ok"

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") == "/health":
            # Unauthenticated, and deliberately says nothing about dd-cli state.
            self._send_json(200, {"ok": True, "server": SERVER_NAME,
                                  "version": SERVER_VERSION, "tools": len(TOOLS)})
            return
        self._send_json(405, {"error": "method_not_allowed", "detail": "POST JSON-RPC to /mcp"})

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") not in ("/mcp", ""):
            self._send_json(404, {"error": "not_found"})
            return

        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="doordash-mcp-bridge"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"error": "bad_content_length"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(400, {"error": "bad_request", "detail": "missing or oversized body"})
            return

        try:
            msg = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, make_error(None, -32700, "parse error"))
            return

        if isinstance(msg, list):  # JSON-RPC batch
            responses = [r for r in (handle_request(m) for m in msg if isinstance(m, dict)) if r]
            if not responses:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(200, responses)
            return

        if not isinstance(msg, dict):
            self._send_json(400, make_error(None, -32600, "invalid request"))
            return

        response = handle_request(msg)
        if response is None:  # notification
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(200, response)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")


def serve_http(host: str, port: int, token: str) -> None:
    try:
        httpd = ThreadingHTTPServer((host, port), MCPHandler)
    except OSError as exc:
        raise RuntimeError(
            f"cannot bind {host}:{port} ({exc.strerror}). If an older instance is "
            f"still running, find it with `lsof -nP -iTCP:{port} -sTCP:LISTEN` and "
            "kill that PID — `pkill -f` won't match it, since argv is just "
            "`python3 server.py`."
        ) from exc
    httpd.auth_token = token
    sys.stderr.write(
        f"{SERVER_NAME} {SERVER_VERSION} listening on http://{host}:{port}/mcp "
        f"({len(TOOLS)} tools, dd-cli: {resolve_dd_cli()})\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nshutting down\n")
    finally:
        httpd.server_close()


def serve_stdio() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_request(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def main(argv) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Expose a fixed subset of dd-cli over MCP.")
    parser.add_argument("--stdio", action="store_true", help="Run as an MCP stdio server instead of HTTP.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"HTTP bind address (default {DEFAULT_HOST}).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default {DEFAULT_PORT}).")
    parser.add_argument("--list-tools", action="store_true", help="Print the tool table and exit.")
    ns = parser.parse_args(argv)

    if ns.list_tools:
        for spec in TOOL_SPECS:
            kind = "WRITE" if spec.get("writes") else "read "
            print(f"  {kind}  {spec['name']:<30} dd-cli {' '.join(spec['argv'])}")
        print(f"\n  {len(TOOL_SPECS)} tools. `order submit` is intentionally absent.")
        return 0

    try:
        resolve_dd_cli()
        if ns.stdio:
            serve_stdio()
            return 0
        token = resolve_token()  # HTTP only: stdio is already local-trust
        if ns.host not in ("127.0.0.1", "localhost", "::1"):
            sys.stderr.write(
                f"warning: binding {ns.host} exposes this server beyond localhost. "
                "Prefer 127.0.0.1 and put a tunnel in front of it.\n"
            )
        serve_http(ns.host, ns.port, token)
    except RuntimeError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
