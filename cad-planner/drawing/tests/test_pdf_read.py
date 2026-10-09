"""pdf_read: a vector PDF page rebuilt as a draw-dialect artifact. PDFs are generated in-test with
PyMuPDF; skipped (not failed) when PyMuPDF or ezdxf is not installed.

Run:  python cad-planner/drawing/tests/test_pdf_read.py   |   pytest cad-planner/drawing/tests/test_pdf_read.py
"""
import os
import sys
import tempfile

_PKG = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # cad-planner/
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

try:
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    import ezdxf  # noqa: F401
    HAVE = True
except Exception:  # noqa: BLE001
    HAVE = False

import drawing  # noqa: E402
from drawing import pdf_read  # noqa: E402


def _m(mm):
    return mm * 72.0 / 25.4


def _arrow(page, tip, direction, length=3.5, half=0.6):
    """A filled arrowhead triangle (paper mm, page y-down). direction = unit vector base->tip."""
    ux, uy = direction
    bx, by = tip[0] - ux * length, tip[1] - uy * length
    nx, ny = -uy, ux
    pts = [fitz.Point(_m(tip[0]), _m(tip[1])), fitz.Point(_m(bx + nx * half), _m(by + ny * half)),
           fitz.Point(_m(bx - nx * half), _m(by - ny * half))]
    page.draw_polyline(pts, color=None, fill=(0, 0, 0), closePath=True)


def _drawing(path, k=1.0, scale_text="SCALE 1:1", dim_text="100", with_circle=True, hidden_circle=False,
             extra=None):
    """A 100 x 50 mm plate drawn at paper size 100*k x 50*k, with a Ø10 hole and a horizontal dimension."""
    doc = fitz.open()
    page = doc.new_page(width=_m(420), height=_m(297))
    w = 0.35 * 72 / 25.4
    page.draw_rect(fitz.Rect(_m(10), _m(10), _m(410), _m(287)), width=w)                    # border
    x0, y0, pw, ph = 100.0, 100.0, 100.0 * k, 50.0 * k
    page.draw_rect(fitz.Rect(_m(x0), _m(y0), _m(x0 + pw), _m(y0 + ph)), width=w)             # the plate
    if with_circle:
        page.draw_circle(fitz.Point(_m(x0 + pw / 2), _m(y0 + ph / 2)), _m(5.0 * k), width=w,
                         dashes="[3 2] 0" if hidden_circle else None)
    ydim = y0 + ph + 15
    for x in (x0, x0 + pw):                                                                 # extension lines
        page.draw_line(fitz.Point(_m(x), _m(y0 + ph + 2)), fitz.Point(_m(x), _m(ydim + 3)), width=w)
    page.draw_line(fitz.Point(_m(x0), _m(ydim)), fitz.Point(_m(x0 + pw), _m(ydim)), width=w)  # dimension line
    _arrow(page, (x0, ydim), (-1.0, 0.0))
    _arrow(page, (x0 + pw, ydim), (1.0, 0.0))
    page.insert_text(fitz.Point(_m(x0 + pw / 2 - 3), _m(ydim - 1.5)), dim_text, fontsize=10)
    if with_circle:
        page.insert_text(fitz.Point(_m(x0 + pw / 2 + 8 * k), _m(y0 + ph / 2 - 8 * k)), "Ø10", fontsize=10)
    page.insert_text(fitz.Point(_m(330), _m(278)), scale_text, fontsize=10)
    if extra:
        extra(page)
    doc.save(path)
    doc.close()


def _read(**kw):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = os.path.join(d, "plate.pdf")
        read_kw = {k: kw.pop(k) for k in ("scale",) if k in kw}
        _drawing(p, **kw)
        return drawing.read_pdf(p, drawing.load_config(), **read_kw)


def _plate_view(art):
    return max(art["views"], key=lambda v: v["size"][0] * v["size"][1])


def test_vector_pdf_reads_like_a_dxf():
    if not HAVE:
        print("     (skipped: PyMuPDF / ezdxf not installed)")
        return
    art = _read()
    assert art["sheet"]["source"] == "pdf" and art["sheet"]["pdf"]["scale"] == "1:1"
    v = _plate_view(art)
    assert abs(v["size"][0] - 100.0) < 0.1 and abs(v["size"][1] - 50.0) < 0.1, v["size"]
    assert len(v["geometry"]["circles"]) == 1 and abs(v["geometry"]["circles"][0]["d"] - 10.0) < 0.1
    lin = [d for d in art["dimensions"] if d["kind"] in ("linear", "aligned")]
    assert lin and abs(lin[0]["value"] - 100.0) < 0.1 and not lin[0].get("printed_mismatch"), art["dimensions"]
    dia = [d for d in art["dimensions"] if d["kind"] == "diameter"]
    assert dia and abs(dia[0]["value"] - 10.0) < 0.1 and dia[0].get("measures"), art["dimensions"]
    assert art["sheet"]["pdf"]["dimensions_made"]["linear"] == 1
    assert art["sheet"]["pdf"]["dimensions_made"]["diameter"] == 1
    assert not art["sheet"]["pdf"]["unassigned_numbers"]


