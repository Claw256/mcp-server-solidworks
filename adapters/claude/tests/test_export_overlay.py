"""export_image's style/section/zoom_to/highlight pass-through and its adapter-side grid / index labels, with the
execution layer faked (no SolidWorks)."""
import io
import json
import os
import sys

import numpy as np
import pytest

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

pytest.importorskip("PIL")
from PIL import Image as PilImage  # noqa: E402

server = pytest.importorskip("server")

FRAME = {"has_perspective": False, "axis_aligned": True, "right": "+X", "up": "+Y"}
BBOX = {"min": [0, 0, 0], "max": [0.24, 0.18, 0.05], "size": [0.24, 0.18, 0.05]}
FACES = {"faces": [{"i": 0, "planar": True, "normal": [0, 0, 1], "point": [0.12, 0.09, 0.05], "area": 0.04},
                   {"i": 1, "planar": True, "normal": [0, 0, -1], "point": [0.12, 0.09, 0.0], "area": 0.04}]}
EDGES = {"edges": [{"i": 0, "start": [0, 0, 0.05], "end": [0.24, 0, 0.05], "mid": [0.12, 0.0, 0.05]}]}


def _scene():
    """400x300 gradient background with a 240x180 part: front view at 1 px/mm, origin pixel (80, 240)."""
    img = np.zeros((300, 400, 3), dtype=np.uint8)
    for y in range(300):
        img[y, :, :] = 200 + int(40 * y / 300)
    img[60:240, 80:320] = (70, 80, 90)
    return img


class FakeBridge:
    def __init__(self, frame=FRAME, features_extra=(), fail_analysis=False):
        self.calls, self.frame, self.extra, self.fail = [], frame, list(features_extra), fail_analysis

    def __call__(self, tool, params):
        self.calls.append((tool, dict(params)))
        if tool == "analyze_model":
            if self.fail:
                return {"status": "FAILED", "error": {"code": "NO_SOLID", "message": "no body"}}
            payload = {"bbox": BBOX, "faces": FACES, "edges": EDGES}[params["analysis_type"]]
            return {"status": "COMPLETED", "cadState": {"features": [json.dumps(payload)]}}
        assert tool == "export_image"
        PilImage.fromarray(_scene()).save(params["file_path"], format="BMP")
        feats = [params["file_path"]] + (["view_frame=" + json.dumps(self.frame)] if self.frame is not None else [])
        return {"status": "COMPLETED", "cadState": {"features": feats + self.extra}}


def _run(monkeypatch, bridge, **kw):
    monkeypatch.setattr(server.bridge, "_call_raw", bridge)
    return server.export_image(width=400, height=300, **kw)


def _pixels(image):
    return np.asarray(PilImage.open(io.BytesIO(image.data)).convert("RGB"), dtype=int)


def test_plain_call_sends_no_new_params_and_draws_nothing(monkeypatch):
    b = FakeBridge()
    res = _run(monkeypatch, b, view="front")
    (tool, params), = b.calls
    assert set(params) == {"view", "width", "height", "file_path"} and "overlay" not in res[0]
    assert np.array_equal(_pixels(res[1])[60:240, 80:320], _scene()[60:240, 80:320].astype(int))


def test_style_section_zoom_highlight_are_forwarded_as_json_to_the_execution_layer(monkeypatch):
    b = FakeBridge()
    _run(monkeypatch, b, view="front", style="wireframe", section='{"plane": "front", "offset_mm": 5}',
         zoom_to='{"face": 3}', highlight='{"faces": [1, 2], "features": ["Boss-Extrude1"]}')
    (_, p), = b.calls
    assert p["style"] == "wireframe" and json.loads(p["section"]) == {"plane": "front", "offset_mm": 5}
    assert json.loads(p["zoom_to"]) == {"face": 3} and json.loads(p["highlight"])["faces"] == [1, 2]


@pytest.mark.parametrize("kw", [{"style": "glossy"}, {"labels": "all"}, {"section": "not json"},
                                {"section": '{"depth": 1}'}, {"zoom_to": "[1]"}, {"highlight": '{"faces": [1]'}])
def test_bad_arguments_fail_before_anything_is_captured(monkeypatch, kw):
    b = FakeBridge()
    res = _run(monkeypatch, b, view="front", **kw)
    assert len(res) == 1 and res[0].startswith("FAILED | INVALID_PARAMETER |") and b.calls == []


