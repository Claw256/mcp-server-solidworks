"""Offline tests for drawing_prep's inputs and text layer: region filter, paging, line grouping, page
rotation, raster inputs, annotate on images, reliability classification, error codes and the DXF render hook."""
import json
import os
import sys
import types

import pytest

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import drawing_prep as dp  # noqa: E402

try:
    fitz = dp._fitz()
except Exception:  # noqa: BLE001
    pytest.skip("PyMuPDF not installed", allow_module_level=True)


def dim_page(path, rotation=0):
    doc = fitz.open()
    pg = doc.new_page(width=600, height=400)
    pg.draw_rect(fitz.Rect(100, 100, 400, 300), width=1)
    pg.draw_circle(fitz.Point(250, 200), 30, width=1)
    for k in range(6):
        pg.draw_line(fitz.Point(100 + k * 10, 320), fitz.Point(100 + k * 10, 340))
    for t, xy in [("4X Ø8", (120, 90)), ("Ø8H7", (300, 90)), ("SCALE 1:2", (480, 380)), ("R5.5", (420, 150)),
                  ("12.5 ±0.1", (420, 250)), ("NOTE", (20, 30))]:
        pg.insert_text(fitz.Point(*xy), t, fontsize=10)
    if rotation:
        pg.set_rotation(rotation)
    doc.save(path)
    doc.close()


def prep(path, **kw):
    src = dp.open_input(path)
    try:
        return dp.prepare_page(src, **kw)
    finally:
        src.close()


def strs(meta):
    return meta["text_layer"]["items"]


def test_text_layer_groups_lines_keeps_exact_strings_and_boxes(tmp_path):
    pdf = str(tmp_path / "d.pdf")
    dim_page(pdf)
    meta, _ = prep(pdf, dpi=150)
    texts = {s.split("@")[0] for s in strs(meta)}
    assert {"4X Ø8", "Ø8H7", "SCALE 1:2", "R5.5", "12.5 ±0.1"} <= texts
    item = next(s for s in strs(meta) if s.startswith("Ø8H7@"))
    x0, y0, x1, y1 = (float(v) for v in item.split("@")[1].split(","))
    assert 295 < x0 < 305 and x1 > x0 + 20 and 78 < y0 < 92 and y1 > y0
    assert strs(meta)[0].split("@")[0] in ("4X Ø8", "Ø8H7", "R5.5", "12.5 ±0.1", "SCALE 1:2")      # dimension-like first
    assert meta["text_layer_reliability"] == "exact" and meta["scale_guess"]["label"] == "1:2"


def test_text_layer_region_filter_and_paging(tmp_path):
    pdf = str(tmp_path / "d.pdf")
    dim_page(pdf)
    meta, _ = prep(pdf, region="[280,70,360,100]", dpi=200)
    assert [s.split("@")[0] for s in strs(meta)] == ["Ø8H7"] and meta["text_layer"]["total"] == 1
    full, _ = prep(pdf, dpi=150, text_limit=2)
    assert full["text_layer"]["shown"] == 2 and full["text_layer"]["truncated"] and full["text_layer"]["next_offset"] == 2
    nxt, _ = prep(pdf, dpi=150, text_offset=2, text_limit=2)
    assert not set(strs(full)) & set(strs(nxt))
    last, _ = prep(pdf, dpi=150, text_offset=4, text_limit=50)
    assert not last["text_layer"]["truncated"] and full["text_layer"]["total"] == 6


def test_separate_strings_on_one_baseline_stay_separate(tmp_path):
    pdf = str(tmp_path / "d.pdf")
    doc = fitz.open()
    pg = doc.new_page(width=300, height=200)
    pg.insert_text(fitz.Point(20, 50), "100", fontsize=10)
    pg.insert_text(fitz.Point(120, 50), "50", fontsize=10)
    doc.save(pdf)
    doc.close()
    meta, _ = prep(pdf)
    assert {s.split("@")[0] for s in strs(meta)} == {"100", "50"}


@pytest.mark.parametrize("rot", [90, 180, 270])
def test_rotated_page_text_and_tile_coordinates_agree(tmp_path, rot):
    pdf = str(tmp_path / "r.pdf")
    dim_page(pdf, rotation=rot)
    src = dp.open_input(pdf)
    try:
        pg = src.doc[0]
        item = next(i for i in dp.text_items(fitz, pg) if i["t"] == "Ø8H7")
        clip = (item["x0"] - 4, item["y0"] - 4, item["x1"] + 4, item["y1"] + 4)
        assert pg.rect.x0 <= clip[0] and clip[2] <= pg.rect.x1 and clip[3] <= pg.rect.y1
        png, info = dp.render_region(fitz, pg, clip, dpi=300)
        meta, _ = dp.prepare_page(src, region=json.dumps(list(clip)), dpi=300)
        assert meta["rotation"] == rot and [s.split("@")[0] for s in strs(meta)] == ["Ø8H7"]
        pix = fitz.Pixmap(png)
        dark = sum(1 for p in range(0, len(pix.samples), pix.n) if pix.samples[p] < 100)
        assert dark > 20, "the zoom on the mapped coordinates must actually contain the text"
        got = [i for i in dp.text_items(fitz, pg) if i["t"] == "SCALE 1:2"][0]
        assert pg.rect.contains(fitz.Rect(got["x0"], got["y0"], got["x1"], got["y1"]))
        assert item["rot"] == (360 - rot) % 360       # text turns with the page
    finally:
        src.close()


