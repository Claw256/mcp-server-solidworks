"""Pure-string tests for callouts.py (hole callouts, tolerances, projection, MTEXT cleaning).

Run:  python cad-planner/drawing/tests/test_callouts.py   |   pytest cad-planner/drawing/tests/test_callouts.py
"""
import os
import sys

_PKG = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # cad-planner/
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

from drawing import callouts as c  # noqa: E402


def test_clean_decodes_cad_escapes_and_mtext_formatting():
    assert c.clean("4X %%c8 THRU") == "4X Ø8 THRU"
    assert c.clean("{\\fArial|b0;M6}\\Px1.0") == "M6 x1.0"
    assert c.clean("\\U+2300 20 %%p0.1") == "⌀ 20 ±0.1"


def test_through_hole_with_count():
    r = c.parse_callout("4X Ø8 THRU")
    assert r["count"] == 4 and r["dia"] == 8.0 and r["thru"] is True and r["kind"] == "hole"
    assert c.parse_callout("4X %%c8 THRU")["dia"] == 8.0


def test_tapped_hole_with_pitch_and_depth():
    r = c.parse_callout("M6x1 ▽12")
    assert r["thread"] == "M6x1" and r["pitch"] == 1.0 and r["dia"] == 6.0
    assert r["depth"] == 12.0 and r["kind"] == "tapped"
    assert c.parse_callout("2X M8 THRU")["count"] == 2


def test_counterbore_and_countersink():
    r = c.parse_callout("Ø8 ⌴Ø14 ▽6")
    assert r["dia"] == 8.0 and r["cbore"] == {"dia": 14.0, "depth": 6.0} and "depth" not in r
    r = c.parse_callout("Ø8 ⌵Ø16 x 90°")
    assert r["dia"] == 8.0 and r["csink"] == {"dia": 16.0, "angle_deg": 90.0}
    assert c.parse_callout("Ø9 CBORE Ø15 DEPTH 8")["cbore"] == {"dia": 15.0, "depth": 8.0}


def test_plain_dimension_labels_and_prose_are_not_callouts():
    for txt in ("Ø8", "100", "SCALE 1:2", "DEBURR ALL EDGES", "A-A", "R5", "4x5"):
        assert c.parse_callout(txt) is None, txt


def test_tolerances():
    assert c.parse_tolerance("50 ±0.1") == {"plus": 0.1, "minus": 0.1}
    assert c.parse_tolerance("50 %%p0.05") == {"plus": 0.05, "minus": 0.05}
    assert c.parse_tolerance("50 +0.2/-0.1") == {"plus": 0.2, "minus": 0.1}
    assert c.parse_tolerance("50 -0.1/+0.2") == {"plus": 0.2, "minus": 0.1}
    assert c.parse_tolerance("Ø20 H7") == {"fit": "H7"}
    assert c.parse_tolerance("Ø20 H7/g6") == {"fit": "H7/g6"}
    assert c.parse_tolerance("20 h6") == {"fit": "h6"}
    assert c.parse_tolerance("50.05 49.95") == {"limits": [49.95, 50.05]}
    for txt in ("50", "M6", "R5", "Ø8", ""):
        assert c.parse_tolerance(txt) is None, txt


def test_projection_detection():
    assert c.detect_projection(["THIRD ANGLE PROJECTION"]) == "third_angle"
    assert c.detect_projection(["Projektionsmethode: 1. Winkel"]) == "first_angle"
    assert c.detect_projection(["PROJECTION", "ISO E"]) == "first_angle"
    assert c.detect_projection(["DRAWN BY", "SCALE 1:1"]) is None


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
