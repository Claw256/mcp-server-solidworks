"""compare_to_reference against VECTOR references (PDF, rendered DXF) and polluted raster sheets: the part pen alone
drives the silhouette, so dimension/extension lines, hatching, the frame and the title block cannot skew it."""
import io
import json
import os
import sys

import numpy as np
import pytest

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import refdiff  # noqa: E402

pytest.importorskip("PIL")
fitz = pytest.importorskip("fitz")

from test_refdiff import _FRAME, _fake_bridge, _png  # noqa: E402

MM = 72.0 / 25.4
PX_MM = 3                                           # px per mm of the fake model capture
L_SHAPE = [(0, 0), (100, 0), (100, 25), (40, 25), (40, 60), (0, 60)]      # mm, y down
HOLES = [(10, 10), (10, 50), (70, 12)]
WHOLE = json.dumps([0, 0, 297 * MM, 210 * MM])


def _pt(x, y):
    return fitz.Point(x * MM, y * MM)


def _stroke(page, pts, width, close=False):
    shape = page.new_shape()
    shape.draw_polyline([_pt(*p) for p in pts])
    shape.finish(color=(0, 0, 0), width=width * MM, closePath=close)
    shape.commit()


def _outline(page, ox, oy, pts, holes=HOLES, pen=0.5):
    _stroke(page, [(ox + x, oy + y) for x, y in pts], pen, close=True)
    for hx, hy in holes:
        shape = page.new_shape()
        shape.draw_circle(_pt(ox + hx, oy + hy), 4 * MM)
        shape.finish(color=(0, 0, 0), width=pen * MM)
        shape.commit()


def _arrow(page, tip, direction):
    dx, dy = direction
    base = (tip[0] + dx * 3.5, tip[1] + dy * 3.5)
    side = (-dy * 0.7, dx * 0.7)
    shape = page.new_shape()
    shape.draw_polyline([_pt(*tip), _pt(base[0] + side[0], base[1] + side[1]), _pt(base[0] - side[0], base[1] - side[1])])
    shape.finish(color=None, fill=(0, 0, 0), closePath=True)
    shape.commit()


def polluted_sheet(path, circles=True):
    """A4 sheet: an L-shaped part (100x60 mm, 0.5 pen) with dimension lines whose extension lines run well past
    the part, a hatched section, a 0.7 frame and a title block."""
    doc = fitz.open()
    page = doc.new_page(width=297 * MM, height=210 * MM)
    ox, oy = 90, 70
    _outline(page, ox, oy, L_SHAPE, HOLES if circles else [])
    thin = 0.18
    _stroke(page, [(ox, oy - 2), (ox, oy - 38)], thin)                       # extension lines, long
    _stroke(page, [(ox + 100, oy - 2), (ox + 100, oy - 38)], thin)
    _stroke(page, [(ox, oy - 30), (ox + 100, oy - 30)], thin)                # dimension line
    _arrow(page, (ox, oy - 30), (1, 0))
    _arrow(page, (ox + 100, oy - 30), (-1, 0))
    _stroke(page, [(ox - 2, oy), (ox - 40, oy)], thin)
    _stroke(page, [(ox - 2, oy + 60), (ox - 40, oy + 60)], thin)
    _stroke(page, [(ox - 30, oy), (ox - 30, oy + 60)], thin)
    _arrow(page, (ox - 30, oy), (0, 1))
    _arrow(page, (ox - 30, oy + 60), (0, -1))
    _stroke(page, [(ox + 10, oy + 5), (ox + 90, oy + 5)], 0.13)             # centre line crossing the part
    _stroke(page, [(30, 150), (80, 150), (80, 190), (30, 190)], thin, close=True)       # hatched section
    for k in range(6, 40, 6):
        _stroke(page, [(30, 150 + k), (30 + k, 150)], 0.1)
    _stroke(page, [(5, 5), (292, 5), (292, 205), (5, 205)], 0.7, close=True)             # frame
    _stroke(page, [(187, 175), (292, 175)], 0.25)                                         # title block
    _stroke(page, [(187, 175), (187, 205)], 0.25)
    page.insert_text(_pt(190, 185), "SCALE 1:1  PLATE  A4", fontsize=8)
    page.insert_text(_pt(ox + 40, oy - 32), "100", fontsize=9)
    doc.save(path)
    doc.close()
    return path


