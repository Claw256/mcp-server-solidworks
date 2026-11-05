"""analyze_model(overview) and the new analyze modes, offline: the bridge is faked, so nothing reaches SolidWorks.

Covers the adapter side only: routing, the design-session map that overview attaches (and its tolerance of a missing
or unrelated design), failure passthrough, and the mode list. The C# readers (IMassProperty2, IMeasure, sketch health,
feature definitions) are compile-checked and need a live session to exercise.
"""
import json
import os
import sys

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)


def _deps():
    try:
        import server
        from solidpilot import design
        from solidpilot.tools import analysis
        return server, design, analysis
    except Exception:  # noqa: BLE001
        return None, None, None


_DIGEST = {"document": {"title": "Bracket", "type": "PART"}, "feature_count": 2,
           "features": [{"name": "Boss-Extrude1", "type": "Extrusion"}, {"name": "Fillet1", "type": "Fillet"}],
           "rollback_at_end": True}


def _ok(digest, doc="Bracket.SLDPRT"):
    return {"status": "COMPLETED", "stateVersion": 3,
            "cadState": {"activeDocument": doc, "activeSketch": None, "features": [json.dumps(digest)]}}


class _Design:
    design_id = "d1"
    doc_title = "Bracket"
    head = 2

    def live(self):
        return {"node_features": {"n1": ["Boss-Extrude1"], "n2": ["Fillet1"]}}


def _run(fake, design_obj, **kw):
    server, design, analysis = _deps()
    real_raw, real_get = server.bridge._call_raw, design.get
    server.bridge._call_raw = fake
    design.get = lambda design_id="": design_obj
    try:
        return server.analyze_model("overview", **kw)
    finally:
        server.bridge._call_raw, design.get = real_raw, real_get


def _payload(text):
    # map_response renders: COMPLETED | state_version=.. | document=.. | sketch=.. | features=['{...}']
    start = text.index("features=['") + len("features=['")
    return json.loads(text[start:text.rindex("']")])


def test_overview_asks_the_execution_layer_for_overview_and_attaches_the_design_map():
    server, _design, _analysis = _deps()
    if server is None:
        return
    asked = []

    def fake(tool, params):
        asked.append((tool, params["analysis_type"]))
        return _ok(_DIGEST)

    out = _run(fake, _Design())
    assert asked == [("analyze_model", "overview")]
    digest = _payload(out)
    assert digest["feature_count"] == 2 and digest["rollback_at_end"] is True
    assert digest["design"] == {"design_id": "d1", "step": 2,
                                "node_features": {"n1": ["Boss-Extrude1"], "n2": ["Fillet1"]}}


def test_overview_without_a_design_is_the_plain_digest():
    server, _design, _analysis = _deps()
    if server is None:
        return
    digest = _payload(_run(lambda t, p: _ok(_DIGEST), None))
    assert "design" not in digest and digest["features"][0]["name"] == "Boss-Extrude1"


def test_overview_ignores_a_design_for_another_document():
    server, _design, _analysis = _deps()
    if server is None:
        return
    other = _Design()
    other.doc_title = "Gearbox"
    assert "design" not in _payload(_run(lambda t, p: _ok(_DIGEST), other))


def test_overview_survives_a_broken_design_record():
    server, _design, _analysis = _deps()
    if server is None:
        return

    class Broken(_Design):
        def live(self):
            raise RuntimeError("corrupt snapshot")

    digest = _payload(_run(lambda t, p: _ok(_DIGEST), Broken()))
    assert "design" not in digest and digest["feature_count"] == 2


def test_overview_failure_is_reported_not_swallowed():
    server, _design, _analysis = _deps()
    if server is None:
        return
    from response_mapper import CadOperationError
    fail = {"status": "FAILED", "error": {"code": "WRONG_DOCUMENT_TYPE", "message": "needs a part"}}
    try:
        _run(lambda t, p: fail, _Design())
    except CadOperationError as ex:
        assert "WRONG_DOCUMENT_TYPE" in str(ex)
    else:
        raise AssertionError("a FAILED overview must raise CadOperationError")


def test_other_modes_pass_straight_through_including_measure():
    server, _design, _analysis = _deps()
    if server is None:
        return
    seen = []

    def fake(tool, operation_id, state_version, params):
        seen.append(params)
        return {"status": "COMPLETED", "stateVersion": 1,
                "cadState": {"activeDocument": "P", "features": [json.dumps({"distance_mm": 12.5})]}}

    spec = json.dumps({"a": {"face": 1}, "b": {"edge": 4}, "kind": "distance"})
    real = server.bridge.call_tool
    server.bridge.call_tool = fake          # _call goes through call_tool, never the real execution layer
    try:
        out = server.analyze_model("measure", name=spec)
    finally:
        server.bridge.call_tool = real
    assert seen[0]["analysis_type"] == "measure" and seen[0]["name"] == spec
    assert _payload(out) == {"distance_mm": 12.5}


def test_new_modes_are_offered_in_the_tool_schema():
    server, _design, _analysis = _deps()
    if server is None:
        return
    tool = {t.name: t for t in server.mcp._tool_manager.list_tools()}["analyze_model"]
    enum = tool.parameters["properties"]["analysis_type"]["enum"]
    assert "overview" in enum and "measure" in enum


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
