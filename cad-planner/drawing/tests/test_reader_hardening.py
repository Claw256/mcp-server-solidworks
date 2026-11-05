"""Exact-reading hardening of the DXF / PDF readers: every case is a DXF or PDF GENERATED IN-TEST
(ezdxf / PyMuPDF), because there are no checked-in real drawings. Each test pins the failure it was
written for: geometry silently dropped, a wrong scale or unit, a mis-classified line, a lost tolerance.

Skipped (not failed) when ezdxf / PyMuPDF is missing.

Run:  python cad-planner/drawing/tests/test_reader_hardening.py   |   pytest cad-planner/drawing/tests/test_reader_hardening.py
"""
import json
import os
import random
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(os.path.dirname(_HERE))                  # cad-planner/
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

try:
    import ezdxf
    from ezdxf.math import Vec2
    HAVE = True
except Exception:  # noqa: BLE001
    HAVE = False
try:
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    HAVE_PDF = HAVE
except Exception:  # noqa: BLE001
    HAVE_PDF = False

import drawing  # noqa: E402
from drawing import callouts, dxf_read, lowering, pdf_read, vocab  # noqa: E402

_REPO = os.path.dirname(_PKG)
_SCHEMA = os.path.join(_REPO, "cad-planner", "contracts", "draw-dialect.schema.json")


# --------------------------------------------------------------------------- helpers
def _new(insunits=4):
    doc = ezdxf.new("R2010", setup=True)
    doc.units = insunits
    return doc


def _border(msp, x0=-40, y0=-40, x1=160, y1=100):
    for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        msp.add_line(a, b)


def _rect_lines(msp, x0, y0, x1, y1, **attr):
    for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        msp.add_line(a, b, dxfattribs=attr)


def _read(doc, layout="", cfg=None, name="t.dxf"):
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, name)
        doc.saveas(p)
        return drawing.read(p, cfg or drawing.load_config(), layout=layout)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def _view(art, key=lambda v: v["size"][0] * v["size"][1]):
    return max(art["views"], key=key)


def _codes(art):
    return [a["code"] for a in art["sheet"].get("advisories", [])]


def _skip_unless(flag):
    if not flag:
        print("     (skipped: ezdxf / PyMuPDF not installed)")
    return flag


# --------------------------------------------------------------------------- 1. entities
def test_lwpolyline_rectangle_reads_like_four_lines():
    if not _skip_unless(HAVE):
        return
    a = _new()
    _border(a.modelspace())
    _rect_lines(a.modelspace(), 0, 0, 100, 40)
    b = _new()
    _border(b.modelspace())
    b.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 40), (0, 40)], close=True)
    va, vb = _view(_read(a)), _view(_read(b))
    assert va["size"] == vb["size"] == [100.0, 40.0], (va["size"], vb["size"])
    assert len(vb["geometry"]["lines"]) == 4 and len(va["geometry"]["lines"]) == 4
    assert [l["id"] if "id" in l else l for l in va["loops"]] and len(vb["loops"]) == 1
    assert abs(vb["loops"][0]["area"] - 4000.0) < 0.01


def test_polyline_bulge_becomes_an_exact_arc_and_open_polyline_keeps_its_ends():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    msp = doc.modelspace()
    _border(msp)
    # 100 x 40 slot-ended plate: right end is a half circle of radius 20 (bulge 1 = 180 deg)
    msp.add_lwpolyline([(0, 0), (80, 0, 0, 0, 1.0), (80, 40), (0, 40)], close=True)
    art = _read(doc)
    v = _view(art)
    assert len(v["geometry"]["arcs"]) == 1, v["geometry"]
    arc = v["geometry"]["arcs"][0]
    assert abs(arc["r"] - 20.0) < 0.01 and abs((arc["a2"] - arc["a1"]) % 360.0 - 180.0) < 0.1, arc
    assert abs(v["size"][0] - 100.0) < 0.01 and abs(v["size"][1] - 40.0) < 0.01, v["size"]
    # classic POLYLINE (2D) too
    d2 = _new()
    m2 = d2.modelspace()
    _border(m2)
    m2.add_polyline2d([(0, 0), (60, 0), (60, 30), (0, 30)], close=True)
    assert _view(_read(d2))["size"] == [60.0, 30.0]
    assert "LWPOLYLINE" not in art["sheet"]["not_read"] and "POLYLINE" not in art["sheet"]["not_read"]


