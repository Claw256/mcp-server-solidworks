"""Offline tests for drawing_verify (no SolidWorks, no MCP).

Run:  python adapters/claude/tests/test_drawing_verify.py   |   pytest adapters/claude/tests/test_drawing_verify.py
"""
import math
import os
import sys

_ADAPTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

import drawing_verify as dv  # noqa: E402


def _hole(x, z, dia_mm, hole=True, u_span=2 * math.pi):
    return {"kind": "cylinder", "origin": [x, 0.0, z], "axis": [0, 1, 0],
            "radius": dia_mm / 2000.0, "hole": hole, "u_span": u_span}


_PLATE = {"bbox": {"size": [0.100, 0.020, 0.050]}, "volume": 1.0e-4, "cg": [0.05, 0.01, 0.025],
          "faces": [_hole(0.01, 0.01, 8), _hole(0.09, 0.01, 8), _hole(0.01, 0.04, 8),
                    _hole(0.09, 0.04, 8), _hole(0.05, 0.025, 20, hole=False)]}


def _status(res, item):
    return next(r["status"] for r in res["rows"] if r["item"] == item)


def test_matching_part_passes():
    res = dv.evaluate({"bbox_mm": [100, 50, 20], "volume_mm3": 100000,
                       "holes": [{"diameter_mm": 8, "count": 4}]}, _PLATE)
    assert res["verdict"] == "PASS", res
    assert _status(res, "hole Ø8") == "PASS"


def test_wrong_size_and_hole_count_fail_with_deltas():
    res = dv.evaluate({"bbox_mm": [100, 50, 25], "holes": [{"diameter_mm": 8, "count": 6}]}, _PLATE)
    assert res["verdict"] == "FAIL"
    assert _status(res, "bbox_mm") == "FAIL" and _status(res, "hole Ø8") == "FAIL"
    assert "worst delta 5.000" in next(r["detail"] for r in res["rows"] if r["item"] == "bbox_mm")


def test_boss_cylinder_is_not_a_hole_and_unlisted_hole_warns():
    res = dv.evaluate({"holes": [{"diameter_mm": 8, "count": 4}]}, _PLATE)
    assert not any(r["item"] == "hole Ø20" for r in res["rows"])     # the Ø20 is a boss
    faces = _PLATE["faces"] + [_hole(0.05, 0.025, 12)]               # an unlisted extra Ø12 hole
    res = dv.evaluate({"holes": [{"diameter_mm": 8, "count": 4}]}, dict(_PLATE, faces=faces))
    assert res["verdict"] == "PASS" and _status(res, "hole Ø12") == "WARN"


def test_split_hole_counts_once():
    half = lambda: _hole(0.05, 0.025, 10, u_span=math.pi)            # noqa: E731
    res = dv.evaluate({"holes": [{"diameter_mm": 10, "count": 1}]}, {"faces": [half(), half()]})
    assert res["verdict"] == "PASS", res


def test_bbox_axis_order_is_ignored_unless_requested():
    ok = dv.evaluate({"bbox_mm": [20, 100, 50]}, _PLATE)
    assert ok["verdict"] == "PASS"
    strict = dv.evaluate({"bbox_mm": [20, 100, 50], "bbox_ordered": True}, _PLATE)
    assert strict["verdict"] == "FAIL"


def test_cg_catches_mirrored_feature_that_volume_cannot():
    res = dv.evaluate({"volume_mm3": 100000, "cg_mm": [60, 10, 25]}, _PLATE)
    assert _status(res, "volume_mm3") == "PASS" and _status(res, "cg_mm") == "FAIL"


def test_missing_measurements_are_reported_not_passed():
    res = dv.evaluate({"bbox_mm": [1, 2, 3]}, {"bbox": None})
    assert res["verdict"] == "FAIL" and any("bbox_mm" in u for u in res["unchecked"])
    assert dv.evaluate({}, _PLATE)["verdict"] == "FAIL"              # nothing compared is not a pass


def test_missing_hole_flag_warns():
    faces = [{"kind": "cylinder", "origin": [0, 0, 0], "axis": [0, 1, 0], "radius": 0.004}]
    res = dv.evaluate({"holes": [{"diameter_mm": 8}]}, {"faces": faces})
    assert _status(res, "holes.sense") == "WARN"


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
