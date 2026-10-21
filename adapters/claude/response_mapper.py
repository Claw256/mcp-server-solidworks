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
import os

try:
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError
except Exception:  # noqa: BLE001 - tests / tooling without the SDK still import this module
    class _ToolError(Exception):
        pass


class CadOperationError(_ToolError, RuntimeError):
    """A failure the model should READ: the execution layer's code and message, verbatim."""


_REGISTRY = None


def _error_registry() -> dict:
    """code -> {retryable, hint} from execution/solidworks/contracts/error-codes.json ({} if unavailable)."""
    global _REGISTRY
    if _REGISTRY is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "execution", "solidworks",
                            "contracts", "error-codes.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                _REGISTRY = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
        except (OSError, ValueError):
            _REGISTRY = {}
    return _REGISTRY


def error_advice(code) -> str:
    """` | hint=... [| retryable]` for a known execution-layer code, '' for an unknown one."""
    entry = _error_registry().get(code or "")
    if not entry:
        return ""
    out = f" | hint={entry['hint']}"
    if entry.get("retryable"):
        out += " | retryable=same call may succeed"
    return out


def _render_diagnostics(diag) -> str:
    """The model-facing form of the execution layer's post-tool health read-back ('' when absent).

    A feature SolidWorks reports in error AFTER this call (and not before it) is stated plainly, because
    the tool call itself succeeded and nothing else would tell the model the new feature is broken."""
    if not diag:
        return ""
    out = ""
    fresh = diag.get("new_feature_errors") or []
    if fresh:
        rows = ", ".join(
            f"{e.get('feature')} ({e.get('type') or '?'}): {e.get('error')}"
            + (" [warning]" if e.get("warning") else "") for e in fresh)
        out += f" | FEATURE_ERRORS=[{rows}]"
        if any(not e.get("warning") for e in fresh):
            out += (" | WARNING: SolidWorks reports the feature(s) above in error although the call "
                    "returned. Fix the inputs or remove them before building on this.")
    if diag.get("volume_delta_m3") is not None:
        out += f" | volume_delta_m3={diag['volume_delta_m3']}"
    ww = diag.get("whats_wrong")
    if ww and not fresh:
        out += f" | whats_wrong={ww} (pre-existing items in the What's Wrong list)"
    return out


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
        text += _render_diagnostics(response.get("diagnostics"))
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
            f"message={error.get('message')}{error_advice(error.get('code'))}"
        )

    raise CadOperationError(f"Unknown execution response status: {status}")