def test_title_block_scale_is_applied_to_geometry_and_dimensions():
    if not HAVE:
        print("     (skipped: PyMuPDF / ezdxf not installed)")
        return
    art = _read(k=0.5, scale_text="SCALE 1:2")          # drawn HALF size on paper, true size 100 x 50
    assert art["sheet"]["pdf"]["scale"] == "1:2" and art["sheet"]["pdf"]["scale_source"] == "title block text"
    v = _plate_view(art)
    assert abs(v["size"][0] - 100.0) < 0.2 and abs(v["size"][1] - 50.0) < 0.2, v["size"]
    lin = [d for d in art["dimensions"] if d["kind"] in ("linear", "aligned")]
    assert lin and abs(lin[0]["value"] - 100.0) < 0.2 and not lin[0].get("printed_mismatch"), art["dimensions"]
    assert abs(v["geometry"]["circles"][0]["d"] - 10.0) < 0.2


def test_missing_scale_text_is_assumed_and_said_so():
    if not HAVE:
        print("     (skipped: PyMuPDF / ezdxf not installed)")
        return
    art = _read(scale_text="SHEET 1 OF 1")
    assert art["sheet"]["pdf"]["scale"] == "1:1" and "ASSUMED" in art["sheet"]["pdf"]["scale_source"]
    forced = _read(k=0.5, scale_text="SHEET 1 OF 1", scale="1:2")
    assert forced["sheet"]["pdf"]["scale_source"] == "argument"
    assert abs(_plate_view(forced)["size"][0] - 100.0) < 0.2


def test_dashed_circle_becomes_a_hidden_edge_and_wrong_dimension_is_flagged():
    if not HAVE:
        print("     (skipped: PyMuPDF / ezdxf not installed)")
        return
    art = _read(hidden_circle=True)
    assert _plate_view(art)["geometry"]["circles"][0]["c"] == "hidden"
    bad = _read(dim_text="120")                          # the printed number disagrees with the geometry
    lin = [d for d in bad["dimensions"] if d["kind"] in ("linear", "aligned")]
    assert lin and lin[0].get("printed_mismatch") is True, bad["dimensions"]


def _segs(page, y, x_start, pattern, vertical=False):
    """Separate solid segments along a line. pattern = [(length, gap_after), ...] in paper mm."""
    w = 0.35 * 72 / 25.4
    x = x_start
    for ln, gap in pattern:
        a, b = ((_m(y), _m(x)), (_m(y), _m(x + ln))) if vertical else ((_m(x), _m(y)), (_m(x + ln), _m(y)))
        page.draw_line(fitz.Point(*a), fitz.Point(*b), width=w)
        x += ln + gap


def _plate_lines(art):
    return _plate_view(art)["geometry"]["lines"]


def test_baked_hidden_dashes_merge_into_one_hidden_line():
    if not HAVE:
        print("     (skipped: PyMuPDF / ezdxf not installed)")
        return
    # 4 mm dashes with 2 mm gaps edge to edge, 3 mm inside the top edge (an isolated floating line would not join the view cluster) (plate is x 100..200, y 100..150)
    art = _read(extra=lambda pg: _segs(pg, 103.0, 100.0, [(4.0, 2.0)] * 16 + [(4.0, 0.0)]))
    hid = [ln for ln in _plate_lines(art) if ln.get("c") == "hidden"]
    assert len(hid) == 1, _plate_lines(art)
    assert art["sheet"]["pdf"]["dashes_merged"] == 1 and art["sheet"]["pdf"]["dash_segments_merged"] == 17
    base = _read()
    assert len(_plate_lines(base)) + 1 == len(_plate_lines(art))


def test_baked_long_short_dashes_become_a_center_line():
    if not HAVE:
        print("     (skipped: PyMuPDF / ezdxf not installed)")
        return
    art = _read(extra=lambda pg: _segs(pg, 125.0, 95.0, [(14.0, 1.5), (2.0, 1.5)] * 4 + [(14.0, 0.0)], False))
    cen = [ln for ln in _plate_lines(art) if ln.get("c") == "center"]
    assert len(cen) == 1, _plate_lines(art)
    assert art["sheet"]["pdf"]["dashes_merged"] == 1 and art["sheet"]["pdf"]["dash_segments_merged"] == 9


def test_baked_dash_negatives_stay_untouched():
    if not HAVE:
        print("     (skipped: PyMuPDF / ezdxf not installed)")
        return
    irregular = [(3.0, 1.0), (5.0, 3.5), (2.0, 0.6), (6.0, 2.5), (3.0, 0.0)]
    art = _read(extra=lambda pg: _segs(pg, 110.0, 110.0, irregular))
    assert art["sheet"]["pdf"]["dashes_merged"] == 0
    assert not [ln for ln in _plate_lines(art) if ln.get("c") in ("hidden", "center")]

    def hatch(pg):                                      # 8 parallel 45-degree lines, 2 mm apart
        w = 0.35 * 72 / 25.4
        for i in range(8):
            pg.draw_line(fitz.Point(_m(110 + 2 * i), _m(140)), fitz.Point(_m(115 + 2 * i), _m(135)), width=w)
    art = _read(extra=hatch)
    assert art["sheet"]["pdf"]["dashes_merged"] == 0
    assert not [ln for ln in _plate_lines(art) if ln.get("c") in ("hidden", "center")]

    def ticks(pg):                                      # perpendicular tick marks along a line (not collinear)
        w = 0.35 * 72 / 25.4
        for i in range(8):
            pg.draw_line(fitz.Point(_m(110 + 6 * i), _m(105)), fitz.Point(_m(110 + 6 * i), _m(108)), width=w)
    assert _read(extra=ticks)["sheet"]["pdf"]["dashes_merged"] == 0


