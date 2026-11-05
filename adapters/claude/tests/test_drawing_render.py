"""Offline tests for drawing_render (generated DXFs; fake bridge, never a real one)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ezdxf = pytest.importorskip("ezdxf")
fitz = pytest.importorskip("fitz")
import drawing_render as dr  # noqa: E402


def _make(path, with_text=True):
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_line((0, 0), (200, 0))
    msp.add_line((200, 0), (200, 100))
    msp.add_arc((50, 50), 20, 0, 90)
    msp.add_circle((120, 60), 15)
    msp.add_lwpolyline([(0, 100), (200, 100), (200, 0), (0, 0)], close=True)
    if with_text:
        msp.add_text("HELLO", height=5, dxfattribs={"insert": (20, 80)})
        msp.add_mtext("Note A", dxfattribs={"insert": (140, 90), "char_height": 4})
        msp.add_linear_dim(base=(0, -15), p1=(0, 0), p2=(200, 0)).render()
        blk = doc.blocks.new("TB")
        blk.add_attdef("PARTNO", (0, 0), height=3)
        ins = msp.add_blockref("TB", (150, 20))
        ins.add_attrib("PARTNO", "AB-123", (150, 20), dxfattribs={"height": 3})
    doc.saveas(path)
    return path


@pytest.fixture
def dxf(tmp_path):
    return _make(str(tmp_path / "p.dxf"))


def _ro(r):
    return r


def test_dxf_geometry_and_mapping(dxf, tmp_path):
    r = dr.resolve(dxf, workdir=str(tmp_path / "w"))
    assert set(r) == {"pdf", "source_kind", "text_items", "to_source", "warnings", "temp_files"}
    assert r["source_kind"] == "dxf" and r["pdf"] in r["temp_files"]
    ts = r["to_source"]
    assert ts["kind"] == "dxf" and ts["units"] == "mm" and ts["flip_y"] is True
    with fitz.open(r["pdf"]) as pdf:
        assert pdf.page_count == 1
        pg = pdf[0]
        # extents ~ x 0..200, y -15..100 (dimension text may extend a little): width ~ 200*s + margins
        assert abs(ts["pt_per_unit"] - 72 / 25.4) < 0.5
        assert pg.rect.width >= 200 * ts["pt_per_unit"] + 2 * 5 * 72 / 25.4 - 1
        pix = pg.get_pixmap(dpi=144)
        pw = pg.rect.width
    k = pix.width / pw
    # circle (120,60) r15: points on the rim must be dark pixels
    import math
    hits = 0
    for deg in range(0, 360, 30):
        x = 120 + 15 * math.cos(math.radians(deg))
        y = 60 + 15 * math.sin(math.radians(deg))
        px = ts["origin_pt"][0] + x * ts["pt_per_unit"]
        py = ts["origin_pt"][1] - y * ts["pt_per_unit"]
        ix, iy = int(round(px * k)), int(round(py * k))
        best = min(sum(pix.pixel(min(max(ix + dx, 0), pix.width - 1),
                                  min(max(iy + dy, 0), pix.height - 1))[:3])
                   for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2))
        hits += best < 300
    assert hits >= 10
    # flipped Y: higher DXF y is higher on the page (smaller page y)
    assert ts["origin_pt"][1] - 100 * ts["pt_per_unit"] < ts["origin_pt"][1] - 0


def test_text_items_roundtrip(dxf, tmp_path):
    r = dr.resolve(dxf, workdir=str(tmp_path))
    ts = r["to_source"]
    by = {i["t"]: i for i in r["text_items"]}
    assert "HELLO" in by and "Note A" in by and "AB-123" in by
    assert any(t.startswith("200") for t in by)            # dimension measurement text
    h = by["HELLO"]
    # left x of HELLO is DXF x=20 ; baseline y=80 -> bottom of box on page
    x_back = (h["x0"] - ts["origin_pt"][0]) / ts["pt_per_unit"]
    y_back = (ts["origin_pt"][1] - h["y1"]) / ts["pt_per_unit"]
    assert abs(x_back - 20) * ts["pt_per_unit"] < 0.5
    assert abs(y_back - 80) * ts["pt_per_unit"] < 6.0       # descender slack in the text box
    assert h["y0"] < h["y1"] and h["x0"] < h["x1"]
    # the exact text sits over rendered ink at its page position
    with fitz.open(r["pdf"]) as pdf:
        pix = pdf[0].get_pixmap(dpi=144, clip=fitz.Rect(h["x0"], h["y0"], h["x1"], h["y1"]))
    dark = sum(1 for yy in range(pix.height) for xx in range(pix.width)
               if sum(pix.pixel(xx, yy)[:3]) < 300)
    assert dark > 5


def test_empty_dxf_error(tmp_path):
    p = str(tmp_path / "e.dxf")
    ezdxf.new().saveas(p)
    with pytest.raises(dr.RenderError) as ei:
        dr.resolve(p, workdir=str(tmp_path))
    assert ei.value.code == "DXF_RENDER_FAILED" and "no entities" in ei.value.message


def test_corrupt_dxf_error(tmp_path):
    p = tmp_path / "c.dxf"
    p.write_text("this is not a dxf")
    with pytest.raises(dr.RenderError) as ei:
        dr.resolve(str(p), workdir=str(tmp_path))
    assert ei.value.code == "DXF_RENDER_FAILED"


def test_paperspace_only_and_both(tmp_path):
    doc = ezdxf.new()
    doc.layout("Layout1").add_line((0, 0), (100, 50))
    p = str(tmp_path / "ps.dxf")
    doc.saveas(p)
    r = dr.resolve(p, workdir=str(tmp_path))
    assert r["source_kind"] == "dxf" and not r["warnings"]
    doc.modelspace().add_line((0, 0), (10, 10))
    p2 = str(tmp_path / "both.dxf")
    doc.saveas(p2)
    r2 = dr.resolve(p2, workdir=str(tmp_path))
    assert any("Model" in w for w in r2["warnings"])


def test_passthrough_and_errors(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    r = dr.resolve(str(pdf))
    assert r["pdf"] == str(pdf) and r["source_kind"] == "pdf" and r["to_source"] is None
    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    assert dr.resolve(str(png))["source_kind"] == "image"
    odd = tmp_path / "a.xyz"
    odd.write_bytes(b"x")
    with pytest.raises(dr.RenderError) as ei:
        dr.resolve(str(odd))
    assert ei.value.code == "UNSUPPORTED_TYPE"
    with pytest.raises(dr.RenderError) as ei:
        dr.resolve(str(tmp_path / "missing.dxf"))
    assert ei.value.code == "FILE_NOT_FOUND"


class FakeBridge:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    def _call_raw(self, tool, params):
        self.calls.append(tool)
        if tool == self.fail:
            return {"status": "FAILED", "error": {"code": "BOOM", "message": "nope"}}
        if tool == "export_document":
            with open(params["file_path"], "wb") as fh:
                fh.write(b"%PDF-1.4 fake")
        return {"status": "COMPLETED"}


@pytest.mark.parametrize("ext", ["dwg", "slddrw"])
def test_solidworks_formats_fake_bridge(tmp_path, ext):
    src = tmp_path / f"d.{ext}"
    src.write_bytes(b"x")
    b = FakeBridge()
    r = dr.resolve(str(src), workdir=str(tmp_path / "w"), bridge=b)
    assert r["source_kind"] == ext and os.path.exists(r["pdf"]) and r["pdf"] in r["temp_files"]
    assert b.calls == ["open_document", "export_document", "close_document"]
    for fail in ("open_document", "export_document"):
        b2 = FakeBridge(fail)
        with pytest.raises(dr.RenderError) as ei:
            dr.resolve(str(src), workdir=str(tmp_path / "w2"), bridge=b2)
        assert ei.value.code == ext.upper() + "_RENDER_FAILED" and "BOOM" in ei.value.message
    with pytest.raises(dr.RenderError) as ei:
        dr.resolve(str(src), bridge=None)
    assert ei.value.code == ext.upper() + "_RENDER_FAILED"
