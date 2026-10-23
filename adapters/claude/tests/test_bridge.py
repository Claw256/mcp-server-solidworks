"""Offline tests for the bridge's retry/resync core, _run_graph, and the tool-surface cleanup."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

try:
    import pytest
    from solidpilot import bridge
    import server
    from solidpilot.tools import graph
    _DEPS = True
except ImportError:                       # adapter deps not installed: standalone runs skip
    _DEPS = False

_BOX_GRAPH = {"units": "meters", "nodes": [{"id": "b1", "type": "box", "ref": {"datum": "top"},
                                            "width": 0.05, "depth": 0.05, "height": 0.05}]}
_ASM_GRAPH = {"units": "meters", "nodes": [{"id": "c1", "type": "component", "source": {"path": "C:/a.SLDPRT"},
                                            "transform": [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]}]}
COMPLETED = {"status": "COMPLETED", "stateVersion": 7, "cadState": {}}
MISMATCH = {"status": "FAILED", "error": {"code": "INVALID_STATE_VERSION", "message": "x"}}


def _scripted(responses, calls):
    it = iter(responses)

    def fake(tool, op_id, state_version, params):
        calls.append((tool, op_id, state_version))
        return next(it)
    return fake


def _swap(**attrs):
    class Ctx:
        def __enter__(self):
            self.old = {k: getattr(bridge, k) for k in attrs}
            for k, v in attrs.items():
                setattr(bridge, k, v)
            self.sv = bridge.state.version

        def __exit__(self, *a):
            for k, v in self.old.items():
                setattr(bridge, k, v)
            bridge.state.version = self.sv
    return Ctx()


def test_mismatch_resyncs_and_retries_once_with_fresh_op_id():
    if not _DEPS:
        return
    calls = []
    with _swap(call_tool=_scripted([MISMATCH, COMPLETED], calls), get_state=lambda: 41):
        bridge.state.version = 3
        resp = bridge._execute("verify_state", {})
        assert bridge.state.version == 7             # updated from the final response
    assert resp is COMPLETED
    assert [c[2] for c in calls] == [3, 41]          # retried with the authoritative version
    assert calls[0][1] != calls[1][1]                # fresh operation_id


def test_second_mismatch_is_returned_not_looped():
    if not _DEPS:
        return
    calls = []
    with _swap(call_tool=_scripted([MISMATCH, MISMATCH], calls), get_state=lambda: 9):
        resp = bridge._execute("verify_state", {})
    assert resp is MISMATCH and len(calls) == 2


def test_call_wraps_transport_failure_as_tool_error():
    if not _DEPS:
        return

    def boom(*a, **k):
        raise bridge.ExecutionLayerError("down")
    with _swap(call_tool=boom):
        with pytest.raises(bridge.CadOperationError) as ei:
            bridge._call("verify_state", {})
    assert "Execution layer unavailable" in str(ei.value)


def test_resync_swallows_get_state_failure():
    if not _DEPS:
        return

    def fail():
        raise RuntimeError("no server")
    with _swap(get_state=fail):
        bridge.state.version = 5
        assert bridge._resync_state_version() == 5


def test_run_graph_fresh_document_failure_is_a_complete_failed_line():
    if not _DEPS:
        return
    real = bridge._call_raw
    bridge._call_raw = lambda tool, params: {"status": "FAILED", "error": {"code": "NO_SW", "message": "closed"}}
    try:
        text, ok, _sv = graph._run_graph(_BOX_GRAPH, True)
    finally:
        bridge._call_raw = real
    assert not ok and text.startswith("FAILED | OPEN_NEW_PART_FAILED | NO_SW: closed")


def test_run_graph_picks_assembly_document_for_assembly_graphs():
    if not _DEPS:
        return
    seen = []
    real = bridge._call_raw
    bridge._call_raw = lambda tool, params: (seen.append(tool) or {"status": "FAILED", "error": {}})
    try:
        graph._run_graph(_ASM_GRAPH, True)
    finally:
        bridge._call_raw = real
    assert seen == ["open_new_assembly"]


def test_normalize_tool_surface_cleans_descriptions_and_schemas():
    if not _DEPS:
        return
    for tool in server.mcp._tool_manager.list_tools():
        assert tool.description == tool.description.strip()
        assert "title" not in tool.parameters
        assert tool.parameters.get("additionalProperties") is False
        for prop in (tool.parameters.get("properties") or {}).values():
            assert "title" not in prop


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