def png_input(path, w=400, h=300):
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h), False)
    pm.set_rect(pm.irect, (255, 255, 255))
    pm.set_rect(fitz.IRect(50, 50, 350, 250), (0, 0, 0))
    pm.set_rect(fitz.IRect(55, 55, 345, 245), (255, 255, 255))
    pm.save(path)


def test_raster_input_has_no_paper_mm_and_reports_source_px(tmp_path):
    png = str(tmp_path / "scan.png")
    png_input(png)
    meta, pngs = prep(png, dpi=300)
    assert meta["source_px"] == [400, 300] and "page_mm_on_paper" not in meta
    assert "dpi_used" not in meta and "px_per_mm_paper" not in meta
    assert all("dpi" not in i and "px_per_mm_paper" not in i for i in meta["images"])
    assert meta["images"][0]["vs_source_px"] <= 1.001           # never upsampled past the source
    assert "no paper size" in meta["note"] and meta["text_layer_reliability"] == "none"


def test_annotate_regions_works_on_a_png(tmp_path):
    png = str(tmp_path / "scan.png")
    png_input(png)
    marks = dp.parse_marks([{"box": [100, 100, 200, 160], "label": "A"}])
    src = dp.open_input(png)
    try:
        meta, out = dp.annotate_page(src, marks, 1)
    finally:
        src.close()
    assert out[:4] == b"\x89PNG" and meta["marks"][0]["label"] == "A" and "px_per_mm_paper" not in meta["image"]
    pix = fitz.Pixmap(out)
    red = sum(1 for p in range(0, len(pix.samples), pix.n) if pix.samples[p] > 200 and pix.samples[p + 1] < 60)
    assert red > 50
    assert os.path.exists(png)


def test_annotate_regions_tool_on_png_and_error_codes(tmp_path):
    server = pytest.importorskip("server")
    png = str(tmp_path / "scan.png")
    png_input(png)
    res = server.annotate_regions(png, json.dumps([{"point": [150, 120], "label": "P"}]))
    assert json.loads(res[0])["marks"][0]["label"] == "P" and len(res) == 2
    txt = str(tmp_path / "x.txt")
    open(txt, "w").write("hi")
    assert server.prepare_drawing(txt)[0].startswith("FAILED | UNSUPPORTED_TYPE")
    assert server.prepare_drawing(str(tmp_path / "missing.pdf"))[0].startswith("FAILED | FILE_NOT_FOUND")


