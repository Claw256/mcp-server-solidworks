"""Offline tests for drawing_prep / prepare_drawing / export_image.

The size maths and BMP->PNG need nothing; the PDF cases need PyMuPDF and the tool cases need the
adapter's dependencies -- both are SKIPPED (not failed) when missing.
Run:  python adapters/claude/tests/test_drawing_prep.py   |   pytest adapters/claude/tests/test_drawing_prep.py
"""
import math
import os
import struct
import sys
import tempfile
import zlib

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import drawing_prep as dp  # noqa: E402

_PNG_SIG = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])


def _have_fitz():
    try:
        dp._fitz()
        return True
    except Exception:  # noqa: BLE001
        return False


def test_resized_size_matches_vision_doc_examples():
    std = dp.TIERS["standard"]
    hi = dp.TIERS["high"]
    assert dp.resized_size(1920, 1080, *std) == (1456, 819)       # doc table, standard tier
    assert dp.resized_size(1075, 1520, *std) == (924, 1307)       # doc A4-scan example
    assert dp.resized_size(1075, 1520, *hi) == (1075, 1520)       # fits on the high-res tier
    assert dp.resized_size(3840, 2160, *hi) == (2576, 1449)       # doc table, high-res tier
    assert dp.resized_size(200, 200, *std) == (200, 200)


def test_tier_default_and_env_override():
    old = os.environ.pop("SOLIDPILOT_IMAGE_TIER", None)
    try:
        assert dp.tier_limits() == (2576, 4784)
        os.environ["SOLIDPILOT_IMAGE_TIER"] = "standard"
        assert dp.tier_limits() == (1568, 1568)
    finally:
        os.environ.pop("SOLIDPILOT_IMAGE_TIER", None)
        if old is not None:
            os.environ["SOLIDPILOT_IMAGE_TIER"] = old


def _make_pdf(path, w_mm=420, h_mm=297):
    fitz = dp._fitz()
    doc = fitz.open()
    page = doc.new_page(width=w_mm * 72 / 25.4, height=h_mm * 72 / 25.4)
    page.draw_rect(fitz.Rect(100, 100, 400, 300), width=1)
    page.draw_circle(fitz.Point(250, 200), 20, width=1)
    for i in range(6):
        page.draw_line(fitz.Point(100 + i * 10, 320), fitz.Point(100 + i * 10, 340))
    page.insert_text(fitz.Point(240, 90), "100", fontsize=10)
    page.insert_text(fitz.Point(410, 200), "50", fontsize=10)
    page.insert_text(fitz.Point(300, 260), "4X 8", fontsize=10)
    doc.save(path)
    doc.close()


def test_pdf_render_fits_limits_and_text_layer_is_exact():
    if not _have_fitz():
        print("     (skipped: PyMuPDF not installed)")
        return
    fitz = dp._fitz()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        pdf = os.path.join(d, "a3.pdf")
        _make_pdf(pdf)
        _f, doc = dp.open_source(pdf)
        try:
            page = doc[0]
            assert dp.classify_page(page) == "vector"
            items, total, trunc = dp.text_layer(page)
            texts = {i["t"] for i in items}
            assert {"100", "50"} <= texts and total >= 3 and not trunc
            rect = [page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y1]
            for tier in ("high", "standard"):
                tiles, used = dp.plan_tiles_auto(rect, 200, tier)
                assert tiles and used > 40, "an A3 sheet must tile on the %s tier" % tier
                for clip in tiles:
                    png, info = dp.render_region(fitz, page, clip, tier=tier, dpi=used)
                    me, mt = dp.tier_limits(tier)
                    assert dp._fits(info["width"], info["height"], me, mt), (tier, info)
                    assert png[:8] == _PNG_SIG
            tiles, _used = dp.plan_tiles_auto(rect, 200, "standard")
            assert min(t[0] for t in tiles) == rect[0] and max(t[2] for t in tiles) == rect[2]
            assert min(t[1] for t in tiles) == rect[1] and max(t[3] for t in tiles) == rect[3]
        finally:
            doc.close()


def test_zoom_region_maps_back_to_page_points():
    if not _have_fitz():
        print("     (skipped: PyMuPDF not installed)")
        return
    fitz = dp._fitz()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        pdf = os.path.join(d, "a3.pdf")
        _make_pdf(pdf)
        _f, doc = dp.open_source(pdf)
        try:
            png, info = dp.render_region(fitz, doc[0], (200, 80, 300, 110), dpi=200)
            scale = info["px_per_pt"]
            assert abs(info["width"] - math.ceil(100 * scale)) <= 1
            assert abs(scale - 200 / 72) < 1e-3                  # small region: full requested dpi
        finally:
            doc.close()