def multi_view_sheet(path, views):
    """views: [(origin_mm, (w, h))] boxes with three holes each, drawn with the 0.5 part pen."""
    doc = fitz.open()
    page = doc.new_page(width=297 * MM, height=210 * MM)
    for (ox, oy), (w, h) in views:
        _outline(page, ox, oy, [(0, 0), (w, 0), (w, h), (0, h)],
                 [(w * 0.2, h * 0.5), (w * 0.5, h * 0.5), (w * 0.8, h * 0.5)])
    doc.save(path)
    doc.close()
    return path


def _box_fn(w_mm, h_mm, x0=50, y0=60):
    return lambda ys, xs: (xs >= x0) & (xs < x0 + w_mm * PX_MM) & (ys >= y0) & (ys < y0 + h_mm * PX_MM)


def _l_fn(x0=50, y0=60):
    a = _box_fn(100, 25, x0, y0)
    b = _box_fn(40, 60, x0, y0)
    return lambda ys, xs: a(ys, xs) | b(ys, xs)


def _scene(shape_fn, h=300, w=400):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        img[y, :, :] = 200 + int(40 * y / h)
    ys, xs = np.mgrid[0:h, 0:w]
    img[shape_fn(ys, xs)] = (70, 80, 90)
    return img


def _call(ref, model_fn, size_mm, **kw):
    import server
    from solidpilot import design
    bbox = {"min": [0, 0, 0], "max": [v / 1000 for v in size_mm], "size": [v / 1000 for v in size_mm]}
    orig_call, orig_get = server.bridge._call_raw, design.get
    server.bridge._call_raw = _fake_bridge(model_fn, None, bbox, kw.pop("frame", _FRAME))
    design.get = lambda *a, **k: None
    try:
        return server.compare_to_reference(str(ref), **kw)
    finally:
        server.bridge._call_raw, design.get = orig_call, orig_get


def _old_iou(pdf, model_fn, size_mm):
    """What the previous pipeline scored on the same sheet: every inked pixel is the silhouette."""
    import drawing_prep as dp
    f, doc = dp.open_source(str(pdf))
    try:
        png, _ = dp.render_region(f, doc[0], (0, 0, doc[0].rect.x1, doc[0].rect.y1), dpi=200)
    finally:
        doc.close()
    model = refdiff.foreground_mask(_png(_scene(model_fn)))
    ref = refdiff.foreground_mask(png, fill=True)
    frame = refdiff.frame_from_bbox(model, size_mm[0], size_mm[1])
    try:
        reg = refdiff.register(ref, model)
    except refdiff.RefDiffError:
        return 0.0, "REGISTRATION_WEAK"
    return refdiff.diff(model, reg.mask, frame).iou, "scored"


def test_polluted_vector_sheet_scores_the_part_alone(tmp_path):
    pdf = polluted_sheet(tmp_path / "sheet.pdf")
    old, how = _old_iou(pdf, _l_fn(), (100, 60, 20))
    data = json.loads(_call(pdf, _l_fn(), (100, 60, 20), view="front", region=WHOLE)[0])
    assert data["reference_kind"] == "vector" and data["pen_mm"] == pytest.approx(0.5, abs=0.01)
    assert data["iou"] > 0.97 and data["verdict"] in ("match", "close")
    assert any("other pens" in f for f in data["filtered"]) and "frame" in data["filtered"]
    assert "edge_tolerance_mm" in data and data["region_used"] == pytest.approx([0.0, 0.0, 842.0, 595.3], abs=0.2)
    assert old < 0.7, f"the raster pipeline was expected to fail/skew on this sheet, scored {old:.3f} ({how})"
    print(f"\npolluted sheet: old IoU {old:.3f} ({how}), new IoU {data['iou']}")


def test_pen_falls_back_to_the_largest_closed_silhouette_without_circles(tmp_path):
    pdf = polluted_sheet(tmp_path / "plain.pdf", circles=False)
    data = json.loads(_call(pdf, _l_fn(), (100, 60, 20), view="front", region=WHOLE)[0])
    assert data["reference_kind"] == "vector" and data["iou"] > 0.97


