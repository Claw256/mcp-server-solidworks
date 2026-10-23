"""Strategy ladder: every emitted graph must validate against pycompiler.ir_schema, and the graph
re-evaluated INDEPENDENTLY (ir_eval) must reproduce the source volume / bbox within tolerance."""
import json
import math
import os

import pytest

pytest.importorskip("trimesh")
import numpy as np  # noqa: E402

import ir_eval  # noqa: E402
import lower  # noqa: E402
import synth  # noqa: E402


def _ir_validate(graph):
    from pycompiler import ir_schema
    return ir_schema.validate(graph)


def _plan(stl, mesh, **kw):
    return lower.plan(stl(mesh), units="m", **kw)


def _check(res, mesh, vol_tol, bbox_tol=0.01, true_vol=None):
    assert _ir_validate(res["graph"]) == []
    ev = ir_eval.evaluate(res["graph"])
    v = true_vol if true_vol is not None else float(mesh.volume)
    assert ev["volume_m3"] == pytest.approx(v, rel=vol_tol), (res["strategy"], ev["volume_m3"], v)
    assert sorted(ev["bbox_m"]) == pytest.approx(sorted(float(x) for x in mesh.extents), rel=bbox_tol, abs=2e-4)
    assert res["expected"]["volume_m3"] == pytest.approx(mesh.volume, rel=1e-3)
    return ev


def test_box_with_hole_extrude(stl):
    m = synth.box_with_hole()
    r = _plan(stl, m)
    assert r["strategy"] == "extrude"
    _check(r, m, 0.004)
    kinds = [p["kind"] for n in r["graph"]["nodes"] if n["type"] == "sketch" for p in n["profile"]]
    assert kinds.count("line") == 4 and kinds.count("circle") == 1
    circ = next(p for n in r["graph"]["nodes"] if n["type"] == "sketch" for p in n["profile"] if p["kind"] == "circle")
    assert circ["diameter"] == pytest.approx(0.02, rel=0.01)


def test_l_bracket_extrude_exact(stl):
    m = synth.l_bracket()
    r = _plan(stl, m)
    assert r["strategy"] == "extrude"
    _check(r, m, 0.002, bbox_tol=0.002)
    assert [n["type"] for n in r["graph"]["nodes"]] == ["sketch", "extrude"]


def test_cylinder_extrude_dimensions(stl):
    m = synth.cylinder(0.02, 0.06)
    r = _plan(stl, m)
    assert r["strategy"] == "extrude"
    _check(r, m, 0.004)


def test_vase_revolve(stl):
    m, _ = synth.vase()
    r = _plan(stl, m, smooth="off")
    assert r["strategy"] == "revolve"
    rev = r["graph"]["nodes"][-1]
    assert rev["type"] == "revolve" and rev["axis"]["x1"] == rev["axis"]["x2"] == 0.0
    _check(r, m, 0.01)
    assert r["summary"]["smoothing"]["applied"] == "off"


def test_torus_revolve_with_separate_axis_line(stl):
    m = synth.torus()
    r = _plan(stl, m, smooth="off")
    assert r["strategy"] == "revolve"
    prof = r["graph"]["nodes"][0]["profile"]
    assert any(p["kind"] == "circle" for p in prof)
    assert any(p["kind"] == "line" and p.get("construction") for p in prof)
    _check(r, m, 0.01)


def test_ellipsoid_loft(stl):
    m = synth.ellipsoid(0.05, 0.03, 0.02, 4)
    r = _plan(stl, m, smooth="off")
    assert r["strategy"] == "loft"
    node_types = [n["type"] for n in r["graph"]["nodes"]]
    assert node_types[-1] == "loft" and node_types.count("sketch") == len(r["graph"]["nodes"][-1]["profiles"]) >= 8
    _check(r, m, 0.02, bbox_tol=0.02)