def _bmp24(w, h, px):
    """px[y][x] = (r, g, b), written bottom-up like SaveBMP."""
    stride = ((w * 24 + 31) // 32) * 4
    body = b""
    for y in range(h - 1, -1, -1):
        row = b"".join(bytes((b, g, r)) for (r, g, b) in px[y])
        body += row + bytes(stride - len(row))
    hdr = struct.pack("<2sIHHI", b"BM", 54 + len(body), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(body), 2835, 2835, 0, 0)
    return hdr + info + body


def _png_pixels(png):
    assert png[:8] == _PNG_SIG
    pos, idat, w, h = 8, b"", None, None
    while pos < len(png):
        n, tag = struct.unpack_from(">I4s", png, pos)
        body = png[pos + 8:pos + 8 + n]
        assert struct.unpack_from(">I", png, pos + 8 + n)[0] == (zlib.crc32(tag + body) & 0xFFFFFFFF)
        if tag == b"IHDR":
            w, h = struct.unpack_from(">II", body)
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    rows = [raw[y * (1 + 3 * w) + 1:(y + 1) * (1 + 3 * w)] for y in range(h)]
    return w, h, [[tuple(r[x * 3:x * 3 + 3]) for x in range(w)] for r in rows]


def test_bmp_to_png_round_trips_pixels_and_orientation():
    px = [[(255, 0, 0), (0, 255, 0), (0, 0, 255)], [(10, 20, 30), (40, 50, 60), (70, 80, 90)]]
    w, h, got = _png_pixels(dp.bmp_to_png(_bmp24(3, 2, px)))
    assert (w, h) == (3, 2) and got == px            # row order and R/B channel order preserved
    try:
        dp.bmp_to_png(b"not a bmp")
    except ValueError:
        pass
    else:
        raise AssertionError("garbage must be rejected")


def test_export_image_tool_returns_png_sized_to_limits():
    try:
        import server
        from mcp.server.mcpserver.utilities.types import Image
    except Exception:  # noqa: BLE001
        print("     (skipped: adapter deps not installed)")
        return
    calls = []

    def fake_call_raw(tool, params):
        calls.append((tool, dict(params)))
        with open(params["file_path"], "wb") as fh:
            fh.write(_bmp24(4, 4, [[(1, 2, 3)] * 4] * 4))
        return {"status": "COMPLETED"}

    real = server._call_raw
    server._call_raw = fake_call_raw
    try:
        res = server.export_image(view="isometric,front", width=4000, height=4000)
    finally:
        server._call_raw = real
    assert isinstance(res[0], str) and len(res) == 3 and all(isinstance(r, Image) for r in res[1:]), res
    me, mt = dp.tier_limits()
    for _t, prm in calls:
        assert dp._fits(prm["width"], prm["height"], me, mt) and not os.path.exists(prm["file_path"])
    assert [c[1]["view"] for c in calls] == ["isometric", "front"]


def test_annotate_draws_marks_where_the_page_coordinates_say():
    if not _have_fitz():
        print("     (skipped: PyMuPDF not installed)")
        return
    fitz = dp._fitz()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        pdf = os.path.join(d, "a3.pdf")
        _make_pdf(pdf)
        _f, doc = dp.open_source(pdf)
        try:
            marks = [{"box": [150, 150, 250, 250], "label": "A"}, {"point": [320, 200], "label": "B"}]
            png, info, resolved = dp.annotate_region(fitz, doc[0], dp.parse_marks(marks),
                                                     clip=(100, 100, 400, 300), dpi=100)
            w, h, px = _png_pixels(png)
            s = info["px_per_pt"]
            assert (w, h) == (info["width"], info["height"])

            def red(x, y, r=3):             # any strongly red pixel within r px
                return any(px[yy][xx][0] > 200 and px[yy][xx][1] < 80 and px[yy][xx][2] < 80
                           for yy in range(max(0, int(y) - r), min(h, int(y) + r + 1))
                           for xx in range(max(0, int(x) - r), min(w, int(x) + r + 1)))
            assert red((150 - 100) * s, (200 - 100) * s), "left edge of box A"
            assert red((250 - 100) * s, (200 - 100) * s), "right edge of box A"
            assert red((320 - 100) * s, (200 - 100) * s), "centre of point B"
            assert resolved[0]["px"] == [round((150 - 100) * s, 1), round((150 - 100) * s, 1),
                                         round((250 - 100) * s, 1), round((250 - 100) * s, 1)]
        finally:
            doc.close()
    for bad in ([], "x", [{"label": "no geometry"}], [{"box": [1, 2, 3]}]):
        try:
            dp.parse_marks(bad)
        except ValueError:
            continue
        raise AssertionError("parse_marks must reject %r" % (bad,))


def test_annotate_regions_tool_end_to_end():
    if not _have_fitz():
        print("     (skipped: PyMuPDF not installed)")
        return
    try:
        import json
        import server
        from mcp.server.mcpserver.utilities.types import Image
    except Exception:  # noqa: BLE001
        print("     (skipped: adapter deps not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        pdf = os.path.join(d, "a3.pdf")
        _make_pdf(pdf)
        res = server.annotate_regions(pdf, json.dumps([{"box": [230, 78, 262, 92], "label": "100"}]))
        meta = json.loads(res[0])
        assert isinstance(res[1], Image) and meta["marks"][0]["label"] == "100" and meta["image"]["width"] > 0
        bad = server.annotate_regions(pdf, "not json")
        assert bad[0].startswith("FAILED | BAD_MARKS")
        assert server.annotate_regions(pdf, "[]")[0].startswith("FAILED | BAD_MARKS")
        assert server.annotate_regions(pdf, '[{"point":[1,1]}]', page=9)[0].startswith("FAILED | BAD_PAGE")


_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for t in _TESTS:
        try:
            t()
            print("ok   -", t.__name__)
        except Exception as ex:  # noqa: BLE001
            failed += 1
            print("FAIL -", t.__name__, "::", type(ex).__name__, ex)
    print("\n%d/%d passed" % (len(_TESTS) - failed, len(_TESTS)))
    sys.exit(1 if failed else 0)
