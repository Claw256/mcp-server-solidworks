"""Low-poly smoothing: error against the ANALYTIC shape must drop; sharp parts must stay sharp."""
import math

import pytest

pytest.importorskip("trimesh")
import numpy as np  # noqa: E402

import geom  # noqa: E402
import mesh_read  # noqa: E402
import sections  # noqa: E402
import smoothing  # noqa: E402
import synth  # noqa: E402


def _ell_err(mesh, abc):
    P, _ = geom.sample_surface(mesh, 20000)
    f = np.sqrt((P[:, 0] / abc[0]) ** 2 + (P[:, 1] / abc[1]) ** 2 + (P[:, 2] / abc[2]) ** 2)
    return float(np.abs(f - 1).mean()), float(np.abs(f - 1).max())


@pytest.mark.parametrize("abc,sub", [((0.03, 0.03, 0.03), 1), ((0.05, 0.03, 0.02), 1), ((0.05, 0.03, 0.02), 2)])
def test_mesh_smoothing_reduces_error_vs_analytic(abc, sub):
    m = synth.ellipsoid(*abc, subdiv=sub)                           # 80 / 320 faces
    assert len(m.faces) in (80, 320)
    raw_mean, raw_max = _ell_err(m, abc)
    for level in ("low", "medium", "high"):
        sm, info = smoothing.smooth_mesh(m, level)
        mean, mx = _ell_err(sm, abc)
        assert mean < raw_mean * 0.6, (level, mean, raw_mean)
        assert mx < raw_max, level
        assert info["faces_after"] > info["faces_before"]
    # higher level is at least as close
    assert _ell_err(smoothing.smooth_mesh(m, "high")[0], abc)[0] <= _ell_err(smoothing.smooth_mesh(m, "low")[0], abc)[0]


def test_low_poly_vase_smoothed_is_closer_to_analytic_profile():
    m, line = synth.vase(sections=14)
    P, _ = geom.sample_surface(m, 20000)

    def err(mesh):
        Q, _ = geom.sample_surface(mesh, 20000)
        keep = (Q[:, 1] > 0.004) & (Q[:, 1] < 0.116)                  # the wall, not the end caps
        Q = Q[keep]
        r = np.hypot(Q[:, 0], Q[:, 2])
        want = np.array([synth.vase_radius(line, y) for y in Q[:, 1]])
        return float(np.abs(r - want).mean())

    sm, rep = smoothing.apply(m, "medium", tol=1e-4)
    assert rep["applied"] != "off"
    assert err(sm) < err(m) * 0.7


def test_sharp_edges_preserved_on_l_bracket():
    m = synth.l_bracket()
    sm, info = smoothing.smooth_mesh(m, "high")
    assert info["sharp_edges_preserved"]
    assert np.allclose(sm.bounds, m.bounds, atol=1e-9)             # corners did not move or round
    assert sm.volume == pytest.approx(m.volume, rel=1e-6)           # flat faces stayed flat
    # every original vertex is still exactly where it was
    from scipy.spatial import cKDTree
    mask = smoothing.sharp_vertices(m)
    d, _ = cKDTree(sm.vertices).query(m.vertices[mask])
    assert mask.sum() > 10 and d.max() < 1e-9                       # every sharp-edge vertex is untouched


def test_volume_drift_bounded_and_fallback():
    m = synth.ellipsoid(0.05, 0.03, 0.02, 2)
    sm, rep = smoothing.apply(m, "high", tol=1e-4)
    assert rep["applied"] == "high"
    assert abs(rep["volume_change_pct"]) < 5.0                      # bounded (growth toward the analytic volume)
    assert rep["max_deviation_pct_diag"] < 2.0
    # a shrinking smoothing must be stepped down: force an impossible shrink limit
    sm2, rep2 = smoothing.apply(m, "high", tol=1e-4, max_shrink=-10.0)
    assert rep2["applied"] == "off" and len(rep2["fell_back"]) == 3
    assert smoothing.apply(m, "off", 1e-4)[1]["applied"] == "off"


def test_auto_level_from_triangle_count():
    assert smoothing.choose_level(80, 0.1) == "high"
    assert smoothing.choose_level(3000, 0.1) == "medium"
    assert smoothing.choose_level(10000, 0.1) == "low"
    assert smoothing.choose_level(200000, 0.1) == "off"


def test_contour_spline_smoothing_closer_to_circle():
    R = 0.03
    t = np.linspace(0, 2 * math.pi, 10, endpoint=False)             # a 10-gon: strong faceting
    poly = np.c_[R * np.cos(t), R * np.sin(t)]
    sm = smoothing.smooth_polyline(poly, tol=1e-4)
    assert len(sm) > 5 * len(poly)

    def err(p):
        q = np.vstack([p, p[:1]])
        pts = np.vstack([q[:-1] + (q[1:] - q[:-1]) * s for s in np.linspace(0, 1, 20, endpoint=False)])
        return float(np.abs(np.hypot(pts[:, 0], pts[:, 1]) - R).max())

    assert err(sm) < err(poly) * 0.5


def test_contour_smoothing_keeps_corners():
    rect = np.array([[0, 0], [0.04, 0], [0.04, 0.02], [0, 0.02]], float)
    dense = np.vstack([np.linspace(rect[i], rect[(i + 1) % 4], 12, endpoint=False) for i in range(4)])
    sm = smoothing.smooth_polyline(dense, tol=1e-4)
    for c in rect:
        assert np.linalg.norm(sm - c, axis=1).min() < 1e-6
    assert sm[:, 0].max() == pytest.approx(0.04, abs=2e-5) and sm[:, 1].max() == pytest.approx(0.02, abs=2e-5)