def test_spline_is_not_lost():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    msp = doc.modelspace()
    _border(msp)
    msp.add_line((0, 0), (100, 0))
    msp.add_line((0, 0), (0, 30))
    msp.add_line((100, 0), (100, 30))
    sp = msp.add_spline(fit_points=[(0, 30), (25, 45), (75, 45), (100, 30)])       # a domed top
    from ezdxf import path
    top = max(p.y for p in path.make_path(sp).flattening(0.001))                   # the curve's true apex
    v = _view(_read(doc))
    assert v["size"][0] == 100.0 and abs(v["size"][1] - top) < 0.05 and top > 40.0, (v["size"], top)
    assert len(v["geometry"]["lines"]) > 10, "the spline was flattened to chords, not dropped"


def test_fills_images_and_wipeouts_are_counted_not_dropped():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    msp = doc.modelspace()
    _border(msp)
    _rect_lines(msp, 0, 0, 50, 30)
    h = msp.add_hatch()
    h.paths.add_polyline_path([(0, 0), (10, 0), (10, 10)], is_closed=True)
    msp.add_solid([(0, 0), (5, 0), (5, 5), (0, 5)])
    msp.add_wipeout([(60, 0), (70, 0), (70, 10), (60, 10)])
    nr = _read(doc)["sheet"]["not_read"]
    assert nr.get("HATCH") == 1 and nr.get("SOLID") == 1 and nr.get("WIPEOUT") == 1, nr


def test_block_geometry_and_block_dimensions_are_read_for_foreign_files():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    blk = doc.blocks.new("PART")
    _rect_lines(blk, 0, 0, 100, 50)
    blk.add_circle((50, 25), 5)
    blk.add_linear_dim(base=(50, -15), p1=(0, 0), p2=(100, 0), dimstyle="Standard").render()
    msp = doc.modelspace()
    _border(msp)
    msp.add_blockref("PART", (10, 10))
    art = _read(doc)
    v = _view(art)
    assert v["size"] == [100.0, 50.0], v["size"]
    assert len(v["geometry"]["circles"]) == 1 and abs(v["geometry"]["circles"][0]["d"] - 10.0) < 0.01
    lin = [d for d in art["dimensions"] if d["kind"] == "linear"]
    assert lin and abs(lin[0]["value"] - 100.0) < 0.01, art["dimensions"]
    assert "BLOCK_GEOMETRY_READ" in _codes(art) and not art["sheet"]["not_read"].get("INSERT")


def test_solidworks_export_keeps_counting_block_geometry_as_not_read():
    """An SLD* dimstyle marks a SolidWorks export: its block geometry (centre marks, symbols) stays UNREAD,
    exactly as before -- the byte-identical-for-SolidWorks promise."""
    if not _skip_unless(HAVE):
        return
    doc = _new()
    doc.dimstyles.duplicate_entry("Standard", "SLD_X")
    blk = doc.blocks.new("CENTERMARK")
    blk.add_line((-2, 0), (2, 0))
    msp = doc.modelspace()
    _border(msp)
    _rect_lines(msp, 0, 0, 100, 50)
    msp.add_blockref("CENTERMARK", (50, 25))
    art = _read(doc)
    assert art["sheet"]["not_read"].get("INSERT") == {"CENTERMARK": 1}, art["sheet"]["not_read"]
    assert "BLOCK_GEOMETRY_READ" not in _codes(art)
    assert _view(art)["size"] == [100.0, 50.0]


def test_attrib_title_fields_and_tolerance_text_reach_the_output():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    blk = doc.blocks.new("TITLE")
    blk.add_attdef("MATERIAL", (0, 0), "x", dxfattribs={"height": 3})
    blk.add_attdef("PARTNO", (0, -6), "x", dxfattribs={"height": 3})
    msp = doc.modelspace()
    _border(msp)
    _rect_lines(msp, 0, 0, 100, 50)
    ins = msp.add_blockref("TITLE", (100, -30))
    ins.add_auto_attribs({"MATERIAL": "AL 6061", "PARTNO": "P-100"})
    msp.new_entity("TOLERANCE", {"insert": (20, 70), "dimstyle": "Standard",
                                 "content": r"{\Fgdt;j}%%v{\Fgdt;n}%%c0.1{\Fgdt;m}%%vA"})
    art = _read(doc)
    assert art["sheet"]["title_fields"] == {"MATERIAL": "AL 6061", "PARTNO": "P-100"}
    texts = {n["text"]: n for n in art["notes"] + art["frame_notes"]}
    assert texts["AL 6061"]["tag"] == "MATERIAL" and texts["P-100"]["tag"] == "PARTNO"
    fcf = [n for n in art["notes"] + art["frame_notes"] if n.get("fcf")]
    assert fcf and fcf[0]["text"] == "FCF [gdt:j]|[gdt:n]Ø0.1[gdt:m]|A", fcf
    assert "TOLERANCE" not in art["sheet"]["not_read"]


