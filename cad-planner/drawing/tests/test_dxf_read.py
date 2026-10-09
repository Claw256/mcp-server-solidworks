"""read() on DXFs generated in-test with ezdxf (the golden-fixture suite is JSON-level on purpose;
this one exercises the READER). Skipped, not failed, when ezdxf is not installed.

Run:  python cad-planner/drawing/tests/test_dxf_read.py   |   pytest cad-planner/drawing/tests/test_dxf_read.py
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(os.path.dirname(_HERE))          # cad-planner/
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

try:
    import ezdxf
    from ezdxf import units
except Exception:  # noqa: BLE001
    ezdxf = None

import drawing  # noqa: E402
from drawing import dxf_read  # noqa: E402


def _plate(path, insunits=4, hidden_circle=True, note="4X M6", dim_text=None, extras=False):
    """A 100 x 50 plate (front view) with a hole Ø10, a linear dimension and a note."""
    doc = ezdxf.new("R2010", setup=True)
    doc.units = insunits
    if "HIDDEN" not in doc.linetypes:           # ezdxf's setup() has no HIDDEN; the reader keys on the name
        doc.linetypes.add("HIDDEN", pattern=[9.525, 6.35, -3.175], description="Hidden __ __ __")
    msp = doc.modelspace()
    # the sheet BORDER: the reader treats the biggest cluster that encloses all others as the frame
    for a, b in (((-40, -40), (160, -40)), ((160, -40), (160, 100)), ((160, 100), (-40, 100)),
                 ((-40, 100), (-40, -40))):
        msp.add_line(a, b)
    for a, b in (((0, 0), (100, 0)), ((100, 0), (100, 50)), ((100, 50), (0, 50)), ((0, 50), (0, 0))):
        msp.add_line(a, b)
    msp.add_circle((50, 25), 5, dxfattribs={"linetype": "HIDDEN"} if hidden_circle else {})
    msp.add_linear_dim(base=(50, -15), p1=(0, 0), p2=(100, 0), dimstyle="Standard",
                       text=dim_text or "<>").render()
    if extras:                                  # a leader note + a projection statement
        from ezdxf.math import Vec2
        from ezdxf.render.mleader import ConnectionSide
        b = msp.add_multileader_mtext("Standard")
        b.set_content("4X Ø8 THRU")
        b.add_leader_line(ConnectionSide.left, [Vec2(50, 25)])
        b.build(insert=Vec2(80, 60))
        msp.add_text("THIRD ANGLE PROJECTION", dxfattribs={"insert": (-30, -30), "height": 2.5})
    if note:
        msp.add_text(note, dxfattribs={"insert": (60, 70), "height": 3.5})
    doc.saveas(path)


def _read(**kw):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = os.path.join(d, "plate.dxf")
        _plate(p, **kw)
        return drawing.read(p, drawing.load_config())


def test_plate_view_geometry_classes_and_dimension():
    if ezdxf is None:
        print("     (skipped: ezdxf not installed)")
        return
    art = _read()
    assert art["sheet"]["units"] == 4 and art["sheet"]["units_mm_per_unit"] == 1.0
    assert art["sheet"].get("source") == "dxf"
    v = max(art["views"], key=lambda x: x["size"][0])
    assert abs(v["size"][0] - 100.0) < 0.01 and abs(v["size"][1] - 50.0) < 0.01, v["size"]
    assert len(v["geometry"]["lines"]) == 4 and {l["c"] for l in v["geometry"]["lines"]} == {"visible"}
    circle = v["geometry"]["circles"][0]
    assert abs(circle["d"] - 10.0) < 0.01 and circle["c"] == "hidden"
    dim = next(d for d in art["dimensions"] if d["kind"] == "linear")
    assert abs(dim["value"] - 100.0) < 0.01
    assert any("4X M6" in n["text"] for n in art["notes"] + art["frame_notes"])


def test_leader_callout_tolerance_and_projection_are_structured():
    if ezdxf is None:
        print("     (skipped: ezdxf not installed)")
        return
    art = _read(dim_text="100 ±0.1", extras=True)
    co = art.get("callouts") or []
    hole = next(c for c in co if c.get("dia") == 8.0)
    assert hole["count"] == 4 and hole["thru"] is True and hole["kind"] == "hole"

    dim = next(d for d in art["dimensions"] if d["kind"] == "linear")
    assert dim["tol"] == {"plus": 0.1, "minus": 0.1}
    assert art["sheet"]["projection_detected"] == "third_angle"
    assert art["sheet"]["not_read"].get("MULTILEADER") is None


def test_layout_selection_and_layout_report():
    if ezdxf is None:
        print("     (skipped: ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = os.path.join(d, "two.dxf")
        _plate(p)
        doc = ezdxf.readfile(p)
        lo = doc.layouts.new("Sheet2")
        for a, b in (((0, 0), (30, 0)), ((30, 0), (30, 20)), ((30, 20), (0, 20)), ((0, 20), (0, 0))):
            lo.add_line(a, b)
        doc.saveas(p)
        cfg = drawing.load_config()
        both = drawing.read(p, cfg)
        names = {l["name"]: l["entities"] for l in both["sheet"]["layouts"]}
        assert names.get("Sheet2") == 4 and names.get("Model", 0) > 4, names
        assert "layout_selected" not in both["sheet"]
        only = drawing.read(p, cfg, layout="Sheet2")
        assert only["sheet"]["layout_selected"] == "Sheet2"
        assert all(abs(v["size"][0] - 30.0) < 0.01 for v in only["views"] if v["role"] != "annotation") or only["views"]
        try:
            drawing.read(p, cfg, layout="Nope")
        except ValueError as ex:
            assert "Sheet2" in str(ex)
        else:
            raise AssertionError("an unknown layout must raise with the available names")


def _scaled_plate(path, insunits, k, dim_text=None, raw_code=None):
    """The 100 x 50 mm plate drawn in a unit worth `k` mm: coordinates are mm / k."""
    doc = ezdxf.new("R2010", setup=True)
    if raw_code is not None:
        doc.header["$INSUNITS"] = raw_code
    else:
        doc.units = insunits
    msp = doc.modelspace()
    W, H = 100.0 / k, 50.0 / k
    for a, b in (((-40 / k, -40 / k), (160 / k, -40 / k)), ((160 / k, -40 / k), (160 / k, 100 / k)),
                 ((160 / k, 100 / k), (-40 / k, 100 / k)), ((-40 / k, 100 / k), (-40 / k, -40 / k))):
        msp.add_line(a, b)
    for a, b in (((0, 0), (W, 0)), ((W, 0), (W, H)), ((W, H), (0, H)), ((0, H), (0, 0))):
        msp.add_line(a, b)
    msp.add_circle((W / 2, H / 2), 5.0 / k)                         # a hole of 10 mm diameter
    msp.add_linear_dim(base=(W / 2, -15 / k), p1=(0, 0), p2=(W, 0), dimstyle="Standard").render()
    # a diameter dimension whose PRINTED text is in the drawing's own unit
    msp.add_diameter_dim(center=(W / 2, H / 2), radius=5.0 / k, angle=45, dimstyle="Standard",
                         override={"dimtxt": 0.18 / k}, text="%.4f" % (10.0 / k)).render()
    doc.saveas(path)


def _read_scaled(insunits, k, **kw):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = os.path.join(d, "u.dxf")
        _scaled_plate(p, insunits, k, **kw)
        return drawing.read(p, drawing.load_config())


def _assert_true_mm(art, native, k):
    sh = art["sheet"]
    assert sh["units_mm_per_unit"] == native and sh["units_converted_from_mm_per_unit"] == k, sh
    v = max(art["views"], key=lambda x: x["size"][0])
    assert abs(v["size"][0] - 100.0) < 0.01 and abs(v["size"][1] - 50.0) < 0.01, v["size"]
    circ = [c for c in v["geometry"]["circles"] if abs(c["d"] - 10.0) < 0.01]
    assert circ, v["geometry"]["circles"]
    lin = [d for d in art["dimensions"] if d["kind"] == "linear"]
    assert lin and abs(lin[0]["value"] - 100.0) < 0.01, art["dimensions"]
    dia = [d for d in art["dimensions"] if d["kind"] == "diameter"]
    assert dia and abs(dia[0]["value"] - 10.0) < 0.01 and not dia[0].get("printed_mismatch"), dia


def test_inch_sheet_is_converted_to_true_mm_and_gate_does_not_refuse():
    if ezdxf is None:
        print("     (skipped: ezdxf not installed)")
        return
    art = _read_scaled(1, 25.4)
    assert art["sheet"]["units"] == 1
    _assert_true_mm(art, 25.4, 25.4)
    a = drawing.assess(art, drawing.load_config())
    assert a.get("reason") != "units_not_mm", a          # no bend notes here, but NOT a units refusal
    # the plain inch plate from the shared fixture: size reads true mm after conversion
    art2 = _read(insunits=1)
    assert art2["sheet"]["units_mm_per_unit"] == 25.4
    assert art2["sheet"]["units_converted_from_mm_per_unit"] == 25.4
    v = max(art2["views"], key=lambda x: x["size"][0])
    assert abs(v["size"][0] - 100.0 * 25.4) < 0.1, v["size"]


def test_centimetre_and_metre_sheets_are_converted():
    if ezdxf is None:
        print("     (skipped: ezdxf not installed)")
        return
    _assert_true_mm(_read_scaled(5, 10.0), 10.0, 10.0)
    _assert_true_mm(_read_scaled(6, 1000.0), 1000.0, 1000.0)


def test_unitless_stays_unconverted_and_unknown_code_is_refused():
    if ezdxf is None:
        print("     (skipped: ezdxf not installed)")
        return
    art = _read(insunits=0)
    assert art["sheet"]["units_mm_per_unit"] == 1.0
    assert art["sheet"]["units_converted_from_mm_per_unit"] == 1.0
    art = _read_scaled(0, 1.0, raw_code=99)
    assert art["sheet"]["units_mm_per_unit"] is None
    assert art["sheet"]["units_converted_from_mm_per_unit"] == 1.0
    a = drawing.assess(art, drawing.load_config())
    assert a["direct"] is False and a["reason"] == "units_not_mm", a


def test_read_doc_entry_point_matches_read():
    """The seam a PDF/SLDDRW reader uses: an in-memory ezdxf document through read_doc."""
    if ezdxf is None:
        print("     (skipped: ezdxf not installed)")
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = os.path.join(d, "plate.dxf")
        _plate(p)
        cfg = drawing.load_config()
        via_file = drawing.read(p, cfg)
        doc, auditor = ezdxf.recover.readfile(p)
        via_doc = dxf_read.read_doc(doc, auditor, p, cfg, source_kind="dxf")
        assert via_file == via_doc
        pdf_like = dxf_read.read_doc(doc, auditor, p, cfg, source_kind="pdf")
        assert pdf_like["sheet"]["source"] == "pdf"


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
