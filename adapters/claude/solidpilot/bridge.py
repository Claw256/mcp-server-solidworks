"""The adapter's single door to the execution layer.

Owns the in-memory state_version and the ONE retry/resync path. Tool modules call `_call`
(mapped text, raises CadOperationError) or `_call_raw` (raw response dict) and never touch
`call_tool` directly; tests patch `bridge.call_tool` / `bridge._call_raw`, so callers reach
`_call_raw` as `bridge._call_raw(...)` (attribute lookup at call time), not by import.
"""
import uuid

from execution_client import call_tool, get_state, ExecutionLayerError
from response_mapper import map_response, CadOperationError


class BridgeState:
    """state_version in memory. Starts at 0, updated after every response. Auto-resyncs from the
    execution layer on an INVALID_STATE_VERSION mismatch (e.g. the execution server was restarted
    after a rebuild)."""

    def __init__(self) -> None:
        self.version: int = 0


state = BridgeState()


def _next_operation_id() -> str:
    return str(uuid.uuid4())


def _update_state_version(response: dict) -> None:
    sv = response.get("stateVersion")
    if sv is not None:
        state.version = sv


def is_state_mismatch(response: dict) -> bool:
    if response.get("status") != "FAILED":
        return False
    return (response.get("error") or {}).get("code") == "INVALID_STATE_VERSION"


_is_state_mismatch = is_state_mismatch   # historical name


def _execute(tool_name: str, params: dict) -> dict:
    """The single retry core: one call, and on INVALID_STATE_VERSION fetch the authoritative
    state_version and retry once with a fresh operation_id."""
    response = call_tool(tool_name, _next_operation_id(), state.version, params)
    if is_state_mismatch(response):
        state.version = get_state()
        response = call_tool(tool_name, _next_operation_id(), state.version, params)
    _update_state_version(response)
    return response


def _call(tool_name: str, params: dict) -> str:
    """Send a tool call to the execution layer and map the response to the model-facing text.

    If the server reports an INVALID_STATE_VERSION mismatch (it was restarted and its
    state_version reset while this adapter kept its old value), the state_version is resynced and
    the call retried once. This removes the need to restart the adapter after every
    execution-layer rebuild.
    """
    try:
        response = _execute(tool_name, params)
    except ExecutionLayerError as ex:      # connection / spawn / HTTP failures: say WHAT, not "Error executing tool"
        raise CadOperationError(f"Execution layer unavailable | {ex}") from ex
    return map_response(response)


def _call_raw(tool_name: str, params: dict) -> dict:
    """Like _call() but returns the RAW ExecutionResponse dict (no string mapping, no raise).

    Used by adapter-side orchestration (save_analysis) that needs the structured payloads the
    analyze tools carry in `result_geometry`. Same one-shot resync-retry on INVALID_STATE_VERSION."""
    return _execute(tool_name, params)


def _resync_state_version() -> int:
    """Realign the adapter's local state_version with the authoritative GET /state.

    A submit_feature_graph run performs MANY execution ops (each bumping state_version) OUTSIDE the
    normal _call() path, so afterwards — success OR failure — we resync the local value, or the NEXT
    normal tool call would fail INVALID_STATE_VERSION (KNOWN-LIMITATIONS #5)."""
    try:
        state.version = get_state()
    except Exception:  # noqa: BLE001
        pass
    return state.version