def test_mirrored_arc_and_circle_use_the_ocs_not_the_raw_numbers():
    if not _skip_unless(HAVE):
        return
    def build(mirrored):
        doc = _new()
        msp = doc.modelspace()
        _border(msp)
        _rect_lines(msp, -30, -10, 30, 40)
        if mirrored:      # OCS x is flipped: this arc is the WCS arc centre (-10,0) r5 from 90 to 180
            msp.add_arc((10, 0), 5, 0, 90, dxfattribs={"extrusion": (0, 0, -1)})
            msp.add_circle((15, 20), 3, dxfattribs={"extrusion": (0, 0, -1)})
        else:
            msp.add_arc((-10, 0), 5, 90, 180)
            msp.add_circle((-15, 20), 3)
        return _read(doc)
    m, p = build(True), build(False)
    vm, vp = _view(m), _view(p)
    assert vm["geometry"]["arcs"] == vp["geometry"]["arcs"], (vm["geometry"]["arcs"], vp["geometry"]["arcs"])
    assert vm["geometry"]["circles"] == vp["geometry"]["circles"]
    arc = vm["geometry"]["arcs"][0]
    assert abs(arc["a1"] - 90.0) < 0.01 and abs(arc["a2"] - 180.0) < 0.01 and abs(arc["r"] - 5.0) < 1e-9


def test_multileader_target_is_linked_to_the_circle_it_points_at():
    if not _skip_unless(HAVE):
        return
    from ezdxf.render.mleader import ConnectionSide
    doc = _new()
    msp = doc.modelspace()
    _border(msp)
    _rect_lines(msp, 0, 0, 100, 50)
    msp.add_circle((50, 25), 5)
    msp.add_circle((20, 25), 3)
    b = msp.add_multileader_mtext("Standard")
    b.set_content("4X Ø10 THRU")
    b.add_leader_line(ConnectionSide.left, [Vec2(55, 25)])          # the rim of the first circle
    b.build(insert=Vec2(80, 60))
    art = _read(doc)
    co = art["callouts"][0]
    assert co["dia"] == 10.0 and co["count"] == 4
    v = _view(art)
    idx = [i for i, c in enumerate(v["geometry"]["circles"]) if abs(c["d"] - 10.0) < 0.01][0]
    assert co["target"] == "%s:c%d" % (v["vid"], idx), co
    assert co["dia"] == 10.0                                         # the link changed no value
    assert any(n.get("target") == co["target"] for n in art["notes"])


# --------------------------------------------------------------------------- 2. scale / units
def _dim_doc(style="ISO-25", dimlfac=2.0, paper=False, geom_k=0.5):
    """A plate drawn at `geom_k` of its true size with one linear dimension on `style`."""
    doc = _new()
    ds = doc.dimstyles.duplicate_entry("Standard", style)
    ds.dxf.dimlfac = dimlfac
    msp = doc.paperspace() if paper else doc.modelspace()
    _border(msp)
    w, h = 100.0 * geom_k, 50.0 * geom_k
    _rect_lines(msp, 0, 0, w, h)
    msp.add_linear_dim(base=(w / 2, -10), p1=(0, 0), p2=(w, 0), dimstyle=style).render()
    return doc


def test_dimlfac_comes_from_the_dimstyle_the_dimension_uses_not_the_first_sld_style():
    if not _skip_unless(HAVE):
        return
    art = _read(_dim_doc("ISO-25", 2.0))
    assert art["sheet"]["scale_factor"] == 2.0
    lin = [d for d in art["dimensions"] if d["kind"] == "linear"][0]
    assert abs(lin["value"] - 100.0) < 0.01, lin               # raw 50 x 2
    assert _view(art)["size"] == [100.0, 50.0]                 # geometry scaled by the same factor


