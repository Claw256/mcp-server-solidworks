"""verify_against_interpretation profile + dimensions rows, and the journal records the compare/verify tools write.
Offline: the bridge is faked (no SolidWorks); the design journal uses the fake feature-tree port."""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

from fake_tree_port import Env, box, graph  # noqa: E402
import test_refdiff as tr  # noqa: E402

pytest.importorskip("PIL")


class _Bridge:
    """analyze_model(measure) answers by face index; anything else the verify needs is bbox/mass."""

    def __init__(self, measures, fail_measure=False):
        self.measures, self.fail_measure, self.names = measures, fail_measure, []

    def __call__(self, tool, params):
        assert tool == "analyze_model", tool
        kind = params["analysis_type"]
        if kind == "measure":
            if self.fail_measure:
                return {"status": "FAILED", "error": {"code": "UNKNOWN_ANALYSIS", "message": "measure"}}
            self.names.append(params["name"])
            req = json.loads(params["name"])
            return {"status": "COMPLETED", "cadState": {"features": [json.dumps(self.measures[req["a"]["face"]])]}}
        raise AssertionError(kind)


def _run(expected, bridge):
    import server
    orig = server.bridge._call_raw
    server.bridge._call_raw = bridge
    try:
        return server.verify_against_interpretation(json.dumps(expected))
    finally:
        server.bridge._call_raw = orig


_DIM = {"name": "boss height", "a": {"face": 0}, "b": {"face": 1}, "kind": "distance", "value_mm": 25}


def test_dimensions_are_measured_through_the_contract_and_judged():
    br = _Bridge({0: {"distance_mm": 25.02}, 1: {"radius_mm": 4.0}})
    out = _run({"dimensions": [_DIM, {"name": "r", "a": {"face": 1}, "kind": "radius", "value_mm": 5}]}, br)
    assert "[PASS] dimension boss height" in out and "[FAIL] dimension r" in out
    assert json.loads(br.names[0]) == {"a": {"face": 0}, "b": {"face": 1}, "kind": "distance"}
    assert json.loads(br.names[1]) == {"a": {"face": 1}, "kind": "radius"}


def test_an_older_execution_layer_reports_not_checked_not_failed():
    out = _run({"dimensions": [_DIM]}, _Bridge({}, fail_measure=True))
    assert "[NOT CHECKED] dimension boss height (UNKNOWN_ANALYSIS: measure)" in out


def test_a_malformed_dimension_is_a_bad_expected_failure():
    out = _run({"dimensions": [{"kind": "distance", "a": {"face": 0}, "b": {"face": 1}}]}, _Bridge({}))
    assert out.startswith("FAILED | BAD_EXPECTED |")


def _fake_compare(monkeypatch, results):
    """compare_view stand-in: results[view] is a result dict or a CompareError."""
    from solidpilot.tools import compare_ref
    calls = []

    def fake(reference, view, page=1, region="", dpi=200, fill=True, clean_px=0, localise=True):
        calls.append({"view": view, "region": region, "page": page, "localise": localise})
        r = results[view]
        if isinstance(r, Exception):
            raise r
        return r, b""
    monkeypatch.setattr(compare_ref, "compare_view", fake)
    return calls


def _ok(iou, regions=0):
    return {"iou": iou, "model_only_pct": 1.0, "ref_only_pct": 1.0, "regions": [{}] * regions}


def test_profile_rows_run_the_shared_pipeline_and_record_the_scores_on_the_design(monkeypatch, tmp_path):
    from solidpilot import design
    from solidpilot.tools.compare_ref import CompareError
    calls = _fake_compare(monkeypatch, {"front": _ok(0.99), "top": _ok(0.9, 2),
                                        "right": CompareError("PERSPECTIVE_ON", "perspective is on")})
    spec = lambda v, **kw: dict({"view": v, "reference": str(tmp_path / "r.png")}, **kw)  # noqa: E731
    with Env():
        import server
        server.design_new(graph=json.dumps(graph(box("b1"))))
        out = server.verify_against_interpretation(json.dumps(
            {"profile": [spec("front", region=[0, 0, 100, 80], page=2), spec("top", min_iou=0.95), spec("right")]}))
        assert "[PASS] profile front" in out and "[FAIL] profile top" in out
        assert "[NOT CHECKED] profile right (PERSPECTIVE_ON: perspective is on)" in out
        assert calls[0] == {"view": "front", "region": "[0, 0, 100, 80]", "page": 2, "localise": False}
        (rec,) = design.get().live()["verification"]
        assert {k: rec[k] for k in ("tool", "verdict", "passed", "failed", "not_checked", "ious")} == {
            "tool": "verify_against_interpretation", "verdict": "FAIL", "passed": 1, "failed": 1, "not_checked": 1,
            "ious": {"front": 0.99, "top": 0.9}}
        assert "checked: front IoU 0.990, top IoU 0.900, 1 check FAIL" in server.design_history()


def test_compare_to_reference_records_view_iou_regions_verdict_and_adds_the_hint(tmp_path):
    import server
    from solidpilot import design
    nodes = [{"id": "s1", "type": "sketch", "ref": {"datum": "right"}, "profile": []},
             {"id": "e1", "type": "extrude", "sketch": "s1", "operation": "boss", "depth": 0.24}]
    # the reference lacks a 30 x 40 mm notch at the model's +X end that the model still has (right datum: the
    # extrude runs along +X, so the notch is the extrude's end face)
    ref = lambda ys, xs: tr._rect(60, 240, 80, 320)(ys, xs) & ~tr._rect(100, 140, 290, 320)(ys, xs)  # noqa: E731
    model = lambda ys, xs: tr._rect(60, 240, 80, 320)(ys, xs)  # noqa: E731
    fmap = [{"feature": "Boss-Extrude1", "type": "Extrusion", "created_faces": [{"point": [0.23, 0.12, 0.02]}]}]
    with Env():
        server.design_new(graph=json.dumps(graph(box("b1"))))
        d = design.get()
        d.live()["nodes"] = nodes
        d.live()["node_features"] = {"e1": ["Boss-Extrude1"]}
        pytest.importorskip("fitz")
        ref_png = tmp_path / "ref.png"
        ref_png.write_bytes(tr._png(tr._gradient_scene(ref)))
        server.bridge._call_raw = tr._fake_bridge(model, fmap, {"min": [0, 0, 0], "max": [0.24, 0.18, 0.05],
                                                                 "size": [0.24, 0.18, 0.05]},
                                                  {"has_perspective": False, "axis_aligned": True, "right": "+X", "up": "+Y"})
        data = json.loads(server.compare_to_reference(str(ref_png), view="front")[0])
        rec = d.live()["verification"][0]
    assert rec["tool"] == "compare_to_reference" and rec["view"] == "front" and rec["iou"] == data["iou"]
    assert rec["regions"] == len(data["regions"]) and rec["verdict"] == data["verdict"]
    (region,) = data["regions"]
    assert region["design_step_hint"]["suggestion_only"] is True
    assert region["design_step_hint"]["patch"] == [{"op": "set", "id": "e1", "params": {"depth": 0.21}}]   # 0.24 m - 30 mm