def test_omitted_region_on_the_polluted_sheet_finds_the_part(tmp_path):
    pdf = polluted_sheet(tmp_path / "sheet.pdf")
    out = _call(pdf, _l_fn(), (100, 60, 20), view="front")
    data = json.loads(out[0])
    assert data["iou"] > 0.9, out[0]


def test_multi_view_sheet_selects_the_view_matching_the_requested_one(tmp_path):
    pdf = multi_view_sheet(tmp_path / "views.pdf", [((30, 30), (100, 60)), ((30, 130), (100, 30)), ((170, 30), (30, 60))])
    front = json.loads(_call(pdf, _box_fn(100, 60), (100, 60, 30), view="front")[0])
    top = json.loads(_call(pdf, _box_fn(100, 30), (100, 60, 30), view="top",
                           frame=dict(_FRAME, up="-Z"))[0])
    right = json.loads(_call(pdf, _box_fn(30, 60), (100, 60, 30), view="right", frame=dict(_FRAME, right="-Z"))[0])
    assert front["iou"] > 0.97 and top["iou"] > 0.97 and right["iou"] > 0.97
    assert front["region_used"][1] < 120 and top["region_used"][1] > 300 and right["region_used"][0] > 400
    assert len(front["region_candidates"]) == 3


def test_two_views_with_the_same_aspect_are_ambiguous(tmp_path):
    pdf = multi_view_sheet(tmp_path / "twins.pdf", [((30, 30), (100, 60)), ((30, 120), (100, 60)), ((170, 30), (30, 60))])
    out = _call(pdf, _box_fn(100, 60), (100, 60, 30), view="front")
    assert out[0].startswith("FAILED | AMBIGUOUS_VIEW |") and "region=" in out[0] and len(out) == 1
    ok = json.loads(_call(pdf, _box_fn(100, 60), (100, 60, 30), view="front", region=json.dumps([80, 80, 375, 260]))[0])
    assert ok["iou"] > 0.97


def test_no_view_with_the_right_proportions_is_registration_weak(tmp_path):
    pdf = multi_view_sheet(tmp_path / "views.pdf", [((30, 30), (100, 60)), ((30, 130), (100, 30))])
    out = _call(pdf, _box_fn(40, 80), (40, 80, 30), view="front")
    assert out[0].startswith("FAILED | REGISTRATION_WEAK |")


def test_dxf_reference_round_trips_through_the_renderer(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    pts = [(x, 60 - y) for x, y in L_SHAPE]
    for a, b in zip(pts, pts[1:] + pts[:1]):
        msp.add_line(a, b, dxfattribs={"lineweight": 50})
    for hx, hy in HOLES:
        msp.add_circle((hx, 60 - hy), 4, dxfattribs={"lineweight": 50})
    for x in (0, 100):                                           # extension lines + dimension line, thin
        msp.add_line((x, 62), (x, 100), dxfattribs={"lineweight": 18})
    msp.add_line((0, 90), (100, 90), dxfattribs={"lineweight": 18})
    path = tmp_path / "ref.dxf"
    doc.saveas(path)
    data = json.loads(_call(path, _l_fn(), (100, 60, 20), view="front")[0])
    assert data["reference_kind"] == "vector" and data["iou"] > 0.95, data


def test_dxf_reference_goes_through_open_input_with_the_bridge(tmp_path, monkeypatch):
    import drawing_render
    ezdxf = pytest.importorskip("ezdxf")
    seen = {}
    real = drawing_render.resolve

    def spy(path, workdir=None, bridge=None):
        seen["bridge"] = bridge
        return real(path, workdir=workdir, bridge=bridge)
    monkeypatch.setattr(drawing_render, "resolve", spy)
    doc = ezdxf.new("R2010")
    doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)], close=True)
    path = tmp_path / "box.dxf"
    doc.saveas(path)
    data = json.loads(_call(path, _box_fn(100, 60), (100, 60, 30), view="front")[0])
    assert seen["bridge"] is not None and data["iou"] > 0.9