def test_negative_dimlfac_applies_in_paper_space_only():
    if not _skip_unless(HAVE):
        return
    model = _read(_dim_doc("ISO-25", -2.0, paper=False, geom_k=1.0))
    paper = _read(_dim_doc("ISO-25", -2.0, paper=True, geom_k=0.5))
    lm = [d for d in model["dimensions"] if d["kind"] == "linear"][0]
    lp = [d for d in paper["dimensions"] if d["kind"] == "linear"][0]
    assert abs(lm["value"] - 100.0) < 0.01 and model["sheet"]["scale_factor"] == 1.0, (lm, model["sheet"])
    assert abs(lp["value"] - 100.0) < 0.01 and paper["sheet"]["scale_factor"] == 2.0, (lp, paper["sheet"])


def test_mixed_dimlfac_styles_scale_each_dimension_by_its_own_style():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    doc.dimstyles.duplicate_entry("Standard", "A2").dxf.dimlfac = 2.0
    msp = doc.modelspace()
    _border(msp)
    _rect_lines(msp, 0, 0, 100, 50)
    msp.add_linear_dim(base=(50, -10), p1=(0, 0), p2=(100, 0), dimstyle="Standard").render()
    msp.add_linear_dim(base=(120, 25), p1=(100, 0), p2=(100, 50), dimstyle="A2", angle=90).render()
    art = _read(doc)
    vals = sorted(round(d["value"], 3) for d in art["dimensions"])
    assert vals == [100.0, 100.0], vals                       # 100 x 1  and  50 x 2
    assert "DIMLFAC_MIXED" in _codes(art)


def _title_doc(text, dimlfac=1.0):
    doc = _dim_doc("Standard", dimlfac, geom_k=1.0 / dimlfac if dimlfac else 1.0)
    doc.modelspace().add_text(text, dxfattribs={"insert": (100, -35), "height": 3})
    return doc


def test_title_block_scale_text_is_cross_checked_against_dimlfac():
    if not _skip_unless(HAVE):
        return
    bad = _read(_title_doc("SCALE 1:2", dimlfac=1.0))
    assert "SCALE_MISMATCH" in _codes(bad), bad["sheet"].get("advisories")
    assert bad["sheet"]["title_scale"]["factor"] == 2.0 and bad["sheet"]["scale_factor"] == 1.0
    ok = _read(_title_doc("Maßstab 1:2", dimlfac=2.0))
    assert "SCALE_MISMATCH" not in _codes(ok) and ok["sheet"]["title_scale"]["factor"] == 2.0
    # a view caption in the same band is NOT the sheet scale
    cap = _title_doc("DETAIL B SCALE 5:1", dimlfac=1.0)
    assert "title_scale" not in _read(cap)["sheet"]


def test_both_layouts_with_content_read_one_layout_and_say_so():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    msp = doc.modelspace()
    _border(msp)
    _rect_lines(msp, 0, 0, 100, 50)
    ps = doc.paperspace()
    _rect_lines(ps, 0, 0, 50, 25)                              # the same part again, at sheet size
    auto = _read(doc)
    assert auto["sheet"]["layout_auto"] == {"chosen": "Model", "ignored": ["Layout1"]}
    assert "LAYOUT_AUTO" in _codes(auto) and "layout_selected" not in auto["sheet"]
    assert _view(auto)["size"] == [100.0, 50.0] and len(auto["views"]) == 1       # no double count
    merged = _read(doc, layout="*")
    assert "LAYOUT_MERGED" in _codes(merged) and "layout_auto" not in merged["sheet"]
    assert len(_view(merged)["geometry"]["lines"]) == 8                     # the overlay the old default produced
    only = _read(doc, layout="Layout1")
    assert only["sheet"]["layout_selected"] == "Layout1" and not _codes(only)


def test_single_content_layout_is_read_silently_whichever_it_is():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    ps = doc.paperspace()                                       # a DWG->DXF conversion: everything in paper space
    _border(ps)
    _rect_lines(ps, 0, 0, 100, 50)
    art = _read(doc)
    assert _view(art)["size"] == [100.0, 50.0] and "layout_auto" not in art["sheet"]
    assert "LAYOUT_AUTO" not in _codes(art)


def test_unitless_insunits_keeps_mm_adds_advisory_and_inch_hint():
    if not _skip_unless(HAVE):
        return
    doc = _new(insunits=0)
    msp = doc.modelspace()
    _border(msp, 0.25, 0.25, 10.75, 8.25)                      # an ANSI-A (11 x 8.5 in) sheet's frame
    _rect_lines(msp, 2, 2, 6, 4)                                # a 4 x 2 part
    art = _read(doc)
    assert art["sheet"]["units_converted_from_mm_per_unit"] == 1.0
    assert _view(art)["size"] == [4.0, 2.0], "values must NOT be silently converted"
    assert "UNITS_UNSET" in _codes(art)
    hint = art["sheet"]["units_hint"]
    assert hint["unit"] == "inch" and "ANSI A" in hint["basis"], hint
    metric = _new(insunits=0)
    _border(metric.modelspace(), 10, 10, 410, 287)             # A3
    _rect_lines(metric.modelspace(), 100, 100, 200, 150)
    mh = _read(metric)["sheet"]["units_hint"]
    assert mh["unit"] == "mm" and "A3" in mh["basis"], mh
    assert "UNITS_UNSET" not in _codes(_read(_new(insunits=4)))      # a declared unit says nothing


