import pytest

pytest.importorskip("trimesh")
import numpy as np  # noqa: E402

import lower  # noqa: E402
import synth  # noqa: E402
import verify  # noqa: E402


def test_identical_mesh_passes(stl):
    m = synth.box_with_hole()
    r = verify.compare(stl(m, "a"), stl(m, "b"), units="m", built_units="m")
    assert r["verdict"] == "pass"
    assert r["distance"]["max_pct_diag"] < 0.8 and abs(r["volume_error_pct"]) < 0.1


def test_translated_and_unit_guess(stl):
    m = synth.l_bracket()
    moved = m.copy()
    moved.apply_translation([0.3, -0.1, 0.2])
    # built STL exported in millimetres and offset in space: unit auto-detect + centre alignment
    r = verify.compare(stl(m, "a"), stl(moved, "b", scale=1000.0), units="m")
    assert r["built"]["units_assumed"] == "mm"
    assert r["verdict"] == "pass"
    assert np.allclose(r["center_offset_m"], [-0.3, 0.1, -0.2], atol=1e-6)


def test_scaled_part_fails(stl):
    m = synth.box_with_hole()
    big = m.copy()
    big.apply_scale([1.0, 1.0, 1.12])
    r = verify.compare(stl(m, "a"), stl(big, "b"), units="m", built_units="m")
    assert r["verdict"] == "fail" and r["volume_error_pct"] > 8
    assert r["reasons"]


def test_missing_hole_is_flagged(stl):
    with_hole = synth.box_with_hole()
    solid = synth.box_with_hole(hole_d=0.0005)
    r = verify.compare(stl(with_hole, "a"), stl(solid, "b"), units="m", built_units="m")
    assert r["verdict"] in ("marginal", "fail")
    assert r["volume_error_pct"] > 3


def test_plan_transform_aligns_rotated_source(stl):
    src = synth.rotated(synth.l_bracket())
    p = lower.plan(stl(src, "src"), units="m")
    # the part as SolidWorks would hold it: the source moved into the planner's build frame
    built = src.copy()
    built.apply_transform(np.array(p["transform"]))
    r = verify.compare(stl(src, "s2"), stl(built, "bt"), units="m", built_units="m", transform=p["transform"])
    assert r["verdict"] == "pass", r["reasons"]
    assert r["distance"]["p95_pct_diag"] < 1.0
    # without the transform the rotated source and the build-frame part do NOT line up
    bad = verify.compare(stl(src, "s3"), stl(built, "bt2"), units="m", built_units="m", align="none")
    assert bad["verdict"] != "pass"
