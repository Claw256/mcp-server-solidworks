import json

import pytest

pytest.importorskip("trimesh")

import mesh_read  # noqa: E402
import segment  # noqa: E402
import synth  # noqa: E402


def _an(stl, mesh):
    md = mesh_read.read_mesh(stl(mesh), "m")
    return segment.analyze(md.mesh), md


def test_box_with_through_hole(stl):
    s, _ = _an(stl, synth.box_with_hole(hole_d=0.02))
    assert s["planes"]["count"] >= 4
    holes = [h for h in s["holes"] if h["through"]]
    assert len(holes) == 1
    assert holes[0]["diameter_m"] == pytest.approx(0.02, rel=0.03)
    assert s["prismatic"]["intervals"] == 1
    assert s["recommended_strategy"] == "extrude"
    assert s["axisymmetric"] is None


def test_cylinder_is_axisymmetric_and_prismatic(stl):
    s, _ = _an(stl, synth.cylinder(0.02, 0.06))
    assert s["axisymmetric"] is not None
    assert s["prismatic"]["intervals"] == 1
    assert s["recommended_strategy"] == "extrude"        # simpler than a revolve of a rectangle
    cyl = [c for c in s["cylinders"] if not c["concave"]]
    assert cyl and cyl[0]["radius_m"] == pytest.approx(0.02, rel=0.03)


def test_vase_and_torus_are_revolves(stl):
    for m in (synth.vase()[0], synth.torus()):
        s, _ = _an(stl, m)
        assert s["axisymmetric"] is not None
        assert s["recommended_strategy"] == "revolve"
        assert s["prismatic"] is None


def test_l_bracket_is_a_single_extrusion(stl):
    s, _ = _an(stl, synth.l_bracket())
    assert s["recommended_strategy"] == "extrude"
    assert s["axisymmetric"] is None


def test_ellipsoid_is_freeform(stl):
    s, _ = _an(stl, synth.ellipsoid())
    assert s["recommended_strategy"] == "loft"
    assert s["axisymmetric"] is None and s["prismatic"] is None


def test_rotated_part_gives_same_answer(stl):
    s, _ = _an(stl, synth.rotated(synth.box_with_hole()))
    assert s["recommended_strategy"] == "extrude"
    assert any(h["through"] for h in s["holes"])


def test_summary_is_compact_json(stl):
    s, _ = _an(stl, synth.box_with_hole())
    txt = json.dumps(segment.public(s))
    assert len(txt) < 6000
    assert "_prism" not in txt
