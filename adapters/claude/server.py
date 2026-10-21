"""SolidWorks MCP adapter entry point.

The tools live in `solidpilot/tools/*` (one module per domain) and register themselves on the
shared `mcp` instance from `solidpilot.app` when imported here. State/retry plumbing is in
`solidpilot.bridge`. Editing any of this requires reconnecting the `solidworks` MCP server
(no hot-reload — KNOWN-LIMITATIONS #4).
"""
import inspect

from solidpilot.app import mcp
from solidpilot import bridge  # noqa: F401  (re-exported for agent.py and tests)

# Importing the tool modules registers their tools. Star-imports keep `server.<tool>` working
# for callers that invoke a tool function directly (tests).
from solidpilot.tools import documents  # noqa: F401  (module re-exported for tests)
from solidpilot.tools.documents import *  # noqa: F401,F403
from solidpilot.tools.assembly import *  # noqa: F401,F403
from solidpilot.tools.sketch import *  # noqa: F401,F403
from solidpilot.tools.features import *  # noqa: F401,F403
from solidpilot.tools.drawing_out import *  # noqa: F401,F403
from solidpilot.tools.export import *  # noqa: F401,F403
from solidpilot.tools.analysis import *  # noqa: F401,F403
from solidpilot.tools.drawing_in import *  # noqa: F401,F403
from solidpilot.tools.clarify import *  # noqa: F401,F403
from solidpilot.tools.recipes import *  # noqa: F401,F403
from solidpilot.tools.compare import *  # noqa: F401,F403
from solidpilot.tools.graph import *  # noqa: F401,F403


# ---------------------------------------------------------------------------
# Tool-surface normalisation (prompt cost)
# ---------------------------------------------------------------------------
def _normalize_tool_surface() -> None:
    """Undo three SDK artefacts that ride in the tools/list prompt on every turn.

    1. Docstrings keep their source indentation (the SDK does not dedent them).
    2. Every parameter gets an auto-generated `title` that just re-spells its own name.
    3. `additionalProperties` is absent, so a typo'd param name is not rejected by the
       schema — the P0.4 stance is to reject at the MCP layer, before any REST/COM
       round-trip, so put it back.

    `Tool.description` / `Tool.parameters` are plain mutable fields that `list_tools()`
    reads at call time, so one pass after registration is enough. Without this the payload
    is ~9% larger than the FastMCP surface it replaced; with it, ~2% smaller.

    Touches `mcp._tool_manager` (private). The public alternative is the `middleware`
    hook, which the SDK marks provisional — revisit if this breaks on an SDK bump; the
    schema-contract test is what catches it.
    """
    for tool in mcp._tool_manager.list_tools():
        if tool.description:
            tool.description = inspect.cleandoc(tool.description)
        schema = tool.parameters
        if isinstance(schema, dict):
            schema.pop("title", None)
            for prop in (schema.get("properties") or {}).values():
                if isinstance(prop, dict):
                    prop.pop("title", None)
            schema.setdefault("additionalProperties", False)


_normalize_tool_surface()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