def test_scan_like_page_is_refused():
    if not HAVE:
        print("     (skipped: PyMuPDF / ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = os.path.join(d, "blank.pdf")
        doc = fitz.open()
        doc.new_page(width=_m(210), height=_m(297)).insert_text(fitz.Point(50, 50), "just text", fontsize=10)
        doc.save(p)
        doc.close()
        try:
            drawing.read_pdf(p, drawing.load_config())
        except ValueError as ex:
            assert "prepare_drawing" in str(ex)
        else:
            raise AssertionError("a page without vector geometry must be refused")


def _two_pen_drawing(path):
    """A CAD-style page: the 100 x 50 plate (+ a Ø10 hole and a tangent-edge double line hugging the outline) in the
    HEAVY pen (0.18 mm), and 'outlined text' glyph strokes + a dimension line in the THIN pen (0.08 mm)."""
    doc = fitz.open()
    page = doc.new_page(width=_m(420), height=_m(297))
    heavy, thin = 0.18 * 72 / 25.4, 0.08 * 72 / 25.4
    page.draw_rect(fitz.Rect(_m(10), _m(10), _m(410), _m(287)), width=0.42 * 72 / 25.4)      # sheet border
    page.draw_rect(fitz.Rect(_m(100), _m(100), _m(200), _m(150)), width=heavy)                # plate outline
    page.draw_rect(fitz.Rect(_m(100.6), _m(100.6), _m(199.4), _m(149.4)), width=heavy)        # tangent-edge line
    page.draw_circle(fitz.Point(_m(150), _m(125)), _m(5), width=heavy)
    page.draw_circle(fitz.Point(_m(120), _m(125)), _m(3), width=heavy)
    page.draw_circle(fitz.Point(_m(180), _m(125)), _m(3), width=heavy)
    for i in range(40):                                                                       # glyph-like clutter
        x = 100 + 2.0 * i
        page.draw_line(fitz.Point(_m(x), _m(160)), fitz.Point(_m(x + 0.8), _m(163)), width=thin)
        page.draw_line(fitz.Point(_m(x + 0.8), _m(163)), fitz.Point(_m(x + 1.6), _m(160)), width=thin)
    page.draw_line(fitz.Point(_m(100), _m(158)), fitz.Point(_m(200), _m(158)), width=thin)
    doc.save(path)
    doc.close()


def test_pen_filter_removes_dimension_clutter_and_suggests_the_part_pen():
    if not HAVE:
        print("     (skipped: PyMuPDF / ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = os.path.join(d, "twopen.pdf")
        _two_pen_drawing(p)
        cfg = drawing.load_config()
        mixed = drawing.read_pdf(p, cfg)
        pdf = mixed["sheet"]["pdf"]
        assert pdf["pen_used_mm"] is None and pdf["pen_suggested_mm"] == 0.18, pdf["pens"]
        clean = drawing.read_pdf(p, cfg, pen_mm=0.18)
        views = [v for v in clean["views"] if max(v["size"]) > 20]
        assert len(views) == 1 and abs(views[0]["size"][0] - 100.0) < 0.2 and abs(views[0]["size"][1] - 50.0) < 0.2, \
            [v["size"] for v in clean["views"]]
        assert clean["sheet"]["pdf"]["pen_used_mm"] == 0.18


def test_silhouette_ignores_tangent_lines_and_interior_detail():
    if not HAVE:
        print("     (skipped: PyMuPDF / ezdxf not installed)")
        return
    from drawing import silhouette
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = os.path.join(d, "twopen.pdf")
        _two_pen_drawing(p)
        pdf = fitz.open(p)
        page = pdf[0]
        K = 25.4 / 72.0
        r = silhouette.view_silhouette(fitz, page, (_m(95) / 1.0, _m(95), _m(205), _m(155)), 0.18)
        pdf.close()
        assert r is not None
        w, h = r["size_mm"]
        assert abs(w - 100.0) < 0.4 and abs(h - 50.0) < 0.4, r["size_mm"]
        assert 4 <= len(r["polygon"]) <= 12, len(r["polygon"])         # a rectangle, not a ring of detail


_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for t in _TESTS:
        try:
            t()
            print("ok   -", t.__name__)
        except Exception as ex:  # noqa: BLE001
            import traceback
            failed += 1
            print("FAIL -", t.__name__, "::", type(ex).__name__, ex)
            traceback.print_exc()
    print("\n%d/%d passed" % (len(_TESTS) - failed, len(_TESTS)))
    sys.exit(1 if failed else 0)