def test_unitless_hint_falls_back_to_measurement_header():
    if not _skip_unless(HAVE):
        return
    doc = _new(insunits=0)
    doc.header["$MEASUREMENT"] = 0
    _rect_lines(doc.modelspace(), 0, 0, 4, 2)
    art = _read(doc)
    assert art["sheet"]["units_hint"]["unit"] == "inch" and "MEASUREMENT" in art["sheet"]["units_hint"]["basis"]


def test_view_scale_label_regex_and_position_are_wider():
    if not _skip_unless(HAVE):
        return
    def build(label, beside=False):
        doc = _new()
        msp = doc.modelspace()
        _border(msp, -60, -60, 260, 160)
        _rect_lines(msp, 0, 0, 50, 25)                       # drawn at half size
        msp.add_circle((25, 12.5), 4)
        pos = (60, 12) if beside else (15, -10)
        msp.add_text(label, dxfattribs={"insert": pos, "height": 3})
        return _view(_read(doc))
    for label in ("SCALE 1/2", "M 2:1" if False else "1 : 2", "Massstab 1:2", "HALF SCALE", "1/2 SCALE"):
        v = build(label)
        assert v.get("scale_factor") == 2.0 and v["size"] == [100.0, 50.0], (label, v.get("scale_factor"), v["size"])
    beside = build("SCALE 1:2", beside=True)
    assert beside.get("scale_factor") == 2.0, "a label level with the view and just beside it belongs to it"
    assert callouts.parse_scale("M 2:1") == (2.0, 1.0) and callouts.parse_scale("R1/2") is None


def test_projection_statement_disagreeing_with_config_is_advised():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    msp = doc.modelspace()
    _border(msp)
    _rect_lines(msp, 0, 0, 100, 50)
    msp.add_text("THIRD ANGLE PROJECTION", dxfattribs={"insert": (-30, -30), "height": 2.5})
    cfg = drawing.load_config()
    assert cfg["projection"] == "first_angle"
    assert "PROJECTION_MISMATCH" in _codes(_read(doc, cfg=cfg))
    cfg3 = dict(cfg, projection="third_angle")
    assert "PROJECTION_MISMATCH" not in _codes(_read(doc, cfg=cfg3))


def test_empty_drawing_raises_zero_views():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    doc.modelspace().add_text("NOTHING TO BUILD", dxfattribs={"insert": (0, 0), "height": 3})
    art = _read(doc)
    assert art["views"] == [] and "ZERO_VIEWS" in _codes(art)


# --------------------------------------------------------------------------- 3. classification etc.
def test_linetype_families_are_classed_hidden_center_or_cut_line():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    doc.linetypes.add("ACAD_ISO02W100", pattern=[12.0, 8.0, -4.0], description="ISO dash")
    doc.linetypes.add("ACAD_ISO04W100", pattern=[24.0, 18.0, -3.0, 0.0, -3.0], description="ISO long-dash dot")
    doc.linetypes.add("ISO05W100", pattern=[24.0, 18.0, -3.0, 0.0, -3.0, 0.0, -3.0], description="x")
    doc.linetypes.add("MYSTERY", pattern=[1.0, 0.5, -0.5], description="unknown")
    cases = {"DASHED": ("hidden", None), "DASHED2": ("hidden", None), "HIDDENX2": ("hidden", None),
             "DASHDOT": ("center", None), "DASHDOT2": ("center", None), "DIVIDE": ("center", None),
             "DOT": ("hidden", None), "ACAD_ISO02W100": ("hidden", None), "ACAD_ISO04W100": ("center", None),
             "ISO05W100": ("cut_line", None), "PHANTOM": ("cut_line", None), "CENTER2": ("center", None),
             "MYSTERY": ("visible", None), "Continuous": ("visible", None),
             "DASHDOT/heavy": ("cut_line", 50), "DASHDOT/thin": ("center", 18)}
    msp = doc.modelspace()
    for i, (name, (cls, lw)) in enumerate(cases.items()):
        attr = {"linetype": name.split("/")[0]}
        if lw is not None:
            attr["lineweight"] = lw
        ln = msp.add_line((0, i), (10, i), dxfattribs=attr)
        assert dxf_read.edge_class(doc, ln) == cls, (name, lw, dxf_read.edge_class(doc, ln))
    # BYLAYER resolves through the layer's linetype
    doc.layers.add("HID", linetype="DASHED")
    assert dxf_read.edge_class(doc, msp.add_line((0, 0), (1, 0), dxfattribs={"layer": "HID"})) == "hidden"


