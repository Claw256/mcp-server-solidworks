import io
import os
import sys

import numpy as np
import pytest

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import refdiff  # noqa: E402
from solidpilot import viewlabels as vl  # noqa: E402

# A 240 x 180 mm silhouette drawn at 1 px/mm, lower-left corner at pixel (80, 240); model bbox 0..240 x 0..180 x 0..50 mm.
BMIN = {"X": 0.0, "Y": 0.0, "Z": 0.0}
BSIZE = {"X": 240.0, "Y": 180.0, "Z": 50.0}
FRAME = refdiff.Frame(px_per_mm=1.0, origin_px=(80, 240), width_mm=240.0, height_mm=180.0)
FRONT = (("X", 1), ("Y", 1))
BACK = (("X", -1), ("Y", 1))


def _mask(h=300, w=400):
    m = np.zeros((h, w), dtype=bool)
    m[60:240, 80:320] = True
    return m


def test_projector_maps_model_meters_to_pixels_and_depth():
    p = vl.Projector(FRONT, BMIN, BSIZE, FRAME)
    x, y, depth = p.px((0.1, 0.05, 0.02))
    assert (x, y) == (180.0, 190.0) and depth == pytest.approx(20.0)      # front view looks along -Z: +Z is toward us
    assert p.toward == (0.0, 0.0, 1.0)


def test_projector_mirrors_u_for_a_negative_axis_and_flips_the_depth_direction():
    p = vl.Projector(BACK, BMIN, BSIZE, FRAME)
    x, _, _ = p.px((0.01, 0.0, 0.0))
    assert x == 80 + 230.0                                    # x = 10 mm is 230 mm from the bbox edge the axis starts at
    assert p.toward == (0.0, 0.0, -1.0)
    assert p.facing([0, 0, -1]) == 1.0 and p.facing([0, 0, 1]) == -1.0


def test_projector_top_view_uses_z_as_screen_up():
    p = vl.Projector((("X", 1), ("Z", -1)), BMIN, BSIZE, refdiff.Frame(1.0, (80, 240), 240.0, 50.0))
    _, y, _ = p.px((0.0, 0.0, 0.0))
    assert y == 240 - 50.0                                    # Z=0 is the far end of the screen-up (-Z) axis
    assert p.toward == (0.0, 1.0, 0.0)


def _face(i, normal, point, area):
    return {"i": i, "planar": normal is not None, **({"normal": normal} if normal else {}), "point": point, "area": area}


def test_plan_labels_keeps_front_facing_faces_on_the_silhouette_only():
    p = vl.Projector(FRONT, BMIN, BSIZE, FRAME)
    faces = [_face(0, [0, 0, 1], [0.12, 0.09, 0.05], 0.04),
             _face(1, [0, 0, -1], [0.12, 0.09, 0.0], 0.04),            # faces away
             _face(2, [1, 0, 0], [0.24, 0.09, 0.02], 0.01),            # edge-on
             _face(3, [0, 0, 1], [0.9, 0.09, 0.05], 0.01),             # off the model
             _face(4, None, [0.05, 0.05, 0.0], 0.005)]                 # non-planar: no normal, kept
    labels, omitted = vl.plan_labels(faces, [], p, _mask(), "faces")
    assert sorted(lb.text for lb in labels) == ["f0", "f4"] and omitted == []
    f0 = next(lb for lb in labels if lb.index == 0)
    assert (f0.x, f0.y) == (200.0, 150.0)


def test_plan_labels_drops_the_smaller_of_two_overlapping_labels():
    p = vl.Projector(FRONT, BMIN, BSIZE, FRAME)
    faces = [_face(7, [0, 0, 1], [0.100, 0.09, 0.05], 0.001), _face(8, [0, 0, 1], [0.102, 0.09, 0.05], 0.03)]
    labels, omitted = vl.plan_labels(faces, [], p, _mask(), "faces")
    assert [lb.text for lb in labels] == ["f8"] and omitted == ["f7"]


def test_plan_labels_edges_and_both_and_cap():
    p = vl.Projector(FRONT, BMIN, BSIZE, FRAME)
    edges = [{"i": i, "start": [0.0, 0.0, 0.0], "end": [0.01 * (i + 1), 0.0, 0.0], "mid": [0.01 + 0.025 * i, 0.09, 0.0]}
             for i in range(10)]
    faces = [_face(0, [0, 0, 1], [0.2, 0.15, 0.05], 0.04)]
    only_edges, _ = vl.plan_labels(faces, edges, p, _mask(), "edges")
    assert {lb.kind for lb in only_edges} == {"e"}
    both, _ = vl.plan_labels(faces, edges, p, _mask(), "both")
    assert {lb.kind for lb in both} == {"e", "f"}
    capped, omitted = vl.plan_labels([], edges, p, _mask(), "edges", max_per_kind=3)
    assert len(capped) == 3 and len(omitted) == 7


def test_nice_step_picks_1_2_5():
    assert vl.nice_step(180) == 20 and vl.nice_step(95) == 10 and vl.nice_step(40) == 5 and vl.nice_step(0.7) == 0.1


def test_grid_ticks_are_model_coordinates_at_their_pixels():
    p = vl.Projector(FRONT, BMIN, BSIZE, FRAME)
    t = vl.grid_ticks(p, 400, 300)
    assert t["step_mm"] == 50                                # ~10 lines over the 400 px (= 400 mm) width
    (ua, u), (va, v) = t["u"], t["v"]
    assert (ua, va) == ("X", "Y")
    assert (0.0, 80.0) in u and (100.0, 180.0) in u and (-50.0, 30.0) in u           # value mm -> pixel x
    assert (0.0, 240.0) in v and (100.0, 140.0) in v                                 # value mm -> pixel y (down)


def test_grid_ticks_follow_a_mirrored_axis():
    p = vl.Projector(BACK, BMIN, BSIZE, FRAME)
    u = dict(vl.grid_ticks(p, 400, 300)["u"][1])
    assert u[200.0] == 120.0 and u[0.0] == 320.0 and u[250.0] == 70.0


def test_draw_overlay_marks_the_image_and_keeps_its_size():
    pytest.importorskip("PIL")
    from PIL import Image
    base = np.full((300, 400, 3), 40, dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(base).save(buf, format="PNG")
    p = vl.Projector(FRONT, BMIN, BSIZE, FRAME)
    labels, _ = vl.plan_labels([_face(12, [0, 0, 1], [0.12, 0.09, 0.05], 0.04)], [], p, _mask(), "faces")
    out = vl.draw_overlay(buf.getvalue(), labels, vl.grid_ticks(p, 400, 300), "right +X, up +Y")
    img = np.asarray(Image.open(io.BytesIO(out)).convert("RGB"), dtype=int)
    assert img.shape == base.shape
    assert img[140:160, 190:210].mean() > 100                    # the white label box around (200, 150)
    assert abs(img[100, 180] - 40).sum() > 0                      # a grid line at x = 100 mm (pixel 180)