def test_unsupported_reference_type_is_a_failed_code(tmp_path):
    bad = tmp_path / "x.step"
    bad.write_bytes(b"x")
    assert _call(bad, _box_fn(100, 60), (100, 60, 30), view="front")[0].startswith("FAILED | UNSUPPORTED_TYPE |")


# ---- raster sheets ------------------------------------------------------------------------------

def _scan_sheet(pale=False, noise=0, seed=1):
    """A 900x700 'scan': an outlined L part, long dimension/extension lines, a frame, text-like specks."""
    from PIL import Image, ImageDraw
    rng = np.random.default_rng(seed)
    ink = (150, 150, 235) if pale else (20, 20, 20)
    light = (215, 215, 240) if pale else (110, 110, 110)
    pts = [(300, 250), (600, 250), (600, 325), (420, 325), (420, 430), (300, 430)]
    pic = Image.fromarray(np.full((700, 900, 3), 250, np.uint8))
    d = ImageDraw.Draw(pic)
    d.polygon(pts, outline=ink, width=4)
    for line in ([(300, 240), (300, 60)], [(600, 240), (600, 60)], [(300, 90), (600, 90)], [(290, 250), (80, 250)],
                 [(290, 430), (80, 430)], [(120, 250), (120, 430)]):                      # dimension + extension lines
        d.line(line, fill=light, width=2)
    d.rectangle([8, 8, 891, 691], outline=(30, 30, 30), width=3)                           # frame
    for i in range(12):
        d.text((700 + (i % 4) * 14, 600 + (i // 4) * 12), "A1", fill=ink)
    arr = np.asarray(pic).astype(np.int16)
    if noise:
        arr = np.clip(arr + rng.integers(-noise, noise + 1, arr.shape), 0, 255)
    return arr.astype(np.uint8)


@pytest.mark.parametrize("pale,noise", [(False, 0), (True, 0), (True, 4)])
def test_scan_like_raster_sheet_still_scores(tmp_path, pale, noise):
    ref = tmp_path / "scan.png"
    ref.write_bytes(_png(_scan_sheet(pale, noise)))
    data = json.loads(_call(ref, _l_fn(), (100, 60, 20), view="front", fill=True)[0])
    assert data["reference_kind"] == "raster" and "pen_mm" not in data
    assert data["iou"] > 0.9, data
    assert any(f.startswith("line work") for f in data["filtered"])
    assert "frame" in data["filtered"]


def test_pale_lines_survive_the_adaptive_threshold():
    pale = np.full((200, 300, 3), 255, np.uint8)
    pale[60:140, 100:200] = 235
    pale[64:136, 104:196] = 255                                   # a hollow outline in a pale 20-level ink
    with pytest.raises(refdiff.RefDiffError):                      # a flat tol=28 loses the 20-level ink entirely
        refdiff.foreground_mask(_png(pale), fill=True)
    mask = refdiff.foreground_mask(_png(pale), fill=True, clean=True)
    assert mask.sum() == 80 * 100


def test_dark_frame_cannot_become_the_background():
    img = np.full((200, 300, 3), 255, np.uint8)
    img[:4], img[-4:], img[:, :4], img[:, -4:] = 0, 0, 0, 0
    img[60:140, 100:200] = 30
    filtered = []
    mask = refdiff.foreground_mask(_png(img), clean=True, filtered=filtered)
    assert mask.sum() == 80 * 100 and "frame" in filtered


def test_overlay_respects_the_byte_budget_and_the_relay_size():
    from PIL import Image
    rng = np.random.default_rng(3)
    a = rng.random((1920, 2560)) > 0.5                    # worst case for PNG: noise
    b = rng.random((1920, 2560)) > 0.5
    free = refdiff.overlay_png(a, b, "t")
    assert max(Image.open(io.BytesIO(free)).size) <= 1280
    assert len(refdiff.overlay_png(a, b, "t", max_bytes=60_000)) <= 60_000


def test_result_overlay_stays_inside_the_relay_cap(tmp_path):
    import drawing_prep as dp
    pdf = polluted_sheet(tmp_path / "sheet.pdf")
    out = _call(pdf, _l_fn(), (100, 60, 20), view="front", region=WHOLE)
    assert len(out[1].data) <= dp.max_png_bytes(1)