def test_dashed_line_in_a_drawing_is_a_hidden_edge():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    msp = doc.modelspace()
    _border(msp)
    _rect_lines(msp, 0, 0, 100, 50)
    msp.add_line((0, 25), (100, 25), dxfattribs={"linetype": "DASHED"})     # touches the edges: one cluster
    cl = [x["c"] for x in _view(_read(doc))["geometry"]["lines"]]
    assert cl.count("hidden") == 1 and cl.count("visible") == 4, cl


def test_count_prefix_is_not_the_value():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    msp = doc.modelspace()
    _border(msp)
    _rect_lines(msp, 0, 0, 100, 50)
    msp.add_circle((30, 25), 4)                                # d 8
    msp.add_arc((80, 25), 5, 0, 180)                           # r 5
    msp.add_diameter_dim(center=(30, 25), radius=4, angle=45, dimstyle="Standard",
                         text="4X Ø8").render()
    msp.add_radius_dim(center=(80, 25), radius=5, angle=60, dimstyle="Standard", text="2X R5").render()
    art = _read(doc)
    dia = [d for d in art["dimensions"] if d["kind"] == "diameter"][0]
    rad = [d for d in art["dimensions"] if d["kind"] == "radius"][0]
    assert dia["value"] == 8.0 and dia["count"] == 4 and dia.get("measures"), dia
    assert rad["value"] == 5.0 and rad["count"] == 2 and rad.get("measures"), rad
    assert not dia.get("printed_mismatch") and not rad.get("printed_mismatch")


def test_count_prefix_never_overrides_the_geometry_value():
    """Printed '4X Ø8' on a circle that is really 10: value stays the GEOMETRY's 10 and is flagged."""
    if not _skip_unless(HAVE):
        return
    doc = _new()
    msp = doc.modelspace()
    _border(msp)
    _rect_lines(msp, 0, 0, 100, 50)
    msp.add_circle((30, 25), 5)
    msp.add_diameter_dim(center=(30, 25), radius=5, angle=45, dimstyle="Standard", text="4X Ø8").render()
    dia = [d for d in _read(doc)["dimensions"] if d["kind"] == "diameter"][0]
    assert dia["value"] == 10.0 and dia["count"] == 4 and dia["printed_mismatch"] is True, dia


def test_stacked_mtext_tolerance_is_parsed():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    msp = doc.modelspace()
    _border(msp)
    _rect_lines(msp, 0, 0, 100, 50)
    msp.add_linear_dim(base=(50, -10), p1=(0, 0), p2=(100, 0), dimstyle="Standard",
                       text="<>\\S+0.1^-0.2;").render()
    d = [x for x in _read(doc)["dimensions"] if x["kind"] == "linear"][0]
    assert d["tol"] == {"plus": 0.1, "minus": 0.2}, d
    assert abs(d["value"] - 100.0) < 0.01
    assert callouts.parse_tolerance("\\S+0.1^0;") == {"plus": 0.1, "minus": 0.0}
    assert callouts.parse_tolerance("\\S0^-0.2;") == {"plus": 0.0, "minus": 0.2}
    assert callouts.parse_tolerance("50\\S±0.05^;") == {"plus": 0.05, "minus": 0.05}


def test_comma_decimals_in_bend_notes_and_thickness_notes():
    if not _skip_unless(HAVE):
        return
    doc = _new()
    msp = doc.modelspace()
    _border(msp)
    _rect_lines(msp, 0, 0, 100, 50)
    msp.add_text("UP 90 R 1,5", dxfattribs={"insert": (10, 20), "height": 3})
    b = _read(doc)["bend_notes"][0]
    assert b["dir"] == "UP" and b["angle_deg"] == 90.0 and b["radius"] == 1.5, b
    flat = {"vid": "v0", "size": [80.0, 100.0]}
    th = lowering._resolve_thickness({"notes": [{"text": "1,5 mm"}], "dimensions": []}, flat, {"v0": flat},
                                     drawing.load_config())
    assert th["state"] == "ok" and th["value"] == 1.5, th


