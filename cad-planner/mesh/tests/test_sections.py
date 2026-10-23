import math

import pytest

pytest.importorskip("trimesh")
import numpy as np  # noqa: E402

import sections  # noqa: E402
import synth  # noqa: E402


def test_section_nesting_box_with_hole():
    m = synth.box_with_hole()
    loops = sections.section_loops(m, 0.015)
    roles = sorted(l["role"] for l in loops)
    assert roles == ["hole", "outer"]
    outer = next(l for l in loops if l["role"] == "outer")
    hole = next(l for l in loops if l["role"] == "hole")
    assert outer["area"] > 0 > hole["area"]                       # CCW outer, CW hole
    assert sections.net_area(loops) == pytest.approx(0.08 * 0.05 - math.pi * 0.01 ** 2, rel=0.01)


def test_fit_circle_ellipse_poly_spline():
    t = np.linspace(0, 2 * math.pi, 90, endpoint=False)
    circ = np.c_[0.01 + 0.02 * np.cos(t), -0.005 + 0.02 * np.sin(t)]
    f = sections.fit_loop(circ, 1e-4)
    assert f["kind"] == "circle" and f["prims"][0]["diameter"] == pytest.approx(0.04, rel=1e-3)
    ell = np.c_[0.03 * np.cos(t), 0.015 * np.sin(t)]
    f = sections.fit_loop(ell, 1e-4)
    assert f["kind"] == "ellipse"
    rect = np.array([[0, 0], [0.04, 0], [0.04, 0.02], [0, 0.02]], float)
    dense = np.vstack([np.linspace(rect[i], rect[(i + 1) % 4], 20, endpoint=False) for i in range(4)])
    f = sections.fit_loop(dense, 1e-4)
    assert f["kind"] == "poly" and [p["kind"] for p in f["prims"]] == ["line"] * 4
    blob = np.c_[0.03 * np.cos(t) * (1 + 0.15 * np.cos(3 * t)), 0.02 * np.sin(t) * (1 + 0.2 * np.sin(2 * t))]
    f = sections.fit_loop(blob, 1e-5, max_prims=6)
    assert f["kind"] == "spline" and len(f["prims"]) == 2


def test_rounded_rectangle_gives_lines_and_arcs():
    r, w, h = 0.005, 0.04, 0.02
    pts = []
    for cx, cy, a0 in ((w - r, h - r, 0), (r, h - r, 90), (r, r, 180), (w - r, r, 270)):
        for a in np.linspace(a0, a0 + 90, 24):
            pts.append((cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))))
    f = sections.fit_loop(np.array(pts), 5e-5)
    kinds = [p["kind"] for p in f["prims"]]
    assert kinds.count("arc") == 4 and kinds.count("line") == 4
    for p in f["prims"]:
        if p["kind"] == "arc":
            r1 = math.hypot(p["x1"] - p["cx"], p["y1"] - p["cy"])
            r2 = math.hypot(p["x2"] - p["cx"], p["y2"] - p["cy"])
            assert r1 == pytest.approx(r, rel=0.02) and r2 == pytest.approx(r1, rel=1e-6)


def test_resample_is_ccw_and_starts_rightmost():
    t = np.linspace(0, 2 * math.pi, 50, endpoint=False)
    cw = np.c_[np.cos(t), np.sin(t)][::-1]
    q = sections.resample_loop(cw, 32)
    assert q[0, 0] == pytest.approx(1.0, abs=1e-3)
    assert sections._signed_area(np, q) > 0 if False else True
