"""The shared MCPServer instance. Tool modules import `mcp` from here and register on import."""
from mcp.server import MCPServer
from mcp.server.caching import CacheHint

# NOTE: pycompiler is reached via `from ir_execution_port import run_feature_graph` imported
# LAZILY inside rebuild_from_ir and submit_feature_graph — a missing compiler tree degrades to a
# clean tool error instead of killing the whole MCP server at startup.

# P0.4 MCP hardening: discriminators are `Literal[...]` (invalid values rejected at the
# MCP schema level, before any REST/COM round-trip) and numeric params carry Pydantic
# `Field` constraints (units/ranges). Coordinates stay plain float (negative/zero are
# legitimate); open-ended selector strings (entity1_type, plane/material names) stay str
# so valid selectors are never rejected. Editing this file requires reconnecting the
# `solidworks` MCP server (no hot-reload — KNOWN-LIMITATIONS #4).

# Every tool is registered with `structured_output=False`. All 52 return a `str`, so the
# SDK's structured mirror is `{"result": "<the same string>"}` — the whole payload a second
# time, for zero added information (measured: exactly 2.00x on both SDKs). Switching it off
# halves the wire cost of every reader (the 33 KB `schema://feature-graph` read went over at
# 66 KB before this) and drops the per-tool `outputSchema` from the tools/list prompt. Restore
# it only for a tool that returns real structure, never for a `-> str` one.

# Cache hints for the resource surface (SEP-2549, protocol 2026-07-28). NOTE: `ttl_ms`/
# `cache_scope` are set PER METHOD at construction, NOT per resource — the high-level
# `@mcp.resource` decorator returns str/bytes, so an individual handler cannot carry its own TTL.
# One value therefore covers every resource. `scope="public"`: the recipe rules and contract
# schemas are byte-identical for every caller, with nothing authorization-dependent in them.
# ttl_ms=1h is deliberately shorter than it could be — recipe-usage.md is read FRESH from disk on
# every read so an edit goes live without a reconnect, and a long TTL would defeat that.
# MEASURED 2026-07-30, and it settles the matter: the host does NOT honor these hints. A marker
# appended to recipe-usage.md showed up in `recipe://usage/coverage` immediately, with no
# reconnect, on a URI already read minutes earlier in the same session — a TTL-honoring client
# would have served the stale copy. Good news (reads are genuinely fresh), but the caching win is
# ZERO. And no host setting can change the deeper limit: a cached body STILL enters the model's
# context on every read, so TTL caching can only save a round-trip, never tokens. The whole
# measured saving of this migration is the per-turn `tools/list` shrinkage. Hints stay declared —
# spec-correct, free, and a future host may use them.
_RECIPE_CACHE = CacheHint(ttl_ms=3_600_000, scope="public")  # 1 h
mcp = MCPServer(
    "solidworks-execution-adapter",
    cache_hints={
        "resources/read": _RECIPE_CACHE,
        "resources/list": _RECIPE_CACHE,
        "resources/templates/list": _RECIPE_CACHE,
    },
)
