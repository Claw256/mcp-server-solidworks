"""slddrw_merge: pure merge of SolidWorks-native extras into a DXF-derived artifact. No deps.
Run:  python adapters/claude/tests/test_slddrw_merge.py   |   pytest ...
"""
import json
import math
import os
import sys

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import slddrw_merge as m  # noqa: E402


def _extras(**kw):
    ex = {
        "sheets": [{
            "name": "Sheet1", "paper_size": 8, "scale_num": 1, "scale_den": 2, "projection": "third_angle",
            "width_m": 0.42, "height_m": 0.297, "template": "a3.drt",
            "notes": [{"text": "BREAK ALL EDGES", "pos": [0.1, 0.05]}, {"text": "  "}],
            "views": [{
                "name": "Drawing View1", "type": 4, "scale": 0.5,
                "referenced_model": "plate.sldprt", "referenced_configuration": "Default",
                "notes": [{"text": "4X O8 THRU"}],
                "dimensions": [
                    {"name": "D1@Drawing View1", "value_si": 0.1,
                     "tol": {"type": 2, "max_si": 0.0001, "min_si": -0.0001}},
                    {"name": "D2@Drawing View1", "value_si": 0.008, "diametric": True,
                     "text": {"prefix": "4X "}},
                    {"name": "D3@Drawing View1", "value_si": 0.05},
                ]}]}],
        "errors": [], "custom_properties": {"Material": "AL"},
    }
    ex.update(kw)
    return ex


def _art(projection=None):
    return {"sheet": {"projection_detected": projection},
            "dimensions": [{"value": 100.0, "kind": "linear"}, {"value": 8.0, "kind": "diameter"},
                           {"value": 50.0, "kind": "linear"}]}


def test_projection_filled_only_when_null():
    a = _art()
    s = m.merge_extras(a, _extras())
    assert s["merged"] and a["sheet"]["projection_detected"] == "third_angle"
    assert a["sheet"]["projection_source"] == "slddrw sheet"
    b = _art("first_angle")
    m.merge_extras(b, _extras())
    assert b["sheet"]["projection_detected"] == "first_angle" and "projection_source" not in b["sheet"]


def test_disagreeing_sheets_leave_projection_null():
    ex = _extras()
    ex["sheets"].append({"name": "Sheet2", "projection": "first_angle", "views": []})
    a = _art()
    m.merge_extras(a, ex)
    assert a["sheet"]["projection_detected"] is None


def test_unambiguous_dimension_match_attaches_tol_and_text():
    a = _art()
    s = m.merge_extras(a, _extras())
    d0, d1, d2 = a["dimensions"]
    assert d0["sw_tol"]["max_mm"] == 0.1 and d0["sw_tol"]["min_mm"] == -0.1 and d0["sw_view"] == "Drawing View1"
    assert d1["sw_text"] == {"prefix": "4X "}
    assert "sw_tol" not in d2 and "sw_text" not in d2       # matched, but nothing to attach
    assert s["dimensions_matched"] == 2
    dims = a["sheet"]["slddrw"]["sheets"][0]["views"][0]["dimensions"]
    assert [bool(d.get("matched")) for d in dims] == [True, True, False]


def test_ambiguous_dimension_match_is_left_in_extras_only():
    a = _art()
    a["dimensions"].append({"value": 100.004, "kind": "linear"})     # two DXF dims for one SW dim
    s = m.merge_extras(a, _extras())
    assert "sw_tol" not in a["dimensions"][0] and "sw_tol" not in a["dimensions"][3]
    assert s["dimensions_ambiguous"] >= 2
    dims = a["sheet"]["slddrw"]["sheets"][0]["views"][0]["dimensions"]
    assert dims[0]["tol"]["type"] == 2 and not dims[0].get("matched")      # still listed, with tol

    b = _art()
    ex = _extras()
    ex["sheets"][0]["views"][0]["dimensions"].append(      # two SW dims for one DXF dim
        {"name": "D4@Drawing View1", "value_si": 0.1, "tol": {"type": 3}})
    m.merge_extras(b, ex)
    assert "sw_tol" not in b["dimensions"][0]


def test_angular_matches_in_degrees():
    a = {"sheet": {}, "dimensions": [{"value": 45.0, "kind": "angular"}]}
    ex = _extras()
    ex["sheets"][0]["views"][0]["dimensions"] = [
        {"name": "A1", "value_si": math.radians(45.0), "tol": {"type": 2, "max_si": math.radians(0.5)}}]
    m.merge_extras(a, ex)
    assert a["dimensions"][0]["sw_tol"]["max_deg"] == 0.5


def test_notes_pass_through_and_blank_dropped():
    a = _art()
    m.merge_extras(a, _extras())
    texts = [(n["text"], n.get("view")) for n in a["slddrw_notes"]]
    assert texts == [("BREAK ALL EDGES", None), ("4X O8 THRU", "Drawing View1")]
    assert a["slddrw_notes"][0]["pos"] == [0.1, 0.05]
    blk = a["sheet"]["slddrw"]
    assert blk["custom_properties"] == {"Material": "AL"}
    assert blk["sheets"][0]["views"][0]["referenced_model"] == "plate.sldprt"


def test_malformed_extras_ignored():
    for bad in (None, [], "x", {}, {"sheets": "no"}, {"sheets": [1, None]}):
        a = _art()
        before = json.dumps(a, sort_keys=True)
        assert m.merge_extras(a, bad)["merged"] is False
        assert json.dumps(a, sort_keys=True) == before
    a = _art()
    ex = {"sheets": [{"name": "S", "views": [{"name": "V", "dimensions": [
        "junk", {"value_si": "x"}, {"value_si": float("nan")}, {"value_si": 0.1, "tol": "bad", "text": 5}]},
        None], "notes": [5, {"text": 3}]}], "errors": [1, "boom"]}
    assert m.merge_extras(a, ex)["merged"]
    assert a["sheet"]["slddrw"]["errors"] == ["boom"] and "sw_tol" not in a["dimensions"][0]
    assert "slddrw_notes" not in a
    assert m.merge_extras("notadict", _extras())["merged"] is False


def test_parse_extras_from_response():
    payload = json.dumps({"view_count": 1, "extras": {"sheets": [], "errors": []}})
    ok = {"status": "COMPLETED", "cadState": {"features": [payload]}}
    assert m.parse_extras(ok) == {"sheets": [], "errors": []}
    assert m.parse_extras({"status": "FAILED"}) is None
    assert m.parse_extras({"status": "COMPLETED", "cadState": {"features": ["{not json", "{}"]}}) is None
    assert m.parse_extras(None) is None


def test_advisory_lists_models_and_errors():
    a = _art()
    ex = _extras(errors=["view_scale[V]: boom"])
    m.merge_extras(a, ex)
    line = m.advisory(a)
    assert "plate.sldprt" in line and "1 native sub-read" in line and "boom" in line
    assert m.advisory({"sheet": {}}) == ""


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
