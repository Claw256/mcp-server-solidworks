"""Offline tests for the input-legibility work in drawing_prep: view-aligned tiles at ~300 dpi on A1/A0 sheets,
the tile index and warnings, the byte budget and the closed-form fit. Generated PDFs only (PyMuPDF)."""
import json
import os
import sys

import pytest

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import drawing_prep as dp  # noqa: E402

try:
    fitz = dp._fitz()
except Exception:  # noqa: BLE001
    pytest.skip("PyMuPDF not installed", allow_module_level=True)

MM = 72.0 / 25.4


def sheet(path, w_mm, h_mm, views=3, view_mm=250):
    """A vector sheet: frame, `views` view clusters with dimension text, a title block bottom-right."""
    doc = fitz.open()
    pg = doc.new_page(width=w_mm * MM, height=h_mm * MM)
    pg.draw_rect(fitz.Rect(20, 20, pg.rect.width - 20, pg.rect.height - 20), width=1.4)
    vs = view_mm * MM
    for k in range(views):
        x0, y0 = 120 + k * (vs + 150), 150 + (k % 2) * 120
        pg.draw_rect(fitz.Rect(x0, y0, x0 + vs, y0 + vs * 0.7), width=0.7)
        pg.draw_circle(fitz.Point(x0 + vs / 2, y0 + vs * 0.35), vs * 0.12, width=0.7)
        for j in range(5):
            pg.draw_line(fitz.Point(x0 + j * 30, y0 - 25), fitz.Point(x0 + j * 30, y0 - 5), width=0.35)
        pg.insert_text(fitz.Point(x0 + vs / 2, y0 - 12), "%d" % (100 + k), fontsize=7)
    tb = fitz.Rect(pg.rect.width - 420, pg.rect.height - 140, pg.rect.width - 22, pg.rect.height - 22)
    pg.draw_rect(tb, width=0.7)
    for j, word in enumerate(["TITLE", "SCALE 1:2", "DRAWN", "CHECKED", "MATERIAL", "SHEET 1/1"]):
        pg.insert_text(fitz.Point(tb.x0 + 8 + (j % 3) * 130, tb.y0 + 25 + (j // 3) * 40), word, fontsize=9)
    doc.save(path)
    doc.close()


def prep(path, **kw):
    src = dp.open_input(path)
    try:
        return dp.prepare_page(src, **kw)
    finally:
        src.close()


@pytest.mark.parametrize("w,h", [(841, 594), (1189, 841)])
def test_big_sheets_keep_dpi_and_report_the_tile_index(tmp_path, w, h):
    pdf = str(tmp_path / "s.pdf")
    sheet(pdf, w, h)
    meta, pngs = prep(pdf, dpi=300, max_images=4)
    assert len(pngs) <= 4
    body = [i for i in meta["images"] if not i.get("overview")]
    assert body and all(i["dpi"] >= 250 for i in body), meta["images"]
    assert meta["dpi_used"] >= 250 and meta["px_per_mm_paper"] >= 9.8
    assert meta["images"][0]["overview"] and meta["images"][0]["dpi"] < 150      # the overview is for layout only
    idx = meta["tile_index"]
    assert len(idx) > 4 and all(t["dpi"] >= 250 and len(t["clip"]) == 4 for t in idx)
    assert {t["kind"] for t in idx} >= {"view", "title"}
    assert any("more tile" in w for w in meta["warnings"]) and "tiles=" in meta["warnings"][-1]
    me, mt = dp.tier_limits()
    assert all(dp._fits(i["width"], i["height"], me, mt) for i in meta["images"])
    assert meta["min_text_px"] is not None and meta["min_text_px"] >= 15


def test_tiles_param_pages_through_the_index(tmp_path):
    pdf = str(tmp_path / "s.pdf")
    sheet(pdf, 841, 594)
    meta, _ = prep(pdf, dpi=300)
    n = len(meta["tile_index"])
    meta2, pngs2 = prep(pdf, dpi=300, tiles=json.dumps([n, 2]))
    assert [i["tile"] for i in meta2["images"]] == [n, 2] and len(pngs2) == 2
    assert meta2["images"][0]["clip"] == pytest.approx(meta["tile_index"][n - 1]["clip"], abs=0.2)
    with pytest.raises(dp.PrepError) as ei:
        prep(pdf, dpi=300, tiles="[999]")
    assert ei.value.code == "BAD_TILES"
    meta3, pngs3 = prep(pdf, dpi=300, tiles="[1,2,3,4,5,6]", max_images=2)
    assert len(pngs3) == 2 and any("max_images" in w for w in meta3["warnings"])


def test_a4_page_fits_one_overview_without_tiles(tmp_path):
    pdf = str(tmp_path / "a4.pdf")
    sheet(pdf, 297, 210, views=1, view_mm=90)
    meta, pngs = prep(pdf, dpi=200)
    assert len(pngs) == 1 and meta["images"][0]["dpi"] >= 170 and "tile_index" not in meta


def test_view_regions_finds_views_and_title(tmp_path):
    pdf = str(tmp_path / "s.pdf")
    sheet(pdf, 841, 594)
    doc = fitz.open(pdf)
    regs = dp.view_regions(fitz, doc[0])
    doc.close()
    assert [r["kind"] for r in regs].count("view") == 3 and [r["kind"] for r in regs].count("title") == 1
    assert [r["area_pt2"] for r in regs] == sorted((r["area_pt2"] for r in regs), reverse=True)
    assert all(r["clip"][2] > r["clip"][0] and r["clip"][3] > r["clip"][1] for r in regs)


def test_byte_budget_forces_rerender_instead_of_exceeding_cap(tmp_path):
    np = pytest.importorskip("numpy")
    pdf = str(tmp_path / "noisy.pdf")
    rng = np.random.default_rng(1)
    pm = fitz.Pixmap(fitz.csGRAY, 2400, 1700, rng.integers(0, 256, 2400 * 1700, dtype=np.uint8).tobytes(), False)
    doc = fitz.open()
    pg = doc.new_page(width=840, height=594)
    pg.insert_image(pg.rect, pixmap=pm)
    doc.save(pdf)
    doc.close()
    src = dp.open_input(pdf)
    try:
        pg = src.doc[0]
        big, _ = dp.render_region(fitz, pg, tuple(pg.rect), dpi=300)
        assert len(big) > dp.max_png_bytes(2)                      # unbudgeted, this would be RESULT_TOO_LARGE
        meta, pngs = dp.prepare_page(src, dpi=300, max_images=3)
    finally:
        src.close()
    assert meta["kind"] == "scan" and len(pngs) >= 2
    total = sum(len(p) for p in pngs)
    assert total * 4 / 3 < dp.RELAY_MAX_BYTES - 64 * 1024
    assert any("re-rendered" in w and "relay cap" in w for w in meta["warnings"])
    assert any(i["dpi"] < 300 for i in meta["images"])


def test_render_region_closed_form_fit_and_errors(tmp_path):
    pdf = str(tmp_path / "s.pdf")
    sheet(pdf, 1189, 841)
    doc = fitz.open(pdf)
    me, mt = dp.tier_limits()
    for clip, d in [((0, 0, 3370, 2384), 600), ((0, 0, 3000, 40), 300), ((0, 0, 90, 3000), 600), ((10, 10, 11, 11), 600)]:
        png, info = dp.render_region(fitz, doc[0], clip, dpi=d)
        assert dp._fits(info["width"], info["height"], me, mt), (clip, info)
    with pytest.raises(ValueError):
        dp.render_region(fitz, doc[0], (5, 5, 5, 9))
    with pytest.raises(ValueError):
        dp.render_region(fitz, doc[0], (0, 0, 800, 800), dpi=300, max_bytes=100)
    doc.close()


def test_region_branch_clamps_dpi_and_reports_limits(tmp_path):
    pdf = str(tmp_path / "s.pdf")
    sheet(pdf, 841, 594)
    meta, pngs = prep(pdf, region="[0,0,2384,1684]", dpi=5000)
    assert len(pngs) == 1 and meta["images"][0]["dpi"] <= 600
    assert any("too large for one image" in w for w in meta["warnings"])
    with pytest.raises(dp.PrepError) as ei:
        prep(pdf, region="[5000,5000,6000,6000]")
    assert ei.value.code == "BAD_REGION"


def test_zero_tile_page_is_an_error_not_an_empty_list(tmp_path, monkeypatch):
    pdf = str(tmp_path / "s.pdf")
    sheet(pdf, 841, 594)
    monkeypatch.setattr(dp, "plan_page_tiles", lambda *a, **k: ([], 300.0))
    with pytest.raises(dp.PrepError) as ei:
        prep(pdf, dpi=300)
    assert ei.value.code == "NO_TILE_FITS"


def test_scan_without_vector_regions_falls_back_to_a_grid(tmp_path):
    np = pytest.importorskip("numpy")
    pdf = str(tmp_path / "scan.pdf")
    pm = fitz.Pixmap(fitz.csGRAY, 600, 400, bytes([200]) * (600 * 400), False)
    doc = fitz.open()
    pg = doc.new_page(width=2384, height=1684)
    pg.insert_image(pg.rect, pixmap=pm)
    doc.save(pdf)
    doc.close()
    meta, pngs = prep(pdf, dpi=300)
    assert meta["kind"] == "scan" and {t["kind"] for t in meta["tile_index"]} == {"page"}
    assert meta["text_layer_reliability"] == "none"