def _brute_cluster(items, gap):
    par = list(range(len(items)))

    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i][1], items[j][1]
            if a[0] <= b[2] + gap and b[0] <= a[2] + gap and a[1] <= b[3] + gap and b[1] <= a[3] + gap:
                ra, rb = find(i), find(j)
                if ra != rb:
                    par[ra] = rb
    groups = {}
    for i, it in enumerate(items):
        groups.setdefault(find(i), []).append(it)
    return list(groups.values())


def test_grid_clustering_equals_the_all_pairs_union_find():
    rnd = random.Random(7)
    for trial in range(30):
        items = []
        for i in range(rnd.randint(1, 160)):
            x, y = rnd.uniform(0, 300), rnd.uniform(0, 200)
            w, h = rnd.choice((0.0, 0.5, 3.0, 12.0)), rnd.choice((0.0, 0.5, 3.0, 12.0))
            items.append((i, (x, y, x + w, y + h)))
        if trial % 3 == 0:
            items.insert(rnd.randint(0, len(items)), ("frame", (-5.0, -5.0, 305.0, 205.0)))   # a sheet border
        assert dxf_read.cluster(items, 1.0) == _brute_cluster(items, 1.0), trial
    assert dxf_read.cluster([], 1.0) == [] and len(dxf_read.cluster([("a", (0, 0, 1, 1))], 1.0)) == 1