def test_low_poly_sphere_and_ellipsoid_smoothed_plan_is_closer(stl):
    for abc, sub in (((0.03, 0.03, 0.03), 1), ((0.05, 0.03, 0.02), 2)):
        m = synth.ellipsoid(*abc, subdiv=sub)
        true_v = 4.0 / 3.0 * math.pi * abc[0] * abc[1] * abc[2]
        off = _plan(stl, m, smooth="off")
        auto = _plan(stl, m)                                   # smooth='auto'
        e_off = abs(ir_eval.evaluate(off["graph"])["volume_m3"] / true_v - 1)
        e_auto = abs(ir_eval.evaluate(auto["graph"])["volume_m3"] / true_v - 1)
        assert _ir_validate(auto["graph"]) == []
        assert auto["summary"]["smoothing"]["applied"] in ("medium", "high")
        assert e_auto < e_off * 0.4 and e_auto < 0.03
        assert len(auto["graph"]["nodes"]) > len(off["graph"]["nodes"])          # denser stations


def test_smoothing_never_applied_to_prismatic(stl):
    r = _plan(stl, synth.l_bracket(), smooth="high")
    assert r["summary"]["smoothing"]["applied"] == "off"
    assert "prismatic" in r["summary"]["smoothing"]["note"]


def test_rotated_part_plans_the_same(stl):
    m = synth.box_with_hole()
    r = _plan(stl, synth.rotated(m))
    assert r["strategy"] == "extrude"
    _check(r, m, 0.006)


def test_transform_maps_source_into_build_frame(stl):
    m = synth.rotated(synth.l_bracket())
    r = _plan(stl, m)
    T = np.array(r["transform"])
    pts = np.c_[m.vertices, np.ones(len(m.vertices))] @ T.T
    ext = pts[:, :3].max(axis=0) - pts[:, :3].min(axis=0)
    assert ext[2] == pytest.approx(0.05, abs=2e-4)           # extrusion axis is +Z in the build frame
    assert r["expected"]["bbox_m"][2] == pytest.approx(0.05, abs=2e-4)


def test_stacked_extrude_for_stepped_part(stl, tm):
    base = synth.prism(np.array([[-.04, -.03], [.04, -.03], [.04, .03], [-.04, .03]]), (), 0.01, h=0.005)
    top = synth.prism(np.array([[-.02, -.015], [.02, -.015], [.02, .015], [-.02, .015]]), (), 0.015, h=0.005)
    top.apply_translation([0, 0, 0.01])
    m = tm.util.concatenate([base, top])                       # touching boxes: a stepped slab
    r = _plan(stl, m)
    assert r["strategy"] in ("stacked_extrude", "loft")
    assert _ir_validate(r["graph"]) == []
    if r["strategy"] == "stacked_extrude":
        assert [n["type"] for n in r["graph"]["nodes"]].count("extrude") == 2
        ev = ir_eval.evaluate(r["graph"])
        assert ev["volume_m3"] == pytest.approx(0.08 * 0.06 * 0.01 + 0.04 * 0.03 * 0.015, rel=0.01)


def test_forced_strategy_and_ladder_fallback(stl):
    m = synth.ellipsoid()
    r = _plan(stl, m, strategy="extrude")                      # impossible: must fall to loft
    assert r["strategy"] == "loft"
    assert r["summary"]["attempts"][0]["ok"] is False
    with pytest.raises(lower.PlanError):
        _plan(stl, m, strategy="bogus")
    with pytest.raises(lower.PlanError):
        _plan(stl, m, smooth="max")


def test_write_graph_and_compact_summary(stl, tmp_path):
    r = _plan(stl, synth.box_with_hole())
    path = lower.write_graph(r, str(tmp_path), "demo")
    assert path.endswith(os.path.join(".solidpilot", "demo.mesh.graph.json"))
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh) == json.loads(json.dumps(r["graph"]))
    assert len(json.dumps(r["summary"])) < 7000
    assert set(r) >= {"strategy", "graph", "summary", "expected", "transform"}


def test_analyze_entry_point(stl):
    a = lower.analyze(stl(synth.vase()[0]), units="m")
    assert a["recommended_strategy"] == "revolve" and "segments" in a
    assert "_prism" not in json.dumps(a)
