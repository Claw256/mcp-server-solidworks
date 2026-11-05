import io
import os
import sys

import numpy as np
import pytest

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import refdiff  # noqa: E402

pytest.importorskip("PIL")


def _png(rgb: np.ndarray) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(rgb.astype(np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


def _gradient_scene(shape_fn, h=300, w=400):
    """SolidWorks-like capture: a vertical background gradient with a flat dark-grey part drawn on it."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        img[y, :, :] = 200 + int(40 * y / h)
    ys, xs = np.mgrid[0:h, 0:w]
    img[shape_fn(ys, xs)] = (70, 80, 90)
    return img


def _rect(y0, y1, x0, x1):
    return lambda ys, xs: (ys >= y0) & (ys < y1) & (xs >= x0) & (xs < x1)


def test_foreground_mask_handles_a_gradient_background():
    m = refdiff.foreground_mask(_png(_gradient_scene(_rect(60, 240, 80, 320))))
    assert m.sum() == 180 * 240


def test_blank_image_is_reference_unreadable():
    with pytest.raises(refdiff.RefDiffError) as e:
        refdiff.foreground_mask(_png(np.full((100, 100, 3), 255, np.uint8)))
    assert e.value.code == "REFERENCE_UNREADABLE"


def test_outline_drawing_is_filled_into_a_silhouette():
    img = np.full((200, 200, 3), 255, np.uint8)
    img[50:150, 50:150] = 0
    img[54:146, 54:146] = 255                       # a hollow square outline, 4px stroke
    assert refdiff.foreground_mask(_png(img)).sum() < 100 * 100 - 90 * 90 + 1      # unfilled: just the ring
    assert refdiff.foreground_mask(_png(img), fill=True).sum() == 100 * 100


def test_fill_holes_fallback_without_scipy(monkeypatch):
    monkeypatch.setitem(sys.modules, "scipy", None)          # makes `from scipy import ndimage` raise ImportError
    monkeypatch.setitem(sys.modules, "scipy.ndimage", None)
    ring = np.zeros((60, 60), bool)
    ring[10:50, 10:50] = True
    ring[15:45, 15:45] = False
    assert refdiff._fill_holes(ring).sum() == 40 * 40


def test_identical_silhouettes_score_one_and_no_regions():
    shape = _rect(60, 240, 80, 320)
    model = refdiff.foreground_mask(_png(_gradient_scene(shape)))
    ref = refdiff.foreground_mask(_png(_gradient_scene(shape)))
    frame = refdiff.frame_from_bbox(model, 240.0, 180.0)             # 1 px == 1 mm
    reg = refdiff.register(ref, model)
    res = refdiff.diff(model, reg.mask, frame, "front")
    assert res.iou == pytest.approx(1.0)
    assert res.regions == []


def test_a_missing_notch_is_found_in_mm_and_on_the_right_edge():
    ref_shape = _rect(60, 240, 80, 320)
    model_shape = lambda ys, xs: _rect(60, 240, 80, 320)(ys, xs) & ~_rect(100, 140, 290, 320)(ys, xs)  # noqa: E731
    model = refdiff.foreground_mask(_png(_gradient_scene(model_shape)))
    ref = refdiff.foreground_mask(_png(_gradient_scene(ref_shape)))
    frame = refdiff.frame_from_bbox(model, 240.0, 180.0)
    res = refdiff.diff(model, refdiff.register(ref, model).mask, frame, "front")
    assert res.iou < 1.0
    assert len(res.regions) == 1
    r = res.regions[0]
    assert r["kind"] == "model_missing"                              # the reference has material the model lacks
    assert r["size_mm"] == [30.0, 40.0]
    assert r["touches_edge_of"] == ["right"]
    assert r["area_mm2"] == pytest.approx(30 * 40, rel=0.02)
    assert r["axes"] == {"u": "+X", "v": "+Y"}
    # u measured from the model bbox's left edge; v from its bottom (image y is down): rows 100..140 of 60..240
    assert r["view_mm"]["u"] == [210.0, 240.0]
    assert r["view_mm"]["v"] == [100.0, 140.0]


def test_extra_material_in_the_model_is_reported_as_model_has_extra():
    ref_shape = lambda ys, xs: _rect(60, 240, 80, 320)(ys, xs) & ~_rect(100, 140, 150, 200)(ys, xs)  # noqa: E731
    model = refdiff.foreground_mask(_png(_gradient_scene(_rect(60, 240, 80, 320))))      # the model filled a slot
    ref = refdiff.foreground_mask(_png(_gradient_scene(ref_shape)))
    frame = refdiff.frame_from_bbox(model, 240.0, 180.0)
    res = refdiff.diff(model, refdiff.register(ref, model).mask, frame, "front")
    assert [r["kind"] for r in res.regions] == ["model_has_extra"]
    assert res.regions[0]["touches_edge_of"] == []                                       # interior, not an edge
    assert res.regions[0]["size_mm"] == [50.0, 40.0]


def test_reference_at_another_scale_and_offset_is_registered():
    model_shape = _rect(60, 240, 80, 320)
    model = refdiff.foreground_mask(_png(_gradient_scene(model_shape)))
    # the same part drawn at half size, elsewhere on a bigger white page
    page = np.full((600, 800, 3), 255, np.uint8)
    page[300:390, 400:520] = 20
    ref = refdiff.foreground_mask(_png(page))
    frame = refdiff.frame_from_bbox(model, 240.0, 180.0)
    reg = refdiff.register(ref, model)
    assert reg.scale == pytest.approx(2.0, rel=0.02)
    assert refdiff.diff(model, reg.mask, frame).iou > 0.98


def test_wrong_proportions_refuse_to_score():
    model = refdiff.foreground_mask(_png(_gradient_scene(_rect(60, 240, 80, 320))))     # 240x180 (4:3)
    page = np.full((300, 400, 3), 255, np.uint8)
    page[50:250, 100:200] = 0                                                           # 100x200 (1:2)
    ref = refdiff.foreground_mask(_png(page))
    with pytest.raises(refdiff.RefDiffError) as e:
        refdiff.register(ref, model)
    assert e.value.code == "REGISTRATION_WEAK"


def test_non_orthographic_capture_is_rejected_by_the_frame():
    model = refdiff.foreground_mask(_png(_gradient_scene(_rect(60, 240, 80, 320))))     # 240x180 px
    with pytest.raises(refdiff.RefDiffError) as e:
        refdiff.frame_from_bbox(model, 240.0, 90.0)                                     # claims 2x the height ratio
    assert e.value.code == "REGISTRATION_WEAK"


def test_view_axes_and_isometric_is_refused():
    assert refdiff.view_extents_mm("top", {"X": 10, "Y": 20, "Z": 30}) == (10.0, 30.0)
    assert refdiff.view_extents_mm("right", {"X": 10, "Y": 20, "Z": 30}) == (30.0, 20.0)
    with pytest.raises(refdiff.RefDiffError) as e:
        refdiff.view_axes("isometric")
    assert e.value.code == "BAD_VIEW"


def test_python_labeller_matches_scipy_for_two_blobs():
    m = np.zeros((20, 20), bool)
    m[2:6, 2:6] = True
    m[10:15, 12:18] = True
    lab, n = refdiff._label_py(m)
    assert n == 2 and set(np.unique(lab)) == {0, 1, 2}


def test_overlay_is_a_png_with_the_three_colours():
    from PIL import Image
    a = np.zeros((40, 40), bool); a[5:25, 5:25] = True
    b = np.zeros((40, 40), bool); b[15:35, 15:35] = True
    png = refdiff.overlay_png(a, b, "front")
    px = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    assert tuple(px[22, 8]) == (40, 90, 220) and tuple(px[30, 30]) == (225, 50, 40) and tuple(px[20, 20]) == (150, 150, 150)


# ---------------------------------------------------------------------------------------------
# the tool end to end, with the execution layer faked
# ---------------------------------------------------------------------------------------------
_FRAME = {"has_perspective": False, "axis_aligned": True, "right": "+X", "up": "+Y"}


def _fake_bridge(model_shape, feature_map=None, bbox=None, frame=_FRAME):
    from PIL import Image
    scene = _gradient_scene(model_shape)

    def call_raw(tool, params):
        if tool == "analyze_model" and params["analysis_type"] == "bbox":
            import json
            b = bbox or {"min": [0, 0, 0], "max": [0.24, 0.18, 0.05], "size": [0.24, 0.18, 0.05]}
            return {"status": "COMPLETED", "cadState": {"features": [json.dumps(b)]}}
        if tool == "analyze_model" and params["analysis_type"] == "feature_map":
            import json
            return {"status": "COMPLETED", "cadState": {"features": [json.dumps({"map": feature_map or []})]}}
        if tool == "export_image":
            import json
            Image.fromarray(scene).save(params["file_path"], format="BMP")
            feats = [params["file_path"]] + (["view_frame=" + json.dumps(frame)] if frame is not None else [])
            return {"status": "COMPLETED", "cadState": {"features": feats}}
        raise AssertionError(f"unexpected call {tool} {params}")
    return call_raw


def _run_tool(tmp_path, ref_shape, model_shape, **kw):
    pytest.importorskip("fitz")
    import server
    from solidpilot import design
    ref = tmp_path / "ref.png"
    ref.write_bytes(_png(_gradient_scene(ref_shape)))
    orig_call, orig_get = server.bridge._call_raw, design.get
    server.bridge._call_raw = _fake_bridge(model_shape, kw.pop("feature_map", None), kw.pop("bbox", None),
                                           kw.pop("frame", _FRAME))
    design.get = lambda *a, **k: None
    try:
        return server.compare_to_reference(str(ref), **kw)
    finally:
        server.bridge._call_raw, design.get = orig_call, orig_get


def test_tool_reports_the_notch_and_names_the_feature_that_made_it(tmp_path):
    import json
    ref_shape = _rect(60, 240, 80, 320)
    model_shape = lambda ys, xs: _rect(60, 240, 80, 320)(ys, xs) & ~_rect(100, 140, 290, 320)(ys, xs)  # noqa: E731
    # the model silhouette spans model x 0..240 mm, y 0..180 mm; the missing notch is u 210..240, v 100..140
    fmap = [{"feature": "Boss-Extrude1", "type": "Extrusion", "created_faces": [{"point": [0.05, 0.05, 0.0]}]},
            {"feature": "Cut-Extrude1", "type": "Cut", "created_faces": [{"point": [0.23, 0.12, 0.02]}]}]
    out = _run_tool(tmp_path, ref_shape, model_shape, view="front", feature_map=fmap)
    data = json.loads(out[0])
    assert data["verdict"] in ("close", "differs") and len(out) == 2
    (r,) = data["regions"]
    assert r["kind"] == "model_missing" and r["size_mm"] == [30.0, 40.0]
    assert r["model_mm"] == {"X": [210.0, 240.0], "Y": [100.0, 140.0]}
    assert [s["feature"] for s in r["suspect_features"]] == ["Cut-Extrude1"]
    assert "no active design session" in data["note"]


def test_tool_matching_silhouettes_is_a_match(tmp_path):
    import json
    shape = _rect(60, 240, 80, 320)
    data = json.loads(_run_tool(tmp_path, shape, shape, view="front")[0])
    assert data["verdict"] == "match" and data["regions"] == []


def test_tool_error_paths_use_registry_style_codes(tmp_path):
    import server
    assert server.compare_to_reference(str(tmp_path / "nope.png"))[0].startswith("FAILED | FILE_NOT_FOUND |")
    bad = _run_tool(tmp_path, _rect(60, 240, 80, 320), _rect(60, 240, 80, 320), view="isometric")
    assert bad[0].startswith("FAILED | BAD_VIEW |")


def test_tool_uses_the_measured_axes_when_the_front_view_was_redefined(tmp_path):
    import json
    ref_shape = _rect(60, 240, 80, 320)
    model_shape = lambda ys, xs: _rect(60, 240, 80, 320)(ys, xs) & ~_rect(100, 140, 290, 320)(ys, xs)  # noqa: E731
    # the user's "Front" looks along X: screen-right is +Z, so the 240 mm width is the part's Z extent
    bbox = {"min": [0, 0, 0], "max": [0.05, 0.18, 0.24], "size": [0.05, 0.18, 0.24]}
    frame = {"has_perspective": False, "axis_aligned": True, "right": "+Z", "up": "+Y"}
    data = json.loads(_run_tool(tmp_path, ref_shape, model_shape, view="front", bbox=bbox, frame=frame)[0])
    (r,) = data["regions"]
    assert r["axes"] == {"u": "+Z", "v": "+Y"}
    assert r["model_mm"] == {"Z": [210.0, 240.0], "Y": [100.0, 140.0]}
    assert "ASSUMED" not in data.get("note", "")


def test_tool_refuses_a_perspective_capture(tmp_path):
    shape = _rect(60, 240, 80, 320)
    out = _run_tool(tmp_path, shape, shape, view="front", frame=dict(_FRAME, has_perspective=True))
    assert out[0].startswith("FAILED | PERSPECTIVE_ON |") and len(out) == 1


def test_tool_refuses_a_view_that_is_not_axis_aligned(tmp_path):
    shape = _rect(60, 240, 80, 320)
    out = _run_tool(tmp_path, shape, shape, view="front", frame={"has_perspective": False, "axis_aligned": False})
    assert out[0].startswith("FAILED | VIEW_NOT_AXIS_ALIGNED |")


def test_tool_falls_back_to_default_axes_and_says_so_when_no_frame_is_reported(tmp_path):
    import json
    shape = _rect(60, 240, 80, 320)
    data = json.loads(_run_tool(tmp_path, shape, shape, view="front", frame=None)[0])
    assert data["verdict"] == "match" and "ASSUMED" in data["note"]


def test_axes_from_frame_rejects_malformed_values():
    f = refdiff.axes_from_frame
    assert f({"right": "+X", "up": "-Z"}) == (("X", 1), ("Z", -1))
    for bad in (None, {}, {"right": "+X"}, {"right": "X", "up": "+Y"}, {"right": "+X", "up": "+X"}, {"right": "", "up": "+Y"},
                {"right": "+W", "up": "+Y"}):
        assert f(bad) is None


# ---------------------------------------------------------------------------------------------
# design_step_hint: templated, never applied
# ---------------------------------------------------------------------------------------------
def _region(kind="model_has_extra", size=(12.0, 30.0), edges=("right",), axes=None, nodes=("e1",)):
    return {"kind": kind, "size_mm": list(size), "touches_edge_of": list(edges),
            "axes": axes or {"u": "+Z", "v": "+Y"},
            "suspect_features": [{"feature": f"F-{n}", "type": "Extrusion", "hits": 3, "design_node": n} for n in nodes]}


def _extrude_nodes(datum="front", depth=0.05, **extra):
    return [{"id": "s1", "type": "sketch", "ref": {"datum": datum}, "profile": []},
            dict({"id": "e1", "type": "extrude", "sketch": "s1", "operation": "boss", "depth": depth}, **extra)]


def test_hint_shortens_an_extrude_whose_end_face_region_is_extra():
    # front datum: the extrude runs along +Z; the view shows Z on its u axis and the region is at the +Z end
    hint = refdiff.design_step_hint(_region(), _extrude_nodes())
    assert hint["suggestion_only"] is True
    assert hint["patch"] == [{"op": "set", "id": "e1", "params": {"depth": 0.038}}]       # 0.050 m - 12 mm
    assert "model_has_extra" in hint["reasoning"] and "12 mm" in hint["reasoning"] and "vision" in hint["reasoning"]


def test_hint_lengthens_when_missing_and_honours_flipped_view_axes_and_reversed():
    nodes = _extrude_nodes(depth=0.05)
    # the view's u axis is -Z, so view-LEFT is the model's +Z end
    r = _region("model_missing", size=(8.0, 30.0), edges=("left",), axes={"u": "-Z", "v": "+Y"})
    assert refdiff.design_step_hint(r, nodes)["patch"][0]["params"] == {"depth": 0.058}
    # a reversed extrude grows toward -Z: its end face is at the LOW end, so a +Z-end region is the sketch end
    assert refdiff.design_step_hint(r, _extrude_nodes(reversed=True)) is None
    low = _region("model_missing", size=(8.0, 30.0), edges=("right",), axes={"u": "-Z", "v": "+Y"})
    assert refdiff.design_step_hint(low, _extrude_nodes(reversed=True))["patch"][0]["params"] == {"depth": 0.058}


def test_hint_for_a_box_edits_its_height_along_the_datum_normal():
    box = {"id": "b1", "type": "box", "ref": {"datum": "top"}, "width": 0.1, "depth": 0.1, "height": 0.04}
    r = _region(size=(20.0, 6.0), edges=("top",), axes={"u": "+X", "v": "+Y"}, nodes=("b1",))
    assert refdiff.design_step_hint(r, [box])["patch"] == [{"op": "set", "id": "b1", "params": {"height": 0.034}}]


def test_hint_is_omitted_whenever_the_mapping_is_ambiguous():
    nodes = _extrude_nodes()
    good = _region()
    assert refdiff.design_step_hint(good, nodes)
    assert refdiff.design_step_hint(dict(good, suspect_features=[]), nodes) is None                       # no suspect
    assert refdiff.design_step_hint(_region(nodes=("e1", "e2")), nodes) is None                           # two nodes
    assert refdiff.design_step_hint(dict(good, touches_edge_of=[]), nodes) is None                        # no bbox edge
    assert refdiff.design_step_hint(dict(good, touches_edge_of=["left", "right"]), nodes) is None
    assert refdiff.design_step_hint(dict(good, touches_edge_of=["top"]), nodes) is None                   # wrong axis
    assert refdiff.design_step_hint(_region(axes={"u": "+X", "v": "+Y"}), nodes) is None                 # Z not shown
    assert refdiff.design_step_hint(good, _extrude_nodes(end="through_all")) is None                      # no depth
    assert refdiff.design_step_hint(good, _extrude_nodes(operation="cut")) is None
    assert refdiff.design_step_hint(good, _extrude_nodes(depth=0.01)) is None                             # would go <= 0
    on_face = _extrude_nodes()
    on_face[0]["ref"] = {"face": {"point": [0, 0, 0]}}
    assert refdiff.design_step_hint(good, on_face) is None
    assert refdiff.design_step_hint(good, [{"id": "e1", "type": "fillet", "radius": 0.001}]) is None