def test_face_labels_are_drawn_where_the_face_projects(monkeypatch):
    plain = _pixels(_run(monkeypatch, FakeBridge(), view="front")[1])
    res = _run(monkeypatch, FakeBridge(), view="front", labels="faces")
    assert "overlay: labels f1 faces / e0 edges" in res[0]                  # f1 faces away from the viewer
    img = _pixels(res[1])
    assert img.shape == plain.shape
    assert img[142:158, 190:210].mean() > plain[142:158, 190:210].mean() + 40     # white label box at (200, 150)


def test_grid_and_edge_labels_report_what_they_drew(monkeypatch):
    res = _run(monkeypatch, FakeBridge(), view="front", grid=True, labels="edges")
    assert "grid 50 mm (model coordinates)" in res[0] and "e1 edges" in res[0]
    plain = _pixels(_run(monkeypatch, FakeBridge(), view="front")[1])
    assert (_pixels(res[1]) != plain).any(axis=2)[:, 180].any()               # the x = 100 mm grid line


def test_saved_file_png_carries_the_overlay_too(monkeypatch, tmp_path):
    target = str(tmp_path / "shot.bmp")
    _run(monkeypatch, FakeBridge(), view="front", labels="faces", file_path=target)
    saved = np.asarray(PilImage.open(str(tmp_path / "shot.png")).convert("RGB"), dtype=int)
    assert saved[142:158, 190:210].mean() > _scene()[142:158, 190:210].mean() + 40


def test_zoom_or_section_skips_the_overlay_with_a_note(monkeypatch):
    res = _run(monkeypatch, FakeBridge(), view="front", labels="faces", zoom_to='{"face": 0}')
    assert "grid/labels skipped: zoom_to / section" in res[0] and len(res) == 2
    assert np.array_equal(_pixels(res[1])[60:240, 80:320], _scene()[60:240, 80:320].astype(int))


@pytest.mark.parametrize("frame,fragment", [
    ({"has_perspective": True, "axis_aligned": True, "right": "+X", "up": "+Y"}, "perspective"),
    ({"has_perspective": False, "axis_aligned": False}, "not aligned with a model axis"),
])
def test_unusable_views_degrade_to_the_plain_image(monkeypatch, frame, fragment):
    res = _run(monkeypatch, FakeBridge(frame=frame), view="front", grid=True)
    assert fragment in res[0] and "FAILED" not in res[0]
    assert np.array_equal(_pixels(res[1])[60:240, 80:320], _scene()[60:240, 80:320].astype(int))


def test_isometric_without_a_frame_is_skipped_not_failed(monkeypatch):
    res = _run(monkeypatch, FakeBridge(frame=None), view="isometric", labels="faces")
    assert "grid/labels skipped" in res[0] and len(res) == 2


def test_model_read_failure_degrades_to_the_plain_image(monkeypatch):
    res = _run(monkeypatch, FakeBridge(fail_analysis=True), view="front", labels="faces")
    assert "grid/labels skipped" in res[0] and "NO_SOLID" in res[0] and len(res) == 2


def test_missing_pillow_stack_degrades_with_a_one_line_note(monkeypatch):
    monkeypatch.setitem(sys.modules, "refdiff", None)               # `import refdiff` now raises ImportError
    res = _run(monkeypatch, FakeBridge(), view="front", grid=True)
    assert "need numpy + Pillow" in res[0] and len(res) == 2 and "FAILED" not in res[0]


def test_capture_warnings_from_the_execution_layer_are_surfaced(monkeypatch):
    b = FakeBridge(features_extra=['capture_warnings=["the original view orientation/zoom could not be restored"]'])
    res = _run(monkeypatch, b, view="front")
    assert "capture warnings: the original view orientation/zoom could not be restored" in res[0]


def test_execution_failure_is_reported_with_its_code(monkeypatch):
    def failing(tool, params):
        return {"status": "FAILED", "error": {"code": "INVALID_PARAMETER", "message": "face index 99 out of range"}}
    res = _run(monkeypatch, failing, view="front", highlight='{"faces": [99]}')
    assert res == ["FAILED | EXPORT_IMAGE_FAILED | front: INVALID_PARAMETER: face index 99 out of range"]