def test_password_pdf_error_code(tmp_path):
    pdf = str(tmp_path / "locked.pdf")
    doc = fitz.open()
    doc.new_page().insert_text((50, 50), "secret")
    doc.save(pdf, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    doc.close()
    with pytest.raises(dp.PrepError) as ei:
        dp.open_input(pdf)
    assert ei.value.code == "PDF_PASSWORD"
    server = pytest.importorskip("server")
    assert server.prepare_drawing(pdf)[0].startswith("FAILED | PDF_PASSWORD")


def test_reader_unavailable_only_when_pymupdf_is_missing(tmp_path, monkeypatch):
    pdf = str(tmp_path / "d.pdf")
    dim_page(pdf)

    def boom():
        raise RuntimeError("PyMuPDF is not installed")
    monkeypatch.setattr(dp, "_fitz", boom)
    with pytest.raises(dp.PrepError) as ei:
        dp.open_input(pdf)
    assert ei.value.code == "PDF_READER_UNAVAILABLE"


def test_corrupt_file_is_unsupported_not_reader_unavailable(tmp_path):
    bad = str(tmp_path / "bad.pdf")
    open(bad, "wb").write(b"not a pdf at all")
    with pytest.raises(dp.PrepError) as ei:
        dp.open_input(bad)
    assert ei.value.code == "UNSUPPORTED_TYPE"


def _scan_page(path, ocr=False, logo=False, outlined=False):
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    if outlined:
        for k in range(120):                                     # text converted to curves: paths, no words
            pg.draw_circle(fitz.Point(40 + (k % 20) * 25, 60 + (k // 20) * 40), 6, width=0.3)
    else:
        pg.draw_rect(fitz.Rect(50, 50, 500, 700), width=1)
        for k in range(6):
            pg.draw_line(fitz.Point(60 + k * 20, 710), fitz.Point(60 + k * 20, 730))
    if outlined:
        pass
    elif ocr:
        pm = fitz.Pixmap(fitz.csGRAY, 300, 400, bytes([230]) * 120000, False)
        pg.insert_image(pg.rect, pixmap=pm)
        for k, w in enumerate(["12.5", "Ø8", "100", "R4"]):
            pg.insert_text(fitz.Point(100, 100 + k * 40), w, fontsize=10, render_mode=3)
    else:
        for k, w in enumerate(["12.5", "Ø8", "100", "R4"]):
            pg.insert_text(fitz.Point(100, 100 + k * 40), w, fontsize=10)
    if logo:
        pm = fitz.Pixmap(fitz.csGRAY, 20, 20, bytes([0]) * 400, False)
        pg.insert_image(fitz.Rect(520, 800, 560, 830), pixmap=pm)
    doc.save(path)
    doc.close()


def test_classification_ocr_outlined_logo(tmp_path):
    cases = {"ocr": dict(ocr=True), "outlined": dict(outlined=True), "logo": dict(logo=True)}
    got = {}
    for name, kw in cases.items():
        pdf = str(tmp_path / (name + ".pdf"))
        _scan_page(pdf, **kw)
        meta, _ = prep(pdf, dpi=100)
        got[name] = (meta["kind"], meta["text_layer_reliability"], meta["note"])
    assert got["ocr"][:2] == ("scan", "ocr") and "OCR" in got["ocr"][2]
    assert got["outlined"][:2] == ("vector", "outlined") and "READ dimensions" in got["outlined"][2]
    assert got["logo"][:2] == ("vector", "exact")               # a tiny logo does not make a vector sheet "mixed"


def test_dxf_hook_uses_resolve_and_merges_exact_text(tmp_path, monkeypatch):
    pdf = str(tmp_path / "rendered.pdf")
    doc = fitz.open()
    pg = doc.new_page(width=300, height=200)
    pg.draw_rect(fitz.Rect(20, 20, 200, 150), width=1)
    for k in range(6):
        pg.draw_line(fitz.Point(20 + k * 5, 170), fitz.Point(20 + k * 5, 190))
    doc.save(pdf)
    doc.close()
    temp = str(tmp_path / "scratch.tmp")
    open(temp, "w").write("x")
    seen = {}

    class FakeRenderError(Exception):
        def __init__(self, code, message):
            super().__init__(message)
            self.code, self.message = code, message

    def resolve(path, workdir=None, bridge=None):
        seen["args"] = (path, bridge)
        if path.endswith("bad.dwg"):
            raise FakeRenderError("DWG_OPEN_FAILED", "cannot open")
        return {"pdf": pdf, "source_kind": "dxf", "text_items": [
            {"t": "Ø25", "x0": 60.0, "y0": 70.0, "x1": 80.0, "y1": 80.0, "rot": 0}],
            "to_source": {"kind": "dxf", "units": "mm", "pt_per_unit": 2.83, "origin_pt": [10, 190], "flip_y": True},
            "warnings": ["entity HATCH not drawn"], "temp_files": [temp]}

    fake = types.ModuleType("drawing_render")
    fake.RenderError, fake.resolve = FakeRenderError, resolve
    monkeypatch.setitem(sys.modules, "drawing_render", fake)
    dxf = str(tmp_path / "part.dxf")
    open(dxf, "w").write("0\nEOF\n")
    sentinel = object()
    src = dp.open_input(dxf, sentinel)
    try:
        meta, pngs = dp.prepare_page(src, dpi=150)
    finally:
        src.close()
    assert seen["args"] == (dxf, sentinel) and not os.path.exists(temp)
    assert "Ø25@60.0,70.0,80.0,80.0" in strs(meta) and meta["text_layer_reliability"] == "exact"
    assert meta["to_source"]["units"] == "mm" and "entity HATCH not drawn" in meta["warnings"] and pngs
    with pytest.raises(dp.PrepError) as ei:
        dp.open_input(str(tmp_path / "bad.dwg"))
    assert ei.value.code == "DWG_OPEN_FAILED" and ei.value.message == "cannot open"
    server = pytest.importorskip("server")
    assert server.prepare_drawing(dxf)[0].startswith("{")
    open(str(tmp_path / "bad.dwg"), "w").write("x")
    assert server.prepare_drawing(str(tmp_path / "bad.dwg"))[0] == "FAILED | DWG_OPEN_FAILED | cannot open"
    out = server.annotate_regions(dxf, json.dumps([{"point": [50, 60], "label": "x"}]))
    assert json.loads(out[0])["to_source"]["flip_y"] is True
