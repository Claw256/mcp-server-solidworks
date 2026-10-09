"""
Maps execution/solidworks ExecutionResponse payloads to MCP tool result strings.

COMPLETED → success text with state summary
FAILED    → raises CadOperationError (a ToolError: the model reads `code | message`)
DUPLICATE → success text noting idempotent result

WHY ToolError: the MCP SDK (v2) shows the model only "Error executing tool <name>" for any exception
that is NOT a ToolError -- the real cause (NO_ACTIVE_DOCUMENT, EXTRUSION_FAILED, ...) was invisible, so
a failed verify_state looked like an unexplained crash. CadOperationError is also a RuntimeError, so
existing `except RuntimeError` handlers keep working.
"""
import json

try:
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError
except Exception:  # noqa: BLE001 - tests / tooling without the SDK still import this module
    class _ToolError(Exception):
        pass


class CadOperationError(_ToolError, RuntimeError):
    """A failure the model should READ: the execution layer's code and message, verbatim."""


def map_response(response: dict) -> str:
    """
    Convert an ExecutionResponse dict into a MCP-compatible result string.

    Raises CadOperationError for FAILED responses so the SDK marks the call as an error AND the
    model sees why.
    """
    status = response.get("status")

    if status == "COMPLETED":
        state = response.get("cadState") or {}
        text = (
            f"COMPLETED | state_version={response.get('stateVersion')} | "
            f"document={state.get('activeDocument')} | "
            f"sketch={state.get('activeSketch')} | "
            f"features={state.get('features', [])}"
        )
        # In-band echo of the REAL geometry a create tool just produced (read back from SW,
        # not the input) so the host can self-verify without a separate analyze round-trip.
        # Read-only — does not affect state_version. Only present on tools that populate it.
        result_geometry = response.get("result_geometry")
        if result_geometry is not None:
            text += f" | result_geometry={json.dumps(result_geometry)}"
        return text

    if status == "DUPLICATE":
        return (
            f"DUPLICATE | operation already executed | "
            f"last_known_state_version={response.get('last_known_state_version')}"
        )

    if status == "FAILED":
        error = response.get("error") or {}
        raise CadOperationError(
            f"CAD operation failed | code={error.get('code')} | "
            f"message={error.get('message')}"
        )

    raise CadOperationError(f"Unknown execution response status: {status}")