def test_clustering_scales_to_a_large_drawing():
    import time
    items = [(i, (x, y, x + 1.0, y + 0.2)) for i, (x, y) in enumerate(
        (float(k % 400) * 3.0, float(k // 400) * 3.0) for k in range(20000))]
    t0 = time.time()
    groups = dxf_read.cluster(items, 1.0)
    assert len(groups) == 20000 and time.time() - t0 < 5.0       # the all-pairs form needs ~2e8 comparisons


# --------------------------------------------------------------------------- 4. PDF
def _m(mm):
    return mm * 72.0 / 25.4


def _pdf(path, extra=None, scale_text="SCALE 1:1", image=False):
    doc = fitz.open()
    page = doc.new_page(width=_m(420), height=_m(297))
    w = 0.35 * 72 / 25.4
    page.draw_rect(fitz.Rect(_m(10), _m(10), _m(410), _m(287)), width=w)
    x0, y0 = 100.0, 100.0
    page.draw_rect(fitz.Rect(_m(x0), _m(y0), _m(x0 + 100), _m(y0 + 50)), width=w)
    page.draw_circle(fitz.Point(_m(150), _m(125)), _m(5.0), width=w)
    if scale_text:
        page.insert_text(fitz.Point(_m(330), _m(278)), scale_text, fontsize=10)
    if image:
        pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 200, 140), False)
        pix.clear_with(200)
        page.insert_image(fitz.Rect(0, 0, _m(420), _m(297)), pixmap=pix)
    if extra:
        extra(page)
    doc.save(path)
    doc.close()


def _read_pdf(**kw):
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "t.pdf")
        _pdf(p, **kw)
        return drawing.read_pdf(p, drawing.load_config())
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_pdf_scale_prefers_the_title_block_over_a_detail_caption():
    if not _skip_unless(HAVE_PDF):
        return

    def detail_last(page):                       # written AFTER the title block scale: last in the stream
        page.insert_text(fitz.Point(_m(60), _m(60)), "DETAIL B SCALE 2:1", fontsize=10)
    art = _read_pdf(scale_text="SCALE 1:2", extra=detail_last)
    pdf = art["sheet"]["pdf"]
    assert pdf["scale"] == "1:2" and pdf["scale_source"] == "title block text", pdf
    kinds = {(c["text"], c["kind"]) for c in pdf["scale_candidates"]}
    assert kinds == {("1:2", "sheet"), ("2:1", "view")}, kinds
    assert "SCALE_CANDIDATES" in _codes(art)
    # unit level: a detail caption FIRST in the stream must not win either
    texts = [("DETAIL B SCALE 2:1", 60.0, 230.0, 3.5, 0.0), ("SCALE 1:2", 330.0, 19.0, 3.5, 0.0)]
    assert pdf_read._detect_scale(texts, "", (420.0, 297.0))[1] == "1:2"
    only_detail = pdf_read._detect_scale(texts[:1], "", (420.0, 297.0))
    assert only_detail[0] == 1.0 and "ASSUMED" in only_detail[2], only_detail


def test_detect_scale_still_accepts_bare_text_tuples_without_positions():
    """drawing_prep feeds (string,) 1-tuples; positions are optional."""
    assert pdf_read._detect_scale([("DETAIL B",), ("SCALE 2:1",), ("SCALE 1:2",)])[:2] == (2.0, "1:2")
    assert pdf_read._detect_scale([("SCALE 1:2",)])[2] == "title block text"
    assert "ASSUMED" in pdf_read._detect_scale([("nothing",)])[2]


def test_pdf_scale_two_sheet_statements_pick_the_one_nearest_the_corner():
    texts = [("SCALE 1:5", 40.0, 150.0, 3.5, 0.0), ("SCALE 1:2", 380.0, 12.0, 3.5, 0.0),
             ("SCALE 1:10", 300.0, 60.0, 3.5, 0.0)]
    assert pdf_read._detect_scale(texts, "", (420.0, 297.0))[1] == "1:2"


def test_pdf_heavy_dash_dot_chain_is_a_cut_line_and_thin_is_a_centre_line():
    if not _skip_unless(HAVE_PDF):
        return

    def lines(page):
        page.draw_line(fitz.Point(_m(95), _m(125)), fitz.Point(_m(205), _m(125)), width=0.6 * 72 / 25.4,
                       dashes="[8 2 2 2] 0")                                  # heavy chain: the cut line
        page.draw_line(fitz.Point(_m(150), _m(95)), fitz.Point(_m(150), _m(155)), width=0.18 * 72 / 25.4,
                       dashes="[8 2 2 2] 0")                                  # thin chain: the axis
    art = _read_pdf(extra=lines)
    classes = [l["c"] for v in art["views"] for l in v["geometry"]["lines"]]
    assert "cut_line" in classes and "center" in classes, classes
    assert pdf_read._dash_class("[8 2 2 2] 0", 0.5) == "cut_line"
    assert pdf_read._dash_class("[8 2 2 2] 0", 0.18) == "center"
    assert pdf_read._dash_class("[3 2] 0", 0.5) == "hidden"


def test_scan_with_a_vector_border_is_refused_as_not_vector():
    if not _skip_unless(HAVE_PDF):
        return
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "scan.pdf")
        _pdf(p, image=True)
        try:
            drawing.read_pdf(p, drawing.load_config())
        except ValueError as ex:
            assert "scan" in str(ex) and "raster" in str(ex), ex
        else:
            raise AssertionError("a page covered by a raster image must be refused (PDF_NOT_VECTOR)")
        clean = os.path.join(d, "vector.pdf")
        _pdf(clean)
        assert drawing.read_pdf(clean, drawing.load_config())["views"]       # the same page without the image reads
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- 5. contracts
def test_every_advisory_code_is_documented_in_the_schema_and_emitted_codes_are_in_vocab():
    with open(_SCHEMA, encoding="utf-8") as fh:
        schema = json.load(fh)
    prose = schema["artifact"]["sheet"]["advisories"]
    for code in vocab.ADVISORY_CODES:
        assert code in prose, "%s is in vocab.ADVISORY_CODES but not documented in the schema" % code
    for key in ("layout_auto", "title_scale", "title_fields", "units_hint", "reader_additions"):
        assert key in schema["artifact"]["sheet"], key


def test_advisory_codes_emitted_by_the_readers_are_all_in_the_vocabulary():
    if not _skip_unless(HAVE):
        return
    import re
    src = ""
    for f in ("dxf_read.py", "pdf_read.py"):
        with open(os.path.join(os.path.dirname(_HERE), f), encoding="utf-8") as fh:
            src += fh.read()
    used = set(re.findall(r'advise\("([A-Z_]+)"', src)) | set(re.findall(r'"code": "([A-Z_]+)"', src))
    assert used and used <= vocab.ADVISORY_CODES, used - vocab.ADVISORY_CODES
    assert used == vocab.ADVISORY_CODES, vocab.ADVISORY_CODES - used     # no dead codes either


def test_wire_encoding_round_trips_the_new_keys():
    if not _skip_unless(HAVE):
        return
    doc = _new(insunits=0)
    _border(doc.modelspace(), 0.25, 0.25, 10.75, 8.25)
    _rect_lines(doc.modelspace(), 2, 2, 6, 4)
    art = _read(doc)
    assert drawing.wire_decode(drawing.wire_encode(art)) == art


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
